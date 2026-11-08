"""Let any player play any stream: a relay on 127.0.0.1, and downloads for players that only open files.

Sources often need HTTP headers (a Referer, or Jellyfin's login) and serve HLS
playlists, sometimes with audio in separate tracks. Most players can do none of that.
Cue steps in as each player needs:

* ``/h/<token>``: the source through Cue, with its headers added and every playlist
  entry pointing back at Cue. Any player that opens HLS links can play it.
* ``/ts/<id>``: one continuous MPEG-TS stream, for players without HLS. FFmpeg
  joins the stream's best video with its default audio (a direct video file is just
  passed through).
* ``save()``: the episode as a temporary MP4, for players that only open files, such
  as Windows' Media Player. Pieces are fetched several at a time into memory and fed
  to FFmpeg, so only the finished file touches the disk; yt-dlp is the fallback.

The relay listens on 127.0.0.1 only, so nothing outside this PC can use it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import httpx

from cue import __version__
from cue.cast import hls
from cue.player.base import Player, PlayerError
from cue.providers.base import Stream, is_local_file
from cue.util import safe_filename

log = logging.getLogger(__name__)

KEEP_STREAMS = 20
MAX_TARGETS = 100_000
MAX_PLAYLIST_BYTES = 8 * 1024 * 1024
CHUNK = 256 * 1024
PLAYLIST_TYPE = "application/vnd.apple.mpegurl"
PARALLEL = 12  # pieces downloaded at once (more gets throttled by some sources)
WINDOW = 24  # pieces held in memory ahead of FFmpeg
RETRIES = 3
_URI_ATTRIBUTE = re.compile(r'URI="([^"]*)"')
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,5}")
# Extensions FFmpeg accepts for HLS pieces (its allowed_segment_extensions), as written in links.
_MEDIA_SUFFIXES = {
    ".ts", ".m4s", ".mp4", ".m4v", ".m4a", ".aac", ".ac3", ".eac3", ".mp3", ".mp2", ".mpg", ".mpeg",
    ".mov", ".mkv", ".flac", ".ogg", ".oga", ".ogv", ".wav", ".vtt", ".webvtt", ".cmfv", ".cmfa",
}


def downloads_dir() -> Path:
    return Path(tempfile.gettempdir()) / "cue-playback"


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return parts.scheme, parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)


@dataclass(frozen=True)
class _Target:
    url: str
    kind: str  # root, single (the root, cut down for FFmpeg), playlist, subtitles or segment
    stream_id: str


@dataclass
class Shared:
    """Links to one stream through the relay."""

    stream_id: str
    hls: str  # for players that open HLS links
    single: str  # the source cut down to one video and one audio track, for FFmpeg
    continuous: str  # one MPEG-TS stream, for players without HLS
    duration: float = field(default=0.0)


class LocalRelay:
    def __init__(self, *, ffmpeg: str | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.ffmpeg = ffmpeg
        self.client = httpx.Client(
            timeout=httpx.Timeout(20.0, read=60.0), follow_redirects=True, max_redirects=5, transport=transport,
        )
        self._lock = threading.Lock()
        self._streams: OrderedDict[str, Stream] = OrderedDict()
        self._durations: dict[str, float] = {}
        self._heights: dict[str, int | None] = {}  # the resolution cap for each stream's cut-down playlist
        self._prefetches: dict[str, _Prefetch] = {}
        self._targets: OrderedDict[str, _Target] = OrderedDict()
        self._server: ThreadingHTTPServer | None = None
        self.port = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        server = _Server(("127.0.0.1", 0), _Handler)
        server.relay = self
        self.port = server.server_address[1]
        self._server = server
        threading.Thread(target=server.serve_forever, name="cue-relay", daemon=True).start()
        log.info("Local relay at %s", self.base_url)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self.client.close()

    # -- streams and links ---------------------------------------------------------------

    def share(self, stream: Stream, quality: str = "best") -> Shared:
        """Links to ``stream``; ``quality`` (``"720"``) caps the resolution of its cut-down playlist."""
        stream_id = uuid.uuid4().hex[:16]
        with self._lock:
            self._streams[stream_id] = stream
            self._heights[stream_id] = _height(quality)
            while len(self._streams) > KEEP_STREAMS:
                old, _ = self._streams.popitem(last=False)
                self._durations.pop(old, None)
                self._heights.pop(old, None)
        return Shared(
            stream_id,
            hls=self.link(stream.url, "root", stream_id),
            single=self.link(stream.url, "single", stream_id),
            continuous=f"{self.base_url}/ts/{stream_id}",
        )

    def stream(self, stream_id: str) -> Stream | None:
        with self._lock:
            return self._streams.get(stream_id)

    def duration(self, stream_id: str) -> float:
        return self._durations.get(stream_id, 0.0)

    def register(self, url: str, kind: str, stream_id: str) -> str:
        token = hashlib.sha256(f"{stream_id}|{kind}|{url}".encode()).hexdigest()[:32]
        with self._lock:
            self._targets[token] = _Target(url, kind, stream_id)
            self._targets.move_to_end(token)
            while len(self._targets) > MAX_TARGETS:
                self._targets.popitem(last=False)
        return token

    def link(self, url: str, kind: str, stream_id: str) -> str:
        """The relay link for ``url``, ending in an extension FFmpeg and players accept.

        FFmpeg only takes HLS pieces with media extensions, and some sources disguise
        theirs (``seg_00000.ts.jpg``), so a piece is named by its media extension, or
        ``.ts`` when it shows none. Playlists end in ``.m3u8``.
        """
        if kind in {"playlist", "subtitles"}:
            suffix = ".m3u8"
        elif kind == "segment":
            suffixes = [s.lower() for s in PurePosixPath(urlsplit(url).path).suffixes]
            suffix = next((s for s in reversed(suffixes) if s in _MEDIA_SUFFIXES), ".ts")
        else:
            suffix = PurePosixPath(urlsplit(url).path).suffix
            suffix = suffix if _SUFFIX.fullmatch(suffix) else ""
        return f"{self.base_url}/h/{self.register(url, kind, stream_id)}{suffix}"

    def target(self, token: str) -> _Target | None:
        with self._lock:
            return self._targets.get(token)

    def upstream_headers(self, target: _Target, stream: Stream) -> dict[str, str]:
        # A source's credentials only go to its own origin; other hosts (CDNs) get the
        # Referer/User-Agent the stream asked for, as the cast relay does.
        same_origin = _origin(target.url) == _origin(stream.url)
        headers = {
            name: value for name, value in (stream.headers or {}).items()
            if same_origin or name.lower() in {"referer", "user-agent"}
        }
        if not any(name.lower() == "user-agent" for name in headers):
            headers["User-Agent"] = f"Cue/{__version__}"
        headers["Accept-Encoding"] = "identity"
        return headers

    def rewrite(self, target: _Target, text: str, base: str) -> str:
        def register(url: str, kind: str, start: float | None) -> str:
            return self.link(url, kind, target.stream_id)

        if hls.is_master(text):
            rewritten = hls.rewrite_master(text, base, register)
            if target.kind == "single":
                return hls.single_variant(rewritten, self._heights.get(target.stream_id))
            return rewritten
        rewritten, total = hls.rewrite_media(text, base, register, track_progress=False)
        if target.kind != "subtitles" and total:
            with self._lock:
                self._durations[target.stream_id] = max(self._durations.get(target.stream_id, 0.0), total)
        return rewritten

    # -- FFmpeg and downloads ------------------------------------------------------------------

    def ffmpeg_command(self, shared: Shared, output: str, container: str) -> list[str]:
        if self.ffmpeg is None:
            raise PlayerError("FFmpeg isn't installed")
        return [
            self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", shared.single, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", container, "-y", output,
        ]

    def save(
        self, stream: Stream, name: str, *, progress: Callable[[str], None] | None = None,
        cancelled: threading.Event | None = None, folder: Path | None = None, quality: str = "best",
        storage: Storage | None = None,
    ) -> Path:
        """Download ``stream`` to an MP4 (or whatever yt-dlp makes) and return its path. Blocks.

        ``quality`` caps the resolution (``"720"``); ``storage`` says how long earlier
        episodes stay in ``folder`` and how much room they may take.
        """
        folder = folder or downloads_dir()
        storage = storage or Storage()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{safe_filename(name)}.mp4"
        partial = target.with_name(target.stem + ".part.mp4")
        tidy(folder, storage, spare={target, partial})
        if target.is_file() and target.stat().st_size > 0:
            return target  # still here from a moment ago
        if self.ffmpeg is not None:
            saved = self._save_with_ffmpeg(stream, target, partial, progress, cancelled, quality)
        else:
            from cue.download import find_ytdlp

            if find_ytdlp() is None:
                raise PlayerError(
                    "To play streams in a player that only opens files, Cue needs FFmpeg (ffmpeg.org) or "
                    "yt-dlp on PATH or next to cue.exe. `cue install --ffmpeg` downloads FFmpeg for you."
                )
            saved = self._save_with_ytdlp(stream, folder, safe_filename(name), progress)
        tidy(folder, storage, spare={saved})
        return saved

    def fetch(self, token: str) -> httpx.Response:
        """Fetch what a relay token stands for, straight from the source, retrying brief failures."""
        target = self.target(token)
        stream = self.stream(target.stream_id) if target else None
        if target is None or stream is None:
            raise PlayerError("The stream's link expired while downloading")
        problem = ""
        for attempt in range(RETRIES):
            try:
                response = self.client.get(target.url, headers=self.upstream_headers(target, stream))
            except httpx.HTTPError as exc:
                problem = type(exc).__name__
            else:
                if response.status_code < 400:
                    return response
                problem = f"HTTP {response.status_code}"
                if response.status_code in {401, 403, 404, 410}:
                    break  # asking again won't help
            time.sleep(0.5 * (attempt + 1))
        raise PlayerError(f"Could not download part of the stream ({problem})")

    def prefetching(self, stream_id: str) -> _Prefetch | None:
        with self._lock:
            return self._prefetches.get(stream_id)

    def _plan(self, shared: Shared, cancelled: threading.Event | None) -> _Prefetch | None:
        """Read the stream's playlists and line up its pieces in the order FFmpeg will want them.

        Returns ``None`` for a source that isn't HLS (a plain video file).
        """
        root_token = _token(shared.single)
        root = self.fetch(root_token)
        if not hls.is_playlist(root.content):
            return None
        top = self.rewrite(self.target(root_token), root.text, str(root.url))  # type: ignore[arg-type]
        playlists = {root_token: top}
        media = [top]
        bandwidth = 0
        if hls.is_master(top):
            bandwidth = _bandwidth(top)
            media = []
            for link in _links(top):
                token = _token(link)
                response = self.fetch(token)
                playlists[token] = self.rewrite(self.target(token), response.text, str(response.url))  # type: ignore[arg-type]
                media.append(playlists[token])
        # FFmpeg reads video and audio side by side, so their pieces are interleaved by time.
        timed = sorted((start, number, token) for number, text in enumerate(media) for start, token in _timed_pieces(text))
        estimate = bandwidth / 8 * self.duration(shared.stream_id)
        return _Prefetch(self, [token for _, _, token in timed], playlists, cancelled, estimate, self._unlock(playlists))

    def _unlock(self, playlists: dict[str, str]) -> dict[str, tuple[bytes, bytes]]:
        """Take AES-128 decryption over from FFmpeg, so it runs on the download threads.

        FFmpeg opens each encrypted piece on a new connection and decrypts it on its own
        thread, which makes it several times slower than reading plain pieces. Playlists
        Cue can decrypt lose their keys here, in place; returns each piece's (key, IV).
        Anything else (SAMPLE-AES, byte ranges, an encrypted init section) stays as it
        is, for FFmpeg to decrypt.
        """
        if not _can_decrypt():
            return {}
        plans = {token: plan for token, text in playlists.items() if (plan := _encryption(text)) and plan[1]}
        key_tokens = {key for _, pieces in plans.values() for key, _ in pieces.values()}
        keys: dict[str, bytes] = {}
        for key_token in key_tokens:
            try:
                key = self.fetch(key_token).content
            except PlayerError:
                return {}  # FFmpeg will ask for the key itself and report the problem
            if len(key) != 16:
                return {}
            keys[key_token] = key
        pieces: dict[str, tuple[bytes, bytes]] = {}
        for token, (text, encrypted) in plans.items():
            playlists[token] = text
            pieces.update({piece: (keys[key], iv) for piece, (key, iv) in encrypted.items()})
        return pieces

    def _save_with_ffmpeg(
        self, stream: Stream, target: Path, partial: Path, progress: Callable[[str], None] | None,
        cancelled: threading.Event | None, quality: str,
    ) -> Path:
        shared = self.share(stream, quality)
        prefetch = self._plan(shared, cancelled)
        if prefetch is not None and prefetch.estimate:
            free = shutil.disk_usage(target.parent).free
            if free < prefetch.estimate * 1.05:
                raise PlayerError(
                    f"Not enough free space in {target.parent}: this episode needs about "
                    f"{_size(prefetch.estimate)} and {_size(free)} is free. Choose a lower quality or another "
                    "folder under [playback] in the settings, or free some space."
                )
        if prefetch is not None:
            with self._lock:
                self._prefetches[shared.stream_id] = prefetch
            prefetch.start()
        process = subprocess.Popen(  # noqa: S603 - FFmpeg reading Cue's own relay
            self.ffmpeg_command(shared, str(partial), "mp4"), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=_NO_WINDOW,
        )
        errors: list[bytes] = []
        reader = threading.Thread(target=lambda: errors.append(process.stderr.read()), daemon=True)  # type: ignore[union-attr]
        reader.start()
        shown = ""
        try:
            while process.poll() is None:
                if cancelled is not None and cancelled.is_set():
                    process.kill()
                    process.wait()
                    partial.unlink(missing_ok=True)
                    raise PlayerError("Download stopped")
                if progress is not None:
                    if prefetch is not None:
                        text = f"{int(prefetch.taken / max(1, prefetch.total) * 99)}%"
                    else:
                        text = f"{partial.stat().st_size / 1e6:.0f} MB" if partial.is_file() else ""
                    if text and text != shown:
                        shown = text
                        progress(text)
                time.sleep(0.2)
            code = process.returncode
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            reader.join(timeout=5)
            if prefetch is not None:
                prefetch.stop()
                with self._lock:
                    self._prefetches.pop(shared.stream_id, None)
        if code != 0 or not partial.is_file():
            partial.unlink(missing_ok=True)
            if prefetch is not None and prefetch.error is not None:
                raise prefetch.error
            lines = b"".join(errors).decode("utf-8", "replace").strip().splitlines()
            raise PlayerError(f"Could not download the stream: {lines[-1] if lines else f'FFmpeg exited with {code}'}")
        partial.replace(target)
        return target

    def _save_with_ytdlp(self, stream: Stream, folder: Path, name: str, progress: Callable[[str], None] | None) -> Path:
        from cue.download import download

        def report(percent: float) -> None:
            if progress is not None:
                progress(f"{percent:.0f}%")

        asyncio.run(download(stream, folder, name, report))
        found = sorted(folder.glob(f"{name}.*"), key=lambda path: path.stat().st_mtime)
        if not found:
            raise PlayerError("yt-dlp finished but saved nothing")
        return found[-1]


class _Prefetch:
    """One download's pieces, fetched ``PARALLEL`` at a time into memory just ahead of FFmpeg.

    FFmpeg asks the relay for pieces one after another, and each is handed over from
    memory and forgotten. At most ``WINDOW`` pieces are held at once, and none is ever
    written to disk: only the finished file is. The playlists FFmpeg reads are the ones
    read here, so the pieces it asks for are exactly the ones fetched.
    """

    def __init__(
        self, relay: LocalRelay, tokens: list[str], playlists: dict[str, str],
        cancelled: threading.Event | None, estimate: float, keys: dict[str, tuple[bytes, bytes]] | None = None,
    ) -> None:
        self._relay = relay
        self._keys = keys or {}  # piece token -> (AES-128 key, IV), for pieces Cue decrypts
        self._order = deque(tokens)
        self._wanted = set(tokens)
        self._playlists = playlists
        self._cancelled = cancelled
        self.estimate = estimate  # bytes, from the playlist's bandwidth; 0 when unknown
        self.total = len(tokens)
        self.taken = 0
        self.error: PlayerError | None = None
        self._ready: dict[str, bytes] = {}
        self._busy: set[str] = set()
        self._done: set[str] = set()
        self._stopped = False
        self._condition = threading.Condition()

    def start(self) -> None:
        for _ in range(PARALLEL):
            threading.Thread(target=self._work, name="cue-download", daemon=True).start()

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._ready.clear()
            self._condition.notify_all()

    def playlist(self, token: str) -> str | None:
        return self._playlists.get(token)

    def _halted(self) -> bool:
        return self._stopped or (self._cancelled is not None and self._cancelled.is_set())

    def _work(self) -> None:
        while True:
            with self._condition:
                while not self._halted() and self._order and len(self._ready) + len(self._busy) >= WINDOW:
                    self._condition.wait(0.5)
                while self._order and self._order[0] in (self._done | self._busy | self._ready.keys()):
                    self._order.popleft()
                if self._halted() or not self._order:
                    return
                token = self._order.popleft()
                self._busy.add(token)
            try:
                data = self._piece(token)
            except PlayerError as exc:
                with self._condition:
                    self._busy.discard(token)
                    self.error = self.error or exc
                    self._condition.notify_all()
                return
            with self._condition:
                self._busy.discard(token)
                if token not in self._done and not self._stopped:
                    self._ready[token] = data
                self._condition.notify_all()

    def take(self, token: str) -> bytes | None:
        """The piece FFmpeg asked for, waiting until it arrives; ``None`` for anything not on the list."""
        with self._condition:
            if token not in self._wanted or token in self._done:
                return None  # a key, or asked for twice: the relay fetches it as usual
            while token not in self._ready:
                if self.error is not None:
                    raise self.error
                if self._halted():
                    raise PlayerError("Download stopped")
                if token not in self._busy:  # wanted before the workers got to it: fetch it right here
                    self._busy.add(token)
                    break
                self._condition.wait(0.5)
            else:
                self._done.add(token)
                self.taken += 1
                self._condition.notify_all()
                return self._ready.pop(token)
        try:
            data = self._piece(token)
        finally:
            with self._condition:
                self._busy.discard(token)
                self._condition.notify_all()
        with self._condition:
            self._done.add(token)
            self.taken += 1
        return data

    def _piece(self, token: str) -> bytes:
        data = self._relay.fetch(token).content
        key = self._keys.get(token)
        return data if key is None else _decrypt(data, *key)


@dataclass(frozen=True)
class Storage:
    """How long temporary episodes stay, and how much room they may take."""

    keep_hours: float = 0.0  # 0: until the next one starts, or Cue closes
    max_mb: int = 4096  # 0: no limit


def tidy(folder: Path, storage: Storage, spare: set[Path] | frozenset[Path] = frozenset()) -> None:
    """Delete temporary episodes past ``storage``: older than its hours, then the oldest past its size.

    A file a player still has open can't be deleted; it stays until the next tidy.
    """
    if not folder.is_dir():
        return
    now = time.time()
    files: list[tuple[float, int, Path]] = []
    for path in folder.iterdir():
        try:
            if path.is_file() and path not in spare:
                info = path.stat()
                files.append((info.st_mtime, info.st_size, path))
        except OSError:
            continue
    files.sort()
    remaining = []
    for modified, size, path in files:
        stale = path.name.endswith(".part.mp4") or now - modified >= storage.keep_hours * 3600
        if not (stale and _delete(path)):
            remaining.append((modified, size, path))
    if storage.max_mb > 0:
        total = sum(size for _, size, _ in remaining) + sum(path.stat().st_size for path in spare if path.is_file())
        for _, size, path in remaining:  # oldest first
            if total <= storage.max_mb * 1_000_000:
                break
            if _delete(path):
                total -= size


def _delete(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False  # a player still has it open


def _token(link: str) -> str:
    match = re.search(r"/h/([0-9a-f]{32})", link)
    if match is None:
        raise PlayerError("Not a relay link")
    return match[1]


def _timed_pieces(text: str) -> list[tuple[float, str]]:
    """(start time, relay token) of each piece in a rewritten media playlist."""
    pieces: list[tuple[float, str]] = []
    elapsed = duration = 0.0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                duration = float(line[len("#EXTINF:"):].split(",", 1)[0])
            except ValueError:
                duration = 0.0
        elif line and not line.startswith("#"):
            with contextlib.suppress(PlayerError):
                pieces.append((elapsed, _token(line)))
            elapsed += duration
            duration = 0.0
    return pieces


def _encryption(text: str) -> tuple[str, dict[str, tuple[str, bytes]]] | None:
    """A rewritten media playlist without its AES-128 keys, and each encrypted piece's (key token, IV).

    ``None`` when the playlist has encryption Cue leaves to FFmpeg: another method
    (SAMPLE-AES, DRM), byte ranges, or an init section under a key.
    """
    output: list[str] = []
    pieces: dict[str, tuple[str, bytes]] = {}
    key: tuple[str, bytes | None] | None = None  # key token and IV (None: the piece's sequence number)
    sequence = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            with contextlib.suppress(ValueError):
                sequence = int(stripped.partition(":")[2])
        elif stripped.startswith("#EXT-X-KEY:"):
            attributes = hls.attributes(stripped)
            method = attributes.get("METHOD", "NONE").upper()
            if method == "NONE":
                key = None
                continue
            if method != "AES-128" or attributes.get("KEYFORMAT", "identity") != "identity":
                return None
            try:
                iv = attributes.get("IV")
                key = (_token(attributes.get("URI", "")), int(iv, 16).to_bytes(16, "big") if iv else None)
            except (PlayerError, ValueError, OverflowError):
                return None
            continue
        elif stripped.startswith("#EXT-X-BYTERANGE") or (stripped.startswith("#EXT-X-MAP:") and key is not None):
            return None
        elif stripped and not stripped.startswith("#"):
            if key is not None:
                try:
                    pieces[_token(stripped)] = (key[0], key[1] or sequence.to_bytes(16, "big"))
                except PlayerError:
                    return None
            sequence += 1
        output.append(stripped)
    return "\n".join(output) + "\n", pieces


def _can_decrypt() -> bool:
    import importlib.util

    return importlib.util.find_spec("cryptography") is not None


def _decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """An HLS AES-128 piece: AES-CBC with PKCS#7 padding."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = decryptor.update(data) + decryptor.finalize()
    except ValueError as exc:
        raise PlayerError(f"Could not decrypt part of the stream ({exc})") from exc
    padding = plain[-1] if plain else 0
    if 1 <= padding <= 16 and plain[-padding:] == bytes([padding]) * padding:
        plain = plain[:-padding]
    return plain


def _bandwidth(master: str) -> int:
    match = re.search(r"AVERAGE-BANDWIDTH=(\d+)", master) or re.search(r"[,:]BANDWIDTH=(\d+)", master)
    return int(match[1]) if match else 0


def _height(quality: str | int | None) -> int | None:
    """``"720"`` or ``"720p"`` -> 720; ``"worst"`` -> 1 (the smallest); ``"best"`` -> no limit."""
    text = str(quality or "").strip().lower().removesuffix("p")
    if text == "worst":
        return 1
    return int(text) if text.isdigit() and int(text) > 0 else None


def _size(count: float) -> str:
    return f"{count / 1e9:.1f} GB" if count >= 1e9 else f"{count / 1e6:.0f} MB"


def _links(text: str) -> list[str]:
    """The URIs in a playlist, in order and once each: entry lines and ``URI="..."`` attributes."""
    found: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        candidates = _URI_ATTRIBUTE.findall(line) if line.startswith("#") else [line] if line else []
        found += [uri for uri in candidates if uri not in found]
    return found


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    relay: LocalRelay

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.debug("Relay connection error", exc_info=True)


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    # Keep connections open between pieces; FFmpeg and players fetch hundreds in a row.
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True  # send each reply at once rather than waiting on the reader's ACK

    def log_message(self, format: str, *args: Any) -> None:  # never print over the TUI
        log.debug("relay %s", format % args)

    def do_GET(self) -> None:
        self._get(head=False)

    def do_HEAD(self) -> None:
        self._get(head=True)

    def _reply(self, status: int, body: str, content_type: str, *, head: bool) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _get(self, *, head: bool) -> None:
        relay = self.server.relay
        path = urlsplit(self.path).path
        try:
            if match := re.fullmatch(r"/h/([0-9a-f]{32})(?:\.[A-Za-z0-9]{1,5})?", path):
                target = relay.target(match[1])
                stream = relay.stream(target.stream_id) if target else None
                if target is None or stream is None:
                    self._reply(404, "This link has expired; play the episode again in Cue.", "text/plain", head=head)
                elif not self._prefetched(match[1], target, head):
                    self._relay(target, stream, head)
            elif match := re.fullmatch(r"/ts/([0-9a-f]{16})", path):
                self._continuous(match[1], head)
            else:
                self._reply(404, "Not found", "text/plain", head=head)
        except (ConnectionError, TimeoutError):
            self.close_connection = True  # the player went away mid-response

    def _prefetched(self, token: str, target: _Target, head: bool) -> bool:
        """Answer from a download in progress: its playlists, and pieces from memory."""
        prefetch = self.server.relay.prefetching(target.stream_id)
        if prefetch is None:
            return False
        text = prefetch.playlist(token)
        if text is not None:
            self._reply(200, text, PLAYLIST_TYPE, head=head)
            return True
        if target.kind != "segment":
            return False
        try:
            data = prefetch.take(token)
        except PlayerError as exc:
            self._reply(502, str(exc), "text/plain", head=head)
            return True
        if data is None:
            return False
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)
        return True

    def _relay(self, target: _Target, stream: Stream, head: bool) -> None:
        relay = self.server.relay
        headers = relay.upstream_headers(target, stream)
        if target.kind in {"root", "single", "segment"} and self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        try:
            response = relay.client.send(relay.client.build_request("GET", target.url, headers=headers), stream=True)
        except httpx.HTTPError as exc:
            self._reply(502, f"The source did not answer ({type(exc).__name__})", "text/plain", head=head)
            return
        try:
            if response.status_code >= 400:
                self._reply(response.status_code, f"The source refused (HTTP {response.status_code})", "text/plain", head=head)
                return
            decoded = target.kind != "segment" or response.is_stream_consumed
            chunks = response.iter_bytes(CHUNK) if decoded else response.iter_raw(CHUNK)
            first = next(chunks, b"")
            if target.kind != "segment" and hls.is_playlist(first):
                body = bytearray(first)
                for chunk in chunks:
                    body.extend(chunk)
                    if len(body) > MAX_PLAYLIST_BYTES:
                        self._reply(502, "The source playlist is too large", "text/plain", head=head)
                        return
                text = relay.rewrite(target, body.decode("utf-8", "replace"), str(response.url))
                self._reply(200, text, PLAYLIST_TYPE, head=head)
                return
            exact = not (decoded and "content-encoding" in response.headers)
            self.send_response(response.status_code)
            self.send_header("Content-Type", response.headers.get("content-type", "application/octet-stream"))
            for name in ("content-length", "content-range", "accept-ranges"):
                if name in response.headers and (exact or name != "content-length"):
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
        finally:
            response.close()

    def _continuous(self, stream_id: str, head: bool) -> None:
        relay = self.server.relay
        stream = relay.stream(stream_id)
        if stream is None:
            self._reply(404, "This link has expired; play the episode again in Cue.", "text/plain", head=head)
            return
        shared = Shared(stream_id, "", relay.link(stream.url, "single", stream_id), f"{relay.base_url}/ts/{stream_id}")
        if relay.ffmpeg is None:
            # Without FFmpeg, a plain video file still plays; an HLS source can't be joined.
            self.send_response(302)
            self.send_header("Location", shared.single)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        if head:
            return
        process = subprocess.Popen(  # noqa: S603 - FFmpeg reading Cue's own relay
            relay.ffmpeg_command(shared, "pipe:1", "mpegts"), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
        assert process.stdout is not None
        try:
            while chunk := process.stdout.read(CHUNK):
                self.wfile.write(chunk)
        finally:
            process.kill()
            process.wait()


# -- choosing what each player gets --------------------------------------------------------


class RelayingPlayer:
    """Hands a player something it can open: the source link, a relay link, or a downloaded file.

    ``headers`` lists the HTTP headers the player can send (lower case; ``None`` means it
    checks for itself), ``hls`` whether it plays HLS links, ``links`` whether it opens web
    links at all. Everything else is passed to the wrapped player.
    """

    def __init__(
        self, player: Player, *, title: str, relay: Callable[[], LocalRelay],
        headers: frozenset[str] | None, hls: bool, links: bool, folder: Path | None = None,
        quality: str = "best", storage: Storage | None = None,
    ) -> None:
        self._player = player
        self._quality = quality
        self._storage = storage or Storage()
        self._file: Path | None = None  # what this player was given to play, when Cue downloaded it
        self._title = title
        self._relay = relay
        self._headers = headers
        self._hls = hls
        self._links = links
        self._folder = folder
        self._messages: list[Callable[[Any], None]] = []
        self._cancelled = threading.Event()

    @property
    def running(self) -> bool:
        return self._player.running

    def observe(self, name: str, callback: Callable[[Any], None]) -> None:
        if name == "message":
            self._messages.append(callback)
        self._player.observe(name, callback)

    def on_close(self, callback: Callable[[], None]) -> None:
        self._player.on_close(callback)

    def can_send(self, headers: Mapping[str, str] | None) -> bool:
        if not headers or self._headers is None:
            return True
        return all(name.lower() in self._headers for name in headers)

    def route(self, url: str, headers: Mapping[str, str] | None) -> str:
        """``direct``, ``hls`` (relay link), ``continuous`` (one stream) or ``file`` (download first)."""
        if is_local_file(url):
            return "direct"
        if not self._links:
            return "file"
        if not self._hls:
            return "continuous"
        return "direct" if self.can_send(headers) else "hls"

    async def launch(
        self, url: str, *, start: float | None = None, headers: Mapping[str, str] | None = None,
        title: str | None = None, extra_args: Sequence[str] = (),
    ) -> None:
        route = self.route(url, headers)
        log.info("Playing in %s: %s", self._title, route)
        if route == "direct":
            await self._player.launch(url, start=start, headers=headers, title=title, extra_args=extra_args)
            return
        stream = Stream(url, dict(headers) if headers else None)
        if route == "file":
            self._file = await self._download(stream, title or "Cue")
            await self._player.launch(str(self._file), start=None, headers=None, title=title, extra_args=extra_args)
            return
        relay = await asyncio.to_thread(self._relay)
        shared = relay.share(stream)
        link = shared.hls if route == "hls" else shared.continuous
        await self._player.launch(link, start=start, headers=None, title=title, extra_args=extra_args)

    async def _download(self, stream: Stream, name: str) -> Path:
        loop = asyncio.get_running_loop()

        def report(text: str) -> None:
            message = f"Downloading for {self._title}… {text}"
            for callback in self._messages:
                loop.call_soon_threadsafe(callback, message)

        report("starting")
        relay = await asyncio.to_thread(self._relay)
        return await asyncio.to_thread(
            relay.save, stream, name, progress=report, cancelled=self._cancelled, folder=self._folder,
            quality=self._quality, storage=self._storage,
        )

    async def command(self, *args: Any, timeout: float = 5.0) -> Any:
        return await self._player.command(*args, timeout=timeout)

    async def quit(self, timeout: float = 3.0) -> None:
        self._cancelled.set()
        await self._player.quit(timeout)
        self._finished()

    def kill(self) -> None:
        self._cancelled.set()
        self._player.kill()
        self._finished()

    def _finished(self) -> None:
        # Kept for no time at all: delete it now, unless the player still has it open
        # (then it goes with the next download, or when Cue closes).
        if self._file is not None and self._storage.keep_hours <= 0:
            _delete(self._file)


def route_player(
    player: Player, *, title: str, headers: frozenset[str] | None, hls: bool, links: bool,
    relay: Callable[[], LocalRelay], folder: Path | None = None, quality: str = "best",
    storage: Storage | None = None,
) -> Player:
    """``player`` itself when it can open any link Cue might give it, else a :class:`RelayingPlayer`."""
    if headers is None and hls and links:
        return player
    return RelayingPlayer(player, title=title, relay=relay, headers=headers, hls=hls, links=links,
                          folder=folder, quality=quality, storage=storage)

