"""The JSON API and web app behind ``cue serve``.

Everything runs on one :class:`~cue.core.Core`. Browsers watch through the cast
server's relay (``/play/<cast>/<episode>`` on ``[cast] port``), the same links TVs
play, so a source's headers, subtitles, resume points and watched markers work alike
on a phone and on a TV. The API lives under ``/api/``; ``/`` is the web app in
``static/``.

There is no login yet. The server answers only devices on private networks, by
addresses and host names a local network uses, and it offers nothing that changes
settings or installs plugins. Requests that do something (``POST``) must be JSON from
the web app's own page, which a page on another site can't send.

The API is versioned by path only when it has to change: new fields may appear in any
answer, so clients ignore what they don't know.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import ipaddress
import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web
from rapidfuzz import fuzz, utils

from cue import __version__
from cue.cast import CastServer, devices
from cue.core import Core
from cue.groups import Group
from cue.providers.base import Episode, ProviderError, Show
from cue.skip import KIND_NAMES
from cue.store import HistoryEntry

log = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
KEEP_SHOWS = 2000  # shows remembered from searches, so episodes come from the provider's own record
EPISODES_FOR = 10 * 60.0  # how long a show's episode list is reused when playing or casting
PAIRING_SECONDS = 5 * 60.0  # how long a TV's pairing code is waited for
KEEPALIVE_SECONDS = 25.0
VLC = "vlc"  # the cast target that means VLC browsing Cue's media server (Xbox), not a TV Cue drives
# Host names a home network uses; others must be listed in [web] hosts (guards against DNS rebinding).
_LOCAL_NAMES = re.compile(r"[a-z0-9-]+|.+\.(local|lan|home|home\.arpa|internal|localdomain|localhost)", re.I)


# -- events -----------------------------------------------------------------------------------


class Events:
    """Notices, status lines and cast updates for every open page, as server-sent events."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[tuple[str, Any] | None]] = set()
        self.status_text = ""

    def notice(self, message: str, *, title: str = "", severity: str = "information",
               timeout: float | None = None) -> None:
        log.log(logging.WARNING if severity == "error" else logging.INFO, "%s%s", f"{title}: " if title else "", message)
        self.publish("notice", {"message": message, "title": title, "severity": severity, "timeout": timeout})

    def status(self, text: str) -> None:
        self.status_text = text
        self.publish("status", {"text": text})

    def publish(self, kind: str, data: Any) -> None:
        for queue in list(self._queues):
            if queue.qsize() < 200:  # a page that stopped reading misses events instead of piling them up
                queue.put_nowait((kind, data))

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[tuple[str, Any] | None]]:
        queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()
        self._queues.add(queue)
        try:
            yield queue
        finally:
            self._queues.discard(queue)

    def close(self) -> None:
        """End every event stream, so the server can stop."""
        for queue in list(self._queues):
            queue.put_nowait(None)


# -- what the API sends ---------------------------------------------------------------------------


def show_json(provider: str, show: Show) -> dict[str, Any]:
    return {
        "provider": provider, "id": show.id, "title": show.title, "year": show.year,
        "episode_count": show.episode_count, "media_type": show.media_type,
    }


def episode_json(episode: Episode) -> dict[str, Any]:
    return {"number": episode.number, "label": episode.label, "title": episode.title, "season": episode.season}


def group_json(group: Group) -> dict[str, Any]:
    return {"name": group.name, "title": group.title, "providers": list(group.providers)}


def history_json(entry: HistoryEntry, available: bool) -> dict[str, Any]:
    return {
        "provider": entry.provider, "show": entry.show_id, "title": entry.title,
        "episode": entry.last_episode, "label": Episode(entry.show_id, entry.last_episode).label,
        "position": entry.position, "updated_at": entry.updated_at, "available": available,
    }


def device_json(device: devices.Device) -> dict[str, Any]:
    return {
        "id": device.identifier, "name": device.name, "kind": device.kind, "kind_label": device.kind_label,
        "model": device.model, "needs_pairing": device.needs_pairing,
    }


def _error(status: type[web.HTTPException], message: str) -> web.HTTPException:
    return status(text=json.dumps({"error": message}), content_type="application/json")


def is_local_address(address: str | None) -> bool:
    """Whether a client is on this network: a private, loopback or link-local address."""
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


def is_local_host(host: str, allowed: list[str]) -> bool:
    """Whether a Host header names this server the way a home network does (or as [web] hosts allows)."""
    name = host.strip().lower()
    if name.startswith("["):  # [IPv6]:port
        name = name[1:].partition("]")[0]
    elif name.count(":") == 1:
        name = name.partition(":")[0]
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name in allowed or bool(_LOCAL_NAMES.fullmatch(name))


# -- remembered shows ------------------------------------------------------------------------------


class Library:
    """The shows and episode lists providers returned, so later requests act on the providers' own objects."""

    def __init__(self) -> None:
        self._shows: OrderedDict[tuple[str, str], Show] = OrderedDict()
        self._episodes: dict[tuple[str, str], tuple[float, list[Episode]]] = {}

    def remember(self, provider: str, show: Show) -> None:
        key = (provider, show.id)
        self._shows[key] = show
        self._shows.move_to_end(key)
        while len(self._shows) > KEEP_SHOWS:
            old, _ = self._shows.popitem(last=False)
            self._episodes.pop(old, None)

    def show(self, provider: str, show_id: str, title: str = "") -> Show:
        """A show found earlier, or one made from its ID and title (as Continue Watching does)."""
        found = self._shows.get((provider, show_id))
        if found is None:
            found = Show(id=show_id, title=title or show_id)
            self.remember(provider, found)
        return found

    def episode(self, provider: str, show_id: str, number: float) -> Episode | None:
        """An episode from the show's last episode list, if Cue has one."""
        cached = self._episodes.get((provider, show_id))
        return next((e for e in cached[1] if e.number == number), None) if cached else None

    async def episodes(self, core: Core, provider: str, show: Show, *, fresh: bool = False) -> list[Episode]:
        key = (provider, show.id)
        cached = self._episodes.get(key)
        if cached is not None and not fresh and time.monotonic() - cached[0] < EPISODES_FOR:
            return cached[1]
        episodes = await core.providers[provider].episodes(show)
        self._episodes[key] = (time.monotonic(), episodes)
        return episodes


# -- casting to TVs --------------------------------------------------------------------------------


@dataclass
class CastJob:
    """A queue playing on a TV Cue drives: a DLNA TV, AirPlay or Chromecast."""

    id: str
    device: devices.Device
    provider: str
    audio: str | None
    show: Show
    queue: list[Episode]
    index: int = 0  # the queue item playing (or about to)
    state: str = "connecting"  # connecting, pairing, playing, finished, stopped or failed
    title: str = ""
    message: str = ""
    started: float = field(default_factory=time.time)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    code: asyncio.Future[str | None] | None = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        return self.state in {"connecting", "pairing", "playing"}

    def json(self) -> dict[str, Any]:
        episode = self.queue[self.index] if 0 <= self.index < len(self.queue) else None
        return {
            "id": self.id, "device": device_json(self.device), "provider": self.provider, "audio": self.audio,
            "show": show_json(self.provider, self.show), "state": self.state, "active": self.active,
            "title": self.title, "message": self.message, "index": self.index, "total": len(self.queue),
            "episode": episode_json(episode) if episode else None,
            "next": episode_json(self.queue[self.index + 1]) if self.index + 1 < len(self.queue) else None,
        }


# -- the web app ----------------------------------------------------------------------------------


class Web:
    def __init__(self, core: Core, events: Events, *, hosts: list[str] | None = None, cast_problem: str = "") -> None:
        self.core = core
        self.events = events
        self.library = Library()
        self.hosts = [name.lower() for name in (hosts if hosts is not None else core.config.web.hosts)]
        self.cast_problem = cast_problem
        self.devices: dict[str, devices.Device] = {}
        self.jobs: dict[str, CastJob] = {}
        self._scan: asyncio.Task[list[devices.Device]] | None = None

    # -- helpers ----------------------------------------------------------------------------

    def _provider_name(self, name: str | None) -> str:
        if not name or name not in self.core.providers:
            raise _error(web.HTTPNotFound, f"The {name or 'requested'} provider is not set up on this server.")
        return name

    def _cast_server(self) -> CastServer:
        server = self.core.cast_server
        if server is None:
            raise _error(web.HTTPServiceUnavailable, self.cast_problem or "The cast server isn't running.")
        return server

    def _audio(self, provider: str, value: Any) -> str | None:
        modes = self.core.audio_modes(provider)
        if not modes:
            return None
        return value if value in modes else self.core.default_audio_mode(provider)

    @staticmethod
    async def _body(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _error(web.HTTPBadRequest, "The request isn't valid JSON.") from exc
        if not isinstance(body, dict):
            raise _error(web.HTTPBadRequest, "The request must be a JSON object.")
        return body

    @staticmethod
    def _number(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise _error(web.HTTPBadRequest, "An episode number is needed.") from exc
        if number != number or abs(number) > 1e9:  # NaN or nonsense
            raise _error(web.HTTPBadRequest, "That isn't an episode number.")
        return number

    def _title_for(self, provider: str, show_id: str, title: str) -> str:
        if title:
            return title
        entry = self.core.store.get_history(provider, show_id)
        return entry.title if entry else show_id

    async def _queue(self, provider: str, show: Show, number: float) -> tuple[list[Episode], int]:
        """A show's episodes and where ``number`` is among them."""
        try:
            episodes = await self.library.episodes(self.core, provider, show)
        except ProviderError as exc:
            raise _error(web.HTTPBadGateway, str(exc)) from exc
        index = next((n for n, episode in enumerate(episodes) if episode.number == number), None)
        if index is None:
            # The list may have changed (a new episode came out): look once more.
            try:
                episodes = await self.library.episodes(self.core, provider, show, fresh=True)
            except ProviderError as exc:
                raise _error(web.HTTPBadGateway, str(exc)) from exc
            index = next((n for n, episode in enumerate(episodes) if episode.number == number), None)
        if index is None:
            raise _error(web.HTTPNotFound, f"{show.title} has no episode {Episode(show.id, number).label}.")
        return episodes, index

    def _choice(self, body: dict[str, Any]) -> tuple[str, Show, float, str | None]:
        provider = self._provider_name(body.get("provider"))
        show_id = body.get("show")
        if not isinstance(show_id, str) or not show_id:
            raise _error(web.HTTPBadRequest, "A show is needed.")
        title = body.get("title") if isinstance(body.get("title"), str) else ""
        show = self.library.show(provider, show_id, self._title_for(provider, show_id, title))
        return provider, show, self._number(body.get("number")), self._audio(provider, body.get("audio"))

    # -- information ----------------------------------------------------------------------------

    async def info(self, request: web.Request) -> web.Response:
        core = self.core
        server = core.cast_server
        group = core.default_group()
        return web.json_response({
            "name": core.config.cast.name, "version": __version__,
            "groups": [group_json(group) for group in core.groups],
            "default_group": group.name if group else None,
            "providers": [
                {"name": name, "title": str(getattr(provider, "title", "") or name)}
                for name, provider in core.providers.items()
            ],
            "problems": dict(core.provider_problems),
            "auto_skip": core.config.auto_skip, "auto_next": core.config.auto_next,
            "cast": {
                "available": server is not None, "name": core.config.cast.name,
                "port": server.port if server else core.config.cast.port,
                "url": server.base_url if server else "", "problem": "" if server else self.cast_problem,
            },
            "status": self.events.status_text,
        })

    async def continue_watching(self, request: web.Request) -> web.Response:
        entries = self.core.store.continue_watching()
        return web.json_response({
            "entries": [history_json(entry, entry.provider in self.core.providers) for entry in entries],
        })

    # -- searching ------------------------------------------------------------------------------

    async def search(self, request: web.Request) -> web.StreamResponse:
        """Each provider's results as one line of JSON as soon as it answers, then ``{"type": "done"}``."""
        query = request.query.get("q", "").strip()
        if not query:
            raise _error(web.HTTPBadRequest, "Type something to search for.")
        name = request.query.get("group", "")
        group = self.core.find_group(name) if name else self.core.default_group()
        if group is None:
            raise _error(web.HTTPNotFound, "No providers are set up." if not name else f"There's no {name} group.")
        names = [provider for provider in group.providers if provider in self.core.providers]
        response = web.StreamResponse(headers={
            "Content-Type": "application/x-ndjson; charset=utf-8", "Cache-Control": "no-cache",
        })
        await response.prepare(request)

        async def send(line: dict[str, Any]) -> None:
            await response.write(json.dumps(line).encode() + b"\n")

        await send({"type": "start", "group": group_json(group), "providers": names})
        tasks = [asyncio.create_task(self._ask(provider, group, query)) for provider in names]
        try:
            for finished in asyncio.as_completed(tasks):
                await send(await finished)
            await send({"type": "done"})
        except ConnectionResetError:
            pass  # the page went away
        finally:
            for task in tasks:
                task.cancel()
        return response

    async def _ask(self, name: str, group: Group, query: str) -> dict[str, Any]:
        provider = self.core.providers[name]
        try:
            shows = group.shows(provider, await provider.search(query))
        except ProviderError as exc:
            return {"type": "results", "provider": name, "shows": [], "error": str(exc)}
        except Exception as exc:  # a plugin's bug shouldn't hide the other providers' results
            log.exception("Search failed in %s", name)
            return {"type": "results", "provider": name, "shows": [], "error": f"{type(exc).__name__}: {exc}"}
        found = []
        for show in shows:
            self.library.remember(name, show)
            found.append({**show_json(name, show),
                          "score": fuzz.WRatio(query, show.title, processor=utils.default_process)})
        return {"type": "results", "provider": name, "shows": found, "error": None}

    # -- episodes -------------------------------------------------------------------------------

    async def episodes(self, request: web.Request) -> web.Response:
        provider = self._provider_name(request.query.get("provider"))
        show_id = request.query.get("show", "")
        if not show_id:
            raise _error(web.HTTPBadRequest, "A show is needed.")
        show = self.library.show(provider, show_id, self._title_for(provider, show_id, request.query.get("title", "")))
        try:
            episodes = await self.library.episodes(self.core, provider, show, fresh=True)
        except ProviderError as exc:
            raise _error(web.HTTPBadGateway, str(exc)) from exc
        except Exception as exc:
            log.exception("Episodes failed in %s", provider)
            raise _error(web.HTTPBadGateway, f"{type(exc).__name__}: {exc}") from exc
        store = self.core.store
        watched = store.watched(provider, show.id)
        entry = store.get_history(provider, show.id)
        in_progress = entry.last_episode if entry and entry.position > 0 else None
        modes = self.core.audio_modes(provider)
        return web.json_response({
            "show": show_json(provider, show),
            "audio_modes": list(modes), "audio": self.core.default_audio_mode(provider),
            "episodes": [
                {**episode_json(episode), "watched": episode.number in watched,
                 "position": entry.position if entry and episode.number == in_progress else 0.0}
                for episode in episodes
            ],
            "resume": self._resume(entry, episodes),
        })

    @staticmethod
    def _resume(entry: HistoryEntry | None, episodes: list[Episode]) -> dict[str, Any] | None:
        """What Continue plays, as the terminal app does: the unfinished episode, else the next one."""
        if not episodes:
            return None
        numbers = [episode.number for episode in episodes]
        if entry is None:
            return {"number": numbers[0], "label": episodes[0].label, "position": 0.0, "caught_up": False}
        if entry.position > 0 and entry.last_episode in numbers:
            episode = episodes[numbers.index(entry.last_episode)]
            return {"number": episode.number, "label": episode.label, "position": entry.position, "caught_up": False}
        later = [episode for episode in episodes if episode.number > entry.last_episode]
        if not later:
            return {"number": numbers[-1], "label": episodes[-1].label, "position": 0.0, "caught_up": True}
        return {"number": later[0].number, "label": later[0].label, "position": 0.0, "caught_up": False}

    # -- watching in the browser ----------------------------------------------------------------

    async def play(self, request: web.Request) -> web.Response:
        """Put a show's episodes on the relay; the page plays ``items[index]`` and moves on from there."""
        provider, show, number, audio = self._choice(await self._body(request))
        server = self._cast_server()
        episodes, index = await self._queue(provider, show, number)
        bound = self.core.playback_provider(provider, audio)
        generation, _ = server.publish_queue(provider, bound, show, episodes)
        cast = server.cast(generation)
        resume = cast.resume if cast else {}
        watched = cast.watched if cast else set()
        return web.json_response({
            "cast": generation, "index": index, "provider": provider, "audio": audio,
            "show": show_json(provider, show),
            "relay": {"port": server.port, "url": server.base_url},
            "skip": getattr(bound, "skip_supported", True) is not False,
            "items": [
                {**episode_json(episode), "index": n, "path": f"/play/{generation}/{n}",
                 "resume": resume.get(n, 0.0), "watched": episode.number in watched}
                for n, episode in enumerate(episodes)
            ],
        })

    async def skip(self, request: web.Request) -> web.Response:
        """An episode's opening and ending (AniSkip), for auto-skip in the browser."""
        provider = self._provider_name(request.query.get("provider"))
        show_id = request.query.get("show", "")
        if not show_id:
            raise _error(web.HTTPBadRequest, "A show is needed.")
        show = self.library.show(provider, show_id, self._title_for(provider, show_id, request.query.get("title", "")))
        number = self._number(request.query.get("number"))
        if getattr(self.core.providers[provider], "skip_supported", True) is False:
            return web.json_response({"summary": "no skip data for this provider", "intervals": []})
        episode = self.library.episode(provider, show.id, number)
        try:
            info = await self.core.skip.lookup(provider, show, episode or Episode(show.id, number))
        except Exception as exc:  # skipping is a bonus; never let it break playback
            log.warning("Skip lookup failed: %s", exc)
            return web.json_response({"summary": "no skip data (lookup failed)", "intervals": []})
        return web.json_response({
            "summary": info.summary,
            "intervals": [
                {"kind": interval.kind, "name": KIND_NAMES.get(interval.kind, interval.kind),
                 "start": interval.start, "end": interval.end}
                for interval in info.intervals
            ],
        })

    # -- casting to a TV ------------------------------------------------------------------------

    async def find_devices(self, request: web.Request) -> web.Response:
        """Look for TVs (a few seconds); ``?cached=1`` answers with the last search instead."""
        server = self.core.cast_server
        if not request.query.get("cached") or not self.devices:
            if self._scan is None or self._scan.done():
                self._scan = asyncio.create_task(devices.discover(self.core.airplay_credentials()))
            found = await asyncio.shield(self._scan)
            self.devices = {device.identifier: device for device in found}
        return web.json_response({
            "server": {"name": self.core.config.cast.name, "available": server is not None,
                       "problem": "" if server else self.cast_problem},
            "vlc": {"id": VLC, "name": "VLC on the TV", "kind": VLC, "kind_label": "DLNA",
                    "model": "", "needs_pairing": False},
            "devices": [device_json(device) for device in self.devices.values()],
            "last": self.core.last_tv,
        })

    def _jobs_json(self) -> list[dict[str, Any]]:
        return [job.json() for job in sorted(self.jobs.values(), key=lambda job: -job.started)]

    def _changed(self) -> None:
        self.events.publish("jobs", {"jobs": self._jobs_json()})

    async def jobs_list(self, request: web.Request) -> web.Response:
        return web.json_response({"jobs": self._jobs_json()})

    async def cast(self, request: web.Request) -> web.Response:
        """Play a show's episodes on a TV, from the chosen one on; or publish them for VLC on the TV."""
        body = await self._body(request)
        provider, show, number, audio = self._choice(body)
        server = self._cast_server()
        target = body.get("device")
        episodes, index = await self._queue(provider, show, number)
        bound = self.core.playback_provider(provider, audio)
        queue = episodes[index:]
        if target == VLC:
            url = server.publish(provider, bound, show, queue)
            return web.json_response({
                "vlc": True, "url": url,
                "message": f"On the TV, open VLC → Network → {server.name} → {show.title}.",
            })
        device = self.devices.get(target) if isinstance(target, str) else None
        if device is None:
            raise _error(web.HTTPNotFound, "That TV isn't in the last search; look for TVs again.")
        return web.json_response(self._start_job(device, provider, audio, show, queue, 0).json())

    def _start_job(self, device: devices.Device, provider: str, audio: str | None, show: Show,
                   queue: list[Episode], start: int) -> CastJob:
        for job in list(self.jobs.values()):
            if job.device.identifier == device.identifier and job.active:
                self._stop(job)  # one queue per TV: the new one replaces it
        for job in [job for job in self.jobs.values() if not job.active][:-4]:
            del self.jobs[job.id]  # keep a few finished ones to show what happened
        self.core.last_tv = device.identifier
        job = CastJob(secrets.token_hex(6), device, provider, audio, show, queue, start)
        job.task = asyncio.create_task(self._run_job(job))
        self.jobs[job.id] = job
        self._changed()
        return job

    async def _run_job(self, job: CastJob) -> None:
        loop = asyncio.get_running_loop()

        async def ask_code() -> str | None:
            job.state, job.code = "pairing", loop.create_future()
            job.message = f"Type the code {job.device.name} shows."
            self._changed()
            try:
                return await asyncio.wait_for(job.code, PAIRING_SECONDS)
            except TimeoutError:
                return None
            finally:
                job.code, job.message = None, ""
                if job.state == "pairing":
                    job.state = "connecting"
                self._changed()

        def playing(index: int, media: devices.Media) -> None:
            job.index, job.title, job.state = index, media.title, "playing"
            if job.device.needs_pairing:  # paired now: it won't ask again
                job.device = dataclasses.replace(job.device, needs_pairing=False)
                self.devices[job.device.identifier] = job.device
            self._changed()

        try:
            server = self._cast_server()
            bound = self.core.playback_provider(job.provider, job.audio)
            problem = await self.core.cast_to_tv(
                server, job.device, job.provider, bound, job.show, job.queue,
                ask_code=ask_code, start=job.index, playing=playing,
            )
        except asyncio.CancelledError:
            job.state = "stopped"
            self._changed()
            raise
        except web.HTTPException as exc:
            job.state, job.message = "failed", json.loads(exc.text or "{}").get("error", "Casting failed")
        except Exception as exc:
            log.exception("Casting to %s failed", job.device.name)
            job.state, job.message = "failed", f"{type(exc).__name__}: {exc}"
        else:
            job.state, job.message = ("failed", problem) if problem else ("finished", "")
        self._changed()

    def _job(self, request: web.Request) -> CastJob:
        job = self.jobs.get(request.match_info["job"])
        if job is None:
            raise _error(web.HTTPNotFound, "That cast has ended.")
        return job

    def _stop(self, job: CastJob) -> None:
        if job.code is not None and not job.code.done():
            job.code.set_result(None)
        if job.task is not None and not job.task.done():
            job.task.cancel()
        job.state = "stopped"

    async def stop_job(self, request: web.Request) -> web.Response:
        job = self._job(request)
        self._stop(job)
        self._changed()
        return web.json_response(job.json())

    async def next_job(self, request: web.Request) -> web.Response:
        """Skip to the queue's next episode on the same TV."""
        job = self._job(request)
        if job.index + 1 >= len(job.queue):
            raise _error(web.HTTPConflict, "That was the last episode in the queue.")
        return web.json_response(
            self._start_job(job.device, job.provider, job.audio, job.show, job.queue, job.index + 1).json()
        )

    async def pairing_code(self, request: web.Request) -> web.Response:
        job = self._job(request)
        body = await self._body(request)
        code = body.get("code")
        if job.code is None or job.code.done():
            raise _error(web.HTTPConflict, f"{job.device.name} isn't waiting for a code.")
        job.code.set_result(str(code).replace(" ", "") if isinstance(code, (str, int)) and str(code).strip() else None)
        return web.json_response(job.json())

    # -- events --------------------------------------------------------------------------------

    async def event_stream(self, request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })
        await response.prepare(request)

        async def send(kind: str, data: Any) -> None:
            await response.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())

        try:
            with self.events.subscribe() as queue:
                await send("jobs", {"jobs": self._jobs_json()})
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                    except TimeoutError:
                        await response.write(b": still here\n\n")
                        continue
                    if item is None:
                        break
                    await send(*item)
        except ConnectionResetError:
            pass
        return response

    # -- pages ---------------------------------------------------------------------------------

    async def index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def shutdown(self, app: web.Application) -> None:
        self.events.close()
        for job in self.jobs.values():
            self._stop(job)
        tasks = [job.task for job in self.jobs.values() if job.task is not None]
        if tasks:
            await asyncio.wait(tasks, timeout=6)  # a DLNA TV is told to stop


STATE_KEY = web.AppKey("cue.web", Web)


@web.middleware
async def guard(request: web.Request, handler: Any) -> web.StreamResponse:
    """Only this network, only by local names, and only the web app's own page may act."""
    app_state = request.app[STATE_KEY]
    if not is_local_address(request.remote):
        raise web.HTTPForbidden(text="Cue's web interface only answers devices on your own network.\n")
    if not is_local_host(request.host, app_state.hosts):
        name = request.host.rpartition(":")[0] if request.host.count(":") == 1 else request.host
        raise web.HTTPForbidden(
            text=f"Cue doesn't answer to {name!r}. Open it by its address, or add the name to [web] hosts in the config.\n"
        )
    if request.method not in {"GET", "HEAD"}:
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != request.host.lower():
            raise web.HTTPForbidden(text="Requests must come from Cue's own page.\n")
        if request.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
            raise web.HTTPForbidden(text="Requests must come from Cue's own page.\n")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Send JSON.\n")
    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")  # a new Cue's page is used right away
    if request.path == "/" or request.path.endswith(".html"):
        # Video comes from the cast server's relay, on another port: media and data from anywhere
        # on the network, but scripts and styles only from this server.
        response.headers.setdefault("Content-Security-Policy", (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "media-src * blob: data:; connect-src *; worker-src 'self' blob:; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        ))
    return response


def create_app(core: Core, events: Events | None = None, *, hosts: list[str] | None = None,
               cast_problem: str = "") -> web.Application:
    """The web app on ``core``; the cast server, if one is wanted, is the core's to start."""
    state = Web(core, events or Events(), hosts=hosts, cast_problem=cast_problem)
    app = web.Application(middlewares=[guard], client_max_size=64 * 1024)
    app[STATE_KEY] = state
    app.router.add_get("/", state.index)
    app.router.add_get("/api/info", state.info)
    app.router.add_get("/api/continue", state.continue_watching)
    app.router.add_get("/api/search", state.search)
    app.router.add_get("/api/episodes", state.episodes)
    app.router.add_post("/api/play", state.play)
    app.router.add_get("/api/skip", state.skip)
    app.router.add_get("/api/devices", state.find_devices)
    app.router.add_get("/api/cast", state.jobs_list)
    app.router.add_post("/api/cast", state.cast)
    app.router.add_post("/api/cast/{job}/stop", state.stop_job)
    app.router.add_post("/api/cast/{job}/next", state.next_job)
    app.router.add_post("/api/cast/{job}/code", state.pairing_code)
    app.router.add_get("/api/events", state.event_stream)
    app.router.add_static("/static/", STATIC, show_index=False, follow_symlinks=False)
    app.on_shutdown.append(state.shutdown)
    return app

