"""The cast server: a DLNA media server and HLS relay on the local network.

VLC on Xbox has no remote-control interface, so Cue cannot start playback there.
Instead, what you cast from Cue appears in VLC's Network tab under this media
server. The TV plays each episode through Cue's relay, which resolves the stream
when it is opened, adds the headers and subtitles the source needs, and follows
segment requests to save resume positions and watched markers.

The HTTP server runs on its own threads. Provider, subtitle and database work is
handed to the app's event loop, which owns those objects.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import mimetypes
import re
import socket
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import httpx

from cue import __version__
from cue.cast import hls
from cue.cast.devices import Media
from cue.cast.network import lan_address
from cue.cast.ssdp import Advertiser
from cue.player.session import RESUME_MIN, WATCHED_AT, PlaybackSession
from cue.providers.base import Episode, Provider, ProviderError, Show, Stream, is_local_file
from cue.selection import select_quality
from cue.store import Store
from cue.subtitles import SubtitleError
from cue.subtitles.provider import ProviderSubtitles
from cue.util import format_time

log = logging.getLogger(__name__)

DEFAULT_PORT = 8770
KEEP_CASTS = 5
RESOLVE_TIMEOUT = 90.0
RESOLVED_FOR = 20 * 60.0  # signed source URLs expire; resolve again after this
SUBTITLE_TIMEOUT = 20.0
PREFETCH = 20.0  # players request segments ahead of what is on screen
SAVE_EVERY = 5.0
MAX_PLAYLIST_BYTES = 8 * 1024 * 1024
MAX_TARGETS = 100_000
CHUNK = 256 * 1024
PLAYLIST_TYPE = "application/vnd.apple.mpegurl"
# VLC's DLNA client only plays items whose protocolInfo is a video/ type; it then
# detects HLS from the response itself.
ITEM_PROTOCOL = "http-get:*:video/mp2t:*"
_MEDIA_PATH = re.compile(r"/(?:play|r|s)/")  # what TVs play; the rest is the DLNA server's own pages
_HOST = re.compile(r"[A-Za-z0-9.\-]+(?::\d{1,5})?|\[[0-9A-Fa-f:.]+\](?::\d{1,5})?")


@dataclass
class _Cast:
    generation: int
    provider_name: str
    provider: Provider
    show: Show
    queue: list[Episode]
    resume: dict[int, float]
    watched: set[float]

    def label(self, index: int) -> str:
        if self.show.media_type == "movie":
            return self.show.title
        return f"{self.show.title} · episode {self.queue[index].label}"


@dataclass
class _Subtitle:
    name: str
    language: str
    vtt: str
    duration: float
    default: bool = False


@dataclass
class _Resolved:
    stream: Stream
    subtitles: list[_Subtitle]
    at: float
    duration: float = 0.0


@dataclass(frozen=True)
class _Target:
    """An upstream URL the relay may fetch, with what it belongs to."""

    url: str
    kind: str  # "root", "playlist", "subtitles" or "segment"
    generation: int
    index: int
    start: float | None
    stream_url: str
    headers: tuple[tuple[str, str], ...]


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return parts.scheme, (parts.hostname or "").lower(), parts.port or (443 if parts.scheme == "https" else 80)


class CastServer:
    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        store: Store,
        provider_subtitles: ProviderSubtitles | None = None,
        subtitle_languages: Sequence[str] = ("en",),
        quality: str = "best",
        name: str = "Cue",
        host: str = "",
        port: int = DEFAULT_PORT,
        bind: str = "",
        on_status: Callable[[str], None] | None = None,
        announce: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.name = name
        self.address = host or lan_address()
        self.port = port
        self._bind = bind
        self.uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"cue-cast.{socket.gethostname()}"))
        self.update_id = 1
        self._loop = loop
        self._store = store
        self._subtitles = provider_subtitles
        self._languages = list(subtitle_languages) or ["en"]
        self._quality = quality
        self._on_status = on_status
        self._announce = announce
        self.client = httpx.Client(
            timeout=httpx.Timeout(20.0, read=60.0), follow_redirects=True, max_redirects=5, transport=transport,
        )
        self._lock = threading.Lock()
        self._generation = 0
        self._casts: OrderedDict[int, _Cast] = OrderedDict()
        self._resolved: dict[tuple[int, int], _Resolved] = {}
        self._resolving: dict[tuple[int, int], threading.Lock] = {}
        self._targets: OrderedDict[str, _Target] = OrderedDict()
        self._last_progress: dict[tuple[int, int], float] = {}
        self._http: ThreadingHTTPServer | None = None
        self._ssdp: Advertiser | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.address}:{self.port}"

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        """Listen (on every interface by default); raises OSError when the port is taken."""
        server = _HttpServer((self._bind, self.port), _Handler)
        server.cast = self
        self.port = server.server_address[1]
        self._http = server
        threading.Thread(target=server.serve_forever, name="cue-cast-http", daemon=True).start()
        log.info("Cast server at %s, listening on %s", self.base_url, self._bind or "every network adapter")
        if self._announce:
            advertiser = Advertiser(
                uuid=self.uuid, location=f"{self.base_url}/dlna/description.xml",
                address=self.address, server=f"Windows UPnP/1.0 Cue/{__version__}",
            )
            try:
                advertiser.start()
                self._ssdp = advertiser
            except OSError as exc:
                log.warning("Cannot announce the cast server on the network: %s", exc)

    def stop(self) -> None:
        if self._ssdp is not None:
            self._ssdp.stop()
            self._ssdp = None
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
        self.client.close()

    # -- casting (event loop thread) ------------------------------------------------

    def publish(self, provider_name: str, provider: Provider, show: Show, queue: Sequence[Episode]) -> str:
        """Make a queue available to the TV; returns the URL of its first item."""
        generation, first = self.publish_queue(provider_name, provider, show, queue)
        return self.item_url(generation, first)

    def publish_queue(
        self, provider_name: str, provider: Provider, show: Show, queue: Sequence[Episode],
    ) -> tuple[int, int]:
        """Make a queue available to the TV; returns its generation and the item to start with."""
        entry = self._store.get_history(provider_name, show.id)
        resume = {
            index: entry.position for index, episode in enumerate(queue)
            if entry and entry.position >= RESUME_MIN and episode.number == entry.last_episode
        }
        watched = self._store.watched(provider_name, show.id)
        with self._lock:
            self._generation += 1
            cast = _Cast(self._generation, provider_name, provider, show, list(queue), resume, watched)
            self._casts[cast.generation] = cast
            while len(self._casts) > KEEP_CASTS:
                old, _ = self._casts.popitem(last=False)
                for key in [key for key in self._resolved if key[0] == old]:
                    del self._resolved[key]
            self.update_id += 1
        return cast.generation, next(iter(resume), 0)

    def item_url(self, generation: int, index: int) -> str:
        return f"{self.base_url}/play/{generation}/{index}"

    async def media(self, generation: int, index: int) -> Media:
        """An item for a TV Cue tells what to play (AirPlay, Chromecast): resolved, with its type."""
        cast = self.cast(generation)
        if cast is None or not 0 <= index < len(cast.queue):
            raise ProviderError("This cast was replaced")
        url = self.item_url(generation, index)
        content_type = await asyncio.to_thread(self._content_type, url)
        return Media(url, cast.label(index), content_type, cast.resume.get(index, 0.0))

    def _content_type(self, url: str) -> str:
        """Open an item as a TV would, which also resolves it; Chromecast needs the type up front."""
        try:
            with httpx.Client(trust_env=False, timeout=RESOLVE_TIMEOUT + 15) as client, client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise ProviderError(response.read().decode("utf-8", "replace").strip()
                                        or f"HTTP {response.status_code}")
                return response.headers.get("content-type", "video/mp4").split(";")[0].strip()
        except httpx.HTTPError as exc:
            raise ProviderError(f"Cue's cast server didn't answer ({type(exc).__name__})") from exc

    def finished(self, generation: int, index: int) -> bool:
        """Whether the TV watched this item to the end (as far as its requests show)."""
        cast = self.cast(generation)
        return cast is not None and 0 <= index < len(cast.queue) and cast.queue[index].number in cast.watched

    def queue_length(self, generation: int) -> int:
        cast = self.cast(generation)
        return len(cast.queue) if cast is not None else 0

    def _status(self, text: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(text)
            except Exception:
                log.exception("Cast status listener failed")

    def _call(self, callback: Callable[..., Any], *args: Any) -> None:
        """Run on the event loop; the app may already be closing."""
        try:
            self._loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            pass

    async def _resolve(self, cast: _Cast, index: int) -> _Resolved:
        self._status(f"Cast: loading {cast.label(index)}…")
        stream = select_quality(await cast.provider.stream(cast.queue[index]), self._quality)
        if stream is None:
            raise ProviderError("No playable stream for this episode")
        subtitles = await self._load_subtitles(stream)
        self._status(f"Cast: {cast.label(index)} is playing on the TV")
        return _Resolved(stream, subtitles, time.monotonic())

    async def _load_subtitles(self, stream: Stream) -> list[_Subtitle]:
        if self._subtitles is None:
            return []
        tracks = self._subtitles.choices(stream)
        if not tracks:
            return []

        async def load(track):
            try:
                path = Path(await self._subtitles.fetch(track))
                vtt = hls.to_webvtt(path.read_text(encoding="utf-8"), path.suffix)
            except (SubtitleError, OSError, UnicodeError) as exc:
                log.info("Cast subtitle %s unavailable: %s", track.label, exc)
                return None
            if vtt is None:
                return None
            return _Subtitle(track.label or track.language, track.language, vtt, hls.webvtt_duration(vtt))

        tasks = [asyncio.ensure_future(load(track)) for track in tracks]
        done, pending = await asyncio.wait(tasks, timeout=SUBTITLE_TIMEOUT)
        for task in pending:
            task.cancel()
        loaded = [
            task.result() for task in tasks
            if task in done and task.exception() is None and task.result() is not None
        ]
        # The TV shows the preferred language by default; the rest stay in VLC's menu.
        for preference in self._languages:
            match = next((sub for sub in loaded if PlaybackSession._language_matches(sub.language, preference)), None)
            if match is not None:
                match.default = True
                break
        return loaded

    def _record(self, generation: int, index: int, start: float) -> None:
        cast = self._casts.get(generation)
        if cast is None:
            return
        resolved = self._resolved.get((generation, index))
        total = resolved.duration if resolved else 0.0
        episode = cast.queue[index]
        show = cast.show
        if total and start >= total * WATCHED_AT:
            if episode.number in cast.watched and index not in cast.resume:
                return  # already recorded; later segments change nothing
            self._store.mark_watched(cast.provider_name, show.id, episode.number)
            self._store.save_progress(cast.provider_name, show.id, show.title, episode.number, 0.0)
            cast.watched.add(episode.number)
            cast.resume.pop(index, None)
            self._status(f"Cast: finished {cast.label(index)}")
            return
        position = max(0.0, start - PREFETCH)
        if position < RESUME_MIN:
            return  # opening a title must not erase an earlier resume point
        self._store.save_progress(cast.provider_name, show.id, show.title, episode.number, position)
        cast.resume[index] = position
        self._status(f"Cast: {cast.label(index)} at about {format_time(position)}")

    # -- server threads ---------------------------------------------------------------

    def cast(self, generation: int) -> _Cast | None:
        with self._lock:
            return self._casts.get(generation)

    def casts(self) -> list[_Cast]:
        with self._lock:
            return list(reversed(self._casts.values()))

    def resolve(self, cast: _Cast, index: int) -> _Resolved:
        key = (cast.generation, index)
        with self._lock:
            lock = self._resolving.setdefault(key, threading.Lock())
        with lock:
            current = self._resolved.get(key)
            if current is not None and time.monotonic() - current.at < RESOLVED_FOR:
                return current
            try:
                future = asyncio.run_coroutine_threadsafe(self._resolve(cast, index), self._loop)
            except RuntimeError as exc:
                raise ProviderError("Cue is closing") from exc
            try:
                resolved = future.result(RESOLVE_TIMEOUT)
            except TimeoutError as exc:
                future.cancel()
                raise ProviderError("Timed out while finding the stream") from exc
            except ProviderError as exc:
                self._call(self._status, f"Cast: {cast.label(index)} failed: {exc}")
                raise
            except Exception as exc:
                log.exception("Cast could not resolve %s", cast.label(index))
                self._call(self._status, f"Cast: {cast.label(index)} failed: {exc}")
                raise ProviderError(f"Could not load the stream: {exc}") from exc
            self._resolved[key] = resolved
            return resolved

    def subtitle(self, generation: int, index: int, number: int) -> _Subtitle | None:
        resolved = self._resolved.get((generation, index))
        return resolved.subtitles[number] if resolved and number < len(resolved.subtitles) else None

    def target(self, token: str) -> _Target | None:
        with self._lock:
            return self._targets.get(token)

    def register(self, url: str, kind: str, generation: int, index: int, start: float | None, stream: Stream) -> str:
        digest = hashlib.sha256(f"{generation}|{index}|{kind}|{start}|{url}".encode()).hexdigest()[:32]
        target = _Target(url, kind, generation, index, start, stream.url, tuple((stream.headers or {}).items()))
        with self._lock:
            self._targets[digest] = target
            self._targets.move_to_end(digest)
            while len(self._targets) > MAX_TARGETS:
                self._targets.popitem(last=False)
        return digest

    def upstream_headers(self, target: _Target) -> dict[str, str]:
        # A source's credentials only go to its own origin; other hosts (CDNs)
        # get the browser-style Referer/User-Agent the stream asked for.
        same_origin = _origin(target.url) == _origin(target.stream_url)
        headers = {
            name: value for name, value in target.headers
            if same_origin or name.lower() in {"referer", "user-agent"}
        }
        if not any(name.lower() == "user-agent" for name in headers):
            headers["User-Agent"] = f"Cue/{__version__}"
        headers["Accept-Encoding"] = "identity"  # segments are passed through byte for byte
        return headers

    def progress(self, target: _Target) -> None:
        if target.start is None:
            return
        key = (target.generation, target.index)
        with self._lock:
            last = self._last_progress.get(key)
            if last is not None and abs(last - target.start) < SAVE_EVERY:
                return
            self._last_progress[key] = target.start
        self._call(self._record, target.generation, target.index, target.start)

    def rewrite(self, target: _Target, text: str, base: str, host: str) -> str:
        """Relay a fetched playlist, adding Cue's subtitles to the stream's master playlist."""
        resolved = self._resolved.get((target.generation, target.index))
        cast = self.cast(target.generation)
        if resolved is None or cast is None:
            raise ProviderError("This cast has expired; cast it again from Cue")

        def register(url: str, kind: str, start: float | None) -> str:
            token = self.register(url, kind, target.generation, target.index, start, resolved.stream)
            return f"http://{host}/r/{token}"

        if target.kind == "root":
            renditions = [
                hls.Rendition(sub.name, sub.language or "und", f"http://{host}/s/{target.generation}/{target.index}/{n}.m3u8", sub.default)
                for n, sub in enumerate(resolved.subtitles)
            ]
            if hls.is_master(text):
                return hls.rewrite_master(text, base, register, renditions)
            if renditions:
                return hls.wrap_media(register(base, "playlist", None), renditions)
        if target.kind == "subtitles":
            return hls.rewrite_media(text, base, register, track_progress=False)[0]
        rewritten, total = hls.rewrite_media(text, base, register)
        resolved.duration = max(resolved.duration, total)
        return rewritten

    # -- DLNA -------------------------------------------------------------------------

    def title(self, cast: _Cast, index: int) -> str:
        episode = cast.queue[index]
        if cast.show.media_type == "movie":
            title = cast.show.title
        else:
            title = f"{episode.label}. {episode.title}" if episode.title else f"Episode {episode.label}"
        if episode.number in cast.watched:
            title = "✓ " + title
        if index in cast.resume:
            title += f" (resume {format_time(cast.resume[index])})"
        return title

    def _container(self, cast: _Cast) -> str:
        mode = getattr(cast.provider, "language", None)
        title = cast.show.title + (f" [{mode.upper()}]" if isinstance(mode, str) else "")
        return (
            f'<container id="c{cast.generation}" parentID="0" restricted="1" childCount="{len(cast.queue)}">'
            f"<dc:title>{escape(title)}</dc:title><upnp:class>object.container.storageFolder</upnp:class></container>"
        )

    def _item(self, cast: _Cast, index: int, host: str) -> str:
        # Subtitles travel inside the relayed HLS playlist, named and tagged with
        # their language. VLC 3 shows one by default only when it matches the
        # player's preferred subtitle language; a DLNA subtitle link would only
        # add an unnamed duplicate.
        url = f"http://{host}/play/{cast.generation}/{index}"
        return (
            f'<item id="c{cast.generation}.{index}" parentID="c{cast.generation}" restricted="1">'
            f"<dc:title>{escape(self.title(cast, index))}</dc:title><upnp:class>object.item.videoItem</upnp:class>"
            f'<res protocolInfo="{ITEM_PROTOCOL}">{escape(url)}</res></item>'
        )

    def browse(self, object_id: str, flag: str, start: int, count: int, host: str) -> tuple[list[str], int] | None:
        """DIDL-Lite elements for a ContentDirectory Browse, or None for an unknown object."""
        casts = self.casts()
        if object_id == "0":
            if flag == "BrowseMetadata":
                return [f'<container id="0" parentID="-1" restricted="1" childCount="{max(1, len(casts))}"><dc:title>{escape(self.name)}</dc:title><upnp:class>object.container.storageFolder</upnp:class></container>'], 1
            children = [self._container(cast) for cast in casts] or [
                '<container id="empty" parentID="0" restricted="1" childCount="0"><dc:title>Nothing cast yet: '
                "press t on an episode in Cue</dc:title><upnp:class>object.container.storageFolder</upnp:class></container>"
            ]
        elif object_id == "empty":
            if flag == "BrowseMetadata":
                return None
            children = []
        else:
            match = re.fullmatch(r"c(\d+)(?:\.(\d+))?", object_id)
            cast = self.cast(int(match[1])) if match else None
            if cast is None or (match[2] is not None and int(match[2]) >= len(cast.queue)):
                return None
            if match[2] is not None:
                if flag != "BrowseMetadata":
                    return [], 0
                return [self._item(cast, int(match[2]), host)], 1
            if flag == "BrowseMetadata":
                return [self._container(cast)], 1
            children = [self._item(cast, index, host) for index in range(len(cast.queue))]
        page = children[start:start + count] if count else children[start:]
        return page, len(children)

    def description(self) -> str:
        return _DESCRIPTION.format(name=escape(self.name), uuid=self.uuid, version=__version__)


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows it would let two programs share the port
    cast: CastServer

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.debug("Cast request from %s failed", client_address[0], exc_info=True)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"Cue/{__version__}"
    server: _HttpServer

    def log_message(self, format: str, *args: Any) -> None:  # never print over the TUI
        # Opening, browsing and failures go to cue.log, so it shows whether the TV
        # reached Cue at all; the steady stream of segment requests does not.
        failed = len(args) > 1 and str(args[1]).isdigit() and int(args[1]) >= 400
        routine = getattr(self, "path", "").startswith(("/r/", "/s/")) and not failed
        log.log(logging.DEBUG if routine else logging.INFO, "cast %s - " + format, self.client_address[0], *args)

    @property
    def cast(self) -> CastServer:
        return self.server.cast

    def _host(self) -> str:
        host = self.headers.get("Host", "")
        return host if _HOST.fullmatch(host) else f"{self.cast.address}:{self.cast.port}"

    def _reply(self, status: int, body: bytes | str, content_type: str, *, head: bool = False,
               headers: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _error(self, status: int, message: str, head: bool = False) -> None:
        self._reply(status, message + "\n", "text/plain; charset=utf-8", head=head)

    def end_headers(self) -> None:
        # Chromecast's receiver is a web page: it may read media only from servers that allow it.
        if _MEDIA_PATH.match(urlsplit(self.path).path):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Expose-Headers", "Content-Length, Content-Range, Accept-Ranges")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # a CORS preflight, for range requests
        self.send_response(204)
        if _MEDIA_PATH.match(urlsplit(self.path).path):
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", self.headers.get("Access-Control-Request-Headers", "*"))
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routing -----------------------------------------------------------------------

    def do_GET(self) -> None:
        self._get(head=False)

    def do_HEAD(self) -> None:
        self._get(head=True)

    def _get(self, *, head: bool) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/":
                self._reply(200, self._index(), "text/html; charset=utf-8", head=head)
            elif path == "/now":
                casts = self.cast.casts()
                if not casts:
                    self._error(404, "Nothing has been cast yet. Press t on an episode in Cue.", head)
                    return
                start = next(iter(casts[0].resume), 0)
                location = f"http://{self._host()}/play/{casts[0].generation}/{start}"
                self._reply(302, "", "text/plain", head=head, headers={"Location": location})
            elif path == "/dlna/description.xml":
                self._reply(200, self.cast.description(), "text/xml; charset=utf-8", head=head)
            elif path == "/dlna/cds.xml":
                self._reply(200, _CDS_SCPD, "text/xml; charset=utf-8", head=head)
            elif path == "/dlna/cms.xml":
                self._reply(200, _CMS_SCPD, "text/xml; charset=utf-8", head=head)
            elif match := re.fullmatch(r"/play/(\d+)/(\d+)", path):
                self._play(int(match[1]), int(match[2]), head)
            elif match := re.fullmatch(r"/r/([0-9a-f]{32})", path):
                target = self.cast.target(match[1])
                if target is None:
                    self._error(404, "This link has expired; cast again from Cue.", head)
                else:
                    self._relay(target, head)
            elif match := re.fullmatch(r"/s/(\d+)/(\d+)/(\d+)\.(m3u8|vtt)", path):
                self._subtitle(int(match[1]), int(match[2]), int(match[3]), match[4], head)
            else:
                self._error(404, "Not found", head)
        except (ConnectionError, TimeoutError):
            self.close_connection = True  # the TV went away mid-response

    def _index(self) -> str:
        rows = []
        for cast in self.cast.casts():
            items = "".join(
                f'<li><a href="/play/{cast.generation}/{index}">{html.escape(self.cast.title(cast, index))}</a></li>'
                for index in range(len(cast.queue))
            )
            rows.append(f"<h2>{html.escape(cast.show.title)}</h2><ol>{items}</ol>")
        body = "".join(rows) or "<p>Nothing has been cast yet. Press t on an episode in Cue.</p>"
        return f"<!doctype html><meta charset=utf-8><title>{html.escape(self.cast.name)}</title><h1>{html.escape(self.cast.name)}</h1>{body}"

    # -- media -------------------------------------------------------------------------

    def _play(self, generation: int, index: int, head: bool) -> None:
        cast = self.cast.cast(generation)
        if cast is None or index >= len(cast.queue):
            self._error(404, "This cast was replaced; cast again from Cue.", head)
            return
        try:
            resolved = self.cast.resolve(cast, index)
        except ProviderError as exc:
            self._error(502, str(exc), head)
            return
        stream = resolved.stream
        if is_local_file(stream.url):
            self._file(Path(stream.url), head)
            return
        token = self.cast.register(stream.url, "root", generation, index, None, stream)
        self._relay(self.cast.target(token), head)

    def _relay(self, target: _Target | None, head: bool) -> None:
        if target is None:
            self._error(404, "This link has expired; cast again from Cue.", head)
            return
        headers = self.cast.upstream_headers(target)
        if target.kind in {"root", "segment"} and self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        try:
            request = self.cast.client.build_request("GET", target.url, headers=headers)
            response = self.cast.client.send(request, stream=True)
        except httpx.HTTPError as exc:
            self._error(502, f"The source did not answer ({type(exc).__name__})", head)
            return
        try:
            if response.status_code >= 400:
                self._error(response.status_code, f"The source refused the request (HTTP {response.status_code})", head)
                return
            # Segments pass through byte for byte; playlists must be decoded to rewrite.
            # A response that was already read can only be iterated decoded.
            decoded = target.kind != "segment" or response.is_stream_consumed
            chunks = response.iter_bytes(CHUNK) if decoded else response.iter_raw(CHUNK)
            first = next(chunks, b"")
            if target.kind != "segment" and hls.is_playlist(first):
                body = bytearray(first)
                for chunk in chunks:
                    body.extend(chunk)
                    if len(body) > MAX_PLAYLIST_BYTES:
                        self._error(502, "The source playlist is too large", head)
                        return
                try:
                    text = self.cast.rewrite(target, body.decode("utf-8", "replace"), str(response.url), self._host())
                except ProviderError as exc:
                    self._error(410, str(exc), head)
                    return
                self._reply(200, text, PLAYLIST_TYPE, head=head)
                return
            self.cast.progress(target)
            self._pass_through(response, first, chunks, head, decoded=decoded)
        finally:
            response.close()

    def _pass_through(self, response: httpx.Response, first: bytes, chunks: Any, head: bool, *, decoded: bool) -> None:
        # Decoded bytes no longer match a compressed response's length or encoding.
        exact = not (decoded and "content-encoding" in response.headers)
        self.send_response(response.status_code)
        self.send_header("Content-Type", response.headers.get("content-type", "application/octet-stream"))
        for name in ("content-length", "content-range", "accept-ranges", "content-encoding"):
            if name in response.headers and (exact or name in {"content-range", "accept-ranges"}):
                self.send_header(name.title(), response.headers[name])
        if "content-length" not in response.headers or not exact:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if head:
            return
        self.wfile.write(first)
        for chunk in chunks:
            self.wfile.write(chunk)

    def _file(self, path: Path, head: bool) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            self._error(404, "The file is no longer available", head)
            return
        start, end, status = 0, size - 1, 200
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if match and (match[1] or match[2]):
            if match[1]:
                start, end = int(match[1]), int(match[2]) if match[2] else size - 1
            else:
                start = max(0, size - int(match[2]))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        with path.open("rb") as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = source.read(min(CHUNK, remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)

    def _subtitle(self, generation: int, index: int, number: int, kind: str, head: bool) -> None:
        subtitle = self.cast.subtitle(generation, index, number)
        if subtitle is None:
            self._error(404, "Subtitle not found", head)
            return
        if kind == "vtt":
            self._reply(200, subtitle.vtt, "text/vtt; charset=utf-8", head=head)
        else:
            url = f"http://{self._host()}/s/{generation}/{index}/{number}.vtt"
            self._reply(200, hls.subtitle_playlist(url, subtitle.duration), PLAYLIST_TYPE, head=head)

    # -- UPnP control ------------------------------------------------------------------

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 64 * 1024)
        except ValueError:
            length = 0
        body = self.rfile.read(length).decode("utf-8", "replace") if length > 0 else ""
        action = self.headers.get("SOAPACTION", "").strip().strip('"').rpartition("#")[2]
        if path == "/dlna/cds/control":
            self._content_directory(action, body)
        elif path == "/dlna/cms/control":
            if action == "GetProtocolInfo":
                self._soap("ConnectionManager", action, {"Source": ITEM_PROTOCOL, "Sink": ""})
            elif action == "GetCurrentConnectionIDs":
                self._soap("ConnectionManager", action, {"ConnectionIDs": "0"})
            else:
                self._fault(401, "Invalid Action")
        else:
            self._error(404, "Not found")

    def _content_directory(self, action: str, body: str) -> None:
        def argument(name: str, default: str = "") -> str:
            match = re.search(rf"<(?:\w+:)?{name}\b[^>]*>(.*?)</(?:\w+:)?{name}>", body, re.S)
            return html.unescape(match[1].strip()) if match else default

        if action == "Browse":
            try:
                start = max(0, int(argument("StartingIndex", "0") or 0))
                count = max(0, int(argument("RequestedCount", "0") or 0))
            except ValueError:
                self._fault(402, "Invalid Args")
                return
            result = self.cast.browse(argument("ObjectID", "0"), argument("BrowseFlag", "BrowseDirectChildren"), start, count, self._host())
            if result is None:
                self._fault(701, "No such object")
                return
            elements, total = result
            didl = (
                '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
                + "".join(elements) + "</DIDL-Lite>"
            )
            self._soap("ContentDirectory", action, {
                "Result": didl, "NumberReturned": str(len(elements)),
                "TotalMatches": str(total), "UpdateID": str(self.cast.update_id),
            })
        elif action == "GetSystemUpdateID":
            self._soap("ContentDirectory", action, {"Id": str(self.cast.update_id)})
        elif action == "GetSearchCapabilities":
            self._soap("ContentDirectory", action, {"SearchCaps": ""})
        elif action == "GetSortCapabilities":
            self._soap("ContentDirectory", action, {"SortCaps": ""})
        else:
            self._fault(401, "Invalid Action")

    def _soap(self, service: str, action: str, values: dict[str, str]) -> None:
        arguments = "".join(f"<{name}>{escape(value)}</{name}>" for name, value in values.items())
        self._reply(200, (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:{service}:1">{arguments}</u:{action}Response>'
            "</s:Body></s:Envelope>"
        ), 'text/xml; charset="utf-8"', headers={"EXT": ""})

    def _fault(self, code: int, description: str) -> None:
        self._reply(500, (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault>'
            "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
            f'<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>{code}</errorCode>'
            f"<errorDescription>{escape(description)}</errorDescription></UPnPError>"
            "</detail></s:Fault></s:Body></s:Envelope>"
        ), 'text/xml; charset="utf-8"')

    def do_SUBSCRIBE(self) -> None:
        # Cue sends no change events; accepting keeps strict control points content.
        self.send_response(200)
        self.send_header("SID", f"uuid:{uuid.uuid4()}")
        self.send_header("TIMEOUT", "Second-1800")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_UNSUBSCRIBE(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


_DESCRIPTION = """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<device>
<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
<friendlyName>{name}</friendlyName>
<manufacturer>Cue</manufacturer>
<modelName>Cue</modelName>
<modelNumber>{version}</modelNumber>
<modelDescription>Cue terminal media player</modelDescription>
<UDN>uuid:{uuid}</UDN>
<serviceList>
<service>
<serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
<serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
<SCPDURL>/dlna/cds.xml</SCPDURL>
<controlURL>/dlna/cds/control</controlURL>
<eventSubURL>/dlna/cds/events</eventSubURL>
</service>
<service>
<serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
<serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
<SCPDURL>/dlna/cms.xml</SCPDURL>
<controlURL>/dlna/cms/control</controlURL>
<eventSubURL>/dlna/cms/events</eventSubURL>
</service>
</serviceList>
</device>
</root>
"""


def _scpd(actions: dict[str, list[tuple[str, str, str]]], variables: dict[str, str]) -> str:
    action_xml = "".join(
        f"<action><name>{name}</name><argumentList>"
        + "".join(
            f"<argument><name>{argument}</name><direction>{direction}</direction>"
            f"<relatedStateVariable>{variable}</relatedStateVariable></argument>"
            for argument, direction, variable in arguments
        )
        + "</argumentList></action>"
        for name, arguments in actions.items()
    )
    variable_xml = "".join(
        f'<stateVariable sendEvents="no"><name>{name}</name><dataType>{kind}</dataType></stateVariable>'
        for name, kind in variables.items()
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?><scpd xmlns="urn:schemas-upnp-org:service-1-0">'
        "<specVersion><major>1</major><minor>0</minor></specVersion>"
        f"<actionList>{action_xml}</actionList><serviceStateTable>{variable_xml}</serviceStateTable></scpd>"
    )


_CDS_SCPD = _scpd(
    {
        "Browse": [
            ("ObjectID", "in", "A_ARG_TYPE_ObjectID"), ("BrowseFlag", "in", "A_ARG_TYPE_BrowseFlag"),
            ("Filter", "in", "A_ARG_TYPE_Filter"), ("StartingIndex", "in", "A_ARG_TYPE_Index"),
            ("RequestedCount", "in", "A_ARG_TYPE_Count"), ("SortCriteria", "in", "A_ARG_TYPE_SortCriteria"),
            ("Result", "out", "A_ARG_TYPE_Result"), ("NumberReturned", "out", "A_ARG_TYPE_Count"),
            ("TotalMatches", "out", "A_ARG_TYPE_Count"), ("UpdateID", "out", "A_ARG_TYPE_UpdateID"),
        ],
        "GetSystemUpdateID": [("Id", "out", "SystemUpdateID")],
        "GetSearchCapabilities": [("SearchCaps", "out", "SearchCapabilities")],
        "GetSortCapabilities": [("SortCaps", "out", "SortCapabilities")],
    },
    {
        "A_ARG_TYPE_ObjectID": "string", "A_ARG_TYPE_BrowseFlag": "string", "A_ARG_TYPE_Filter": "string",
        "A_ARG_TYPE_Index": "ui4", "A_ARG_TYPE_Count": "ui4", "A_ARG_TYPE_SortCriteria": "string",
        "A_ARG_TYPE_Result": "string", "A_ARG_TYPE_UpdateID": "ui4", "SystemUpdateID": "ui4",
        "SearchCapabilities": "string", "SortCapabilities": "string",
    },
)
_CMS_SCPD = _scpd(
    {
        "GetProtocolInfo": [("Source", "out", "SourceProtocolInfo"), ("Sink", "out", "SinkProtocolInfo")],
        "GetCurrentConnectionIDs": [("ConnectionIDs", "out", "CurrentConnectionIDs")],
    },
    {"SourceProtocolInfo": "string", "SinkProtocolInfo": "string", "CurrentConnectionIDs": "string"},
)
