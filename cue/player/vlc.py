"""Control an isolated VLC 3 process through its authenticated loopback API.

Commands follow VideoLAN's share/lua/http/requests/README.txt and
share/lua/intf/modules/httprequests.lua. No system VLC preferences are changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import secrets
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from cue.player.base import PlayerError

log = logging.getLogger(__name__)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class VlcPlayer:
    """One VLC instance. Observations use the names consumed by PlaybackSession."""

    def __init__(
        self, command: str | Sequence[str], *, port: int | None = None,
        password: str | None = None, connect_timeout: float = 15.0,
        poll_interval: float = 0.25, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._command = [command] if isinstance(command, str) else list(command)
        self.port = port if port is not None else _free_port()
        self._password = password or secrets.token_urlsafe(32)
        self._connect_timeout = connect_timeout
        self._poll_interval = poll_interval
        self._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{self.port}",
            auth=httpx.BasicAuth("", self._password), trust_env=False,
            follow_redirects=False, timeout=2.0, transport=transport,
        )
        self._process: asyncio.subprocess.Process | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._observers: dict[str, list[Callable[[Any], None]]] = {}
        self._close_callbacks: list[Callable[[], None]] = []
        self._closed = asyncio.Event()
        self._ready = False
        self._quitting = False
        self._seen_playing = False
        self._last_state = "stopped"
        self._position = 0.0
        self._duration = 0.0
        self._eof = False
        self._pending_end_seek = False
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return bool(self._ready and not self._closed.is_set() and self._process is not None
                    and self._process.returncode is None)

    def observe(self, name: str, callback: Callable[[Any], None]) -> None:
        self._observers.setdefault(name, []).append(callback)

    def on_close(self, callback: Callable[[], None]) -> None:
        self._close_callbacks.append(callback)

    def _emit(self, name: str, value: Any) -> None:
        for callback in self._observers.get(name, []):
            try:
                callback(value)
            except Exception:
                log.exception("VLC observer for %s failed", name)

    def _mark_closed(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        for callback in self._close_callbacks:
            try:
                callback()
            except Exception:
                log.exception("VLC close callback failed")

    def build_args(
        self, url: str, *, start: float | None = None,
        headers: Mapping[str, str] | None = None, title: str | None = None,
        extra_args: Sequence[str] = (),
    ) -> list[str]:
        args = [*self._command, "--fullscreen", "--ignore-config", "--no-media-library",
                "--no-qt-privacy-ask", "--no-metadata-network-access"]
        args.extend("--fullscreen" if value == "--fs" else value for value in extra_args)
        # Final flags keep the control endpoint local and prevent reuse of a
        # VLC instance the user opened separately.
        args.extend([
            "--no-one-instance", "--no-one-instance-when-started-from-file",
            "--no-loop", "--no-repeat", "--no-random", "--no-play-and-exit",
            "--extraintf=http", "--http-host=127.0.0.1", f"--http-port={self.port}",
            f"--http-password={self._password}",
            f"--http-src={Path(__file__).with_name('vlc_http')}",
        ])
        if start is not None:
            if not math.isfinite(start) or start < 0:
                raise PlayerError("VLC resume position must be a nonnegative number")
            args.append(f"--start-time={start:.3f}")
        if title:
            args.append(f"--meta-title={title}")
        supported = {"referer": "http-referrer", "user-agent": "http-user-agent"}
        for name, value in (headers or {}).items():
            option = supported.get(name.lower())
            if option is None:
                raise PlayerError(
                    f"VLC cannot forward the required {name!r} HTTP header. "
                    "This source needs a player with arbitrary HTTP-header support."
                )
            if not isinstance(value, str) or any(char in value for char in "\r\n\0"):
                raise PlayerError("Invalid VLC playback HTTP header")
            args.append(f"--{option}={value}")
        args.extend(["--", url])
        return args

    async def _request(self, command: str | None = None, **params: Any) -> dict[str, Any]:
        if command is not None:
            params["command"] = command
        try:
            async with self._lock:
                response = await self._client.get("/requests/status.json", params=params)
            # Never follow redirects carrying local VLC authentication.
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or "state" not in data:
                raise ValueError("missing VLC state")
            return data
        except (httpx.HTTPError, ValueError) as exc:
            raise PlayerError("VLC's local control interface did not return a valid response") from exc

    async def launch(self, url: str, **options: Any) -> None:
        if self._process is not None or self._quitting:
            raise PlayerError("This VLC player was already launched or closed")
        try:
            args = self.build_args(url, **options)
            kwargs: dict[str, Any] = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self._process = await asyncio.create_subprocess_exec(
                *args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, **kwargs,
            )
            deadline = asyncio.get_running_loop().time() + self._connect_timeout
            while asyncio.get_running_loop().time() < deadline:
                if self._process.returncode is not None:
                    raise PlayerError(f"VLC exited before playback started (code {self._process.returncode})")
                try:
                    state = await self._request()
                except PlayerError:
                    state = {}
                if state.get("state") in {"playing", "paused"} and state.get("currentplid", 0) != -1:
                    self._ready = True
                    self._update_status(state)
                    self._poll_task = asyncio.create_task(self._poll())
                    return
                await asyncio.sleep(min(self._poll_interval, 0.1))
            raise PlayerError("VLC did not start playback or its local control interface in time")
        except BaseException as exc:
            await self.quit()
            if isinstance(exc, OSError):
                raise PlayerError(f"Cannot start VLC: {exc}") from exc
            raise

    def _update_status(self, status: Mapping[str, Any]) -> None:
        state = status.get("state")
        if state in {"playing", "paused"}:
            self._seen_playing = True
            try:
                duration = float(status.get("length", 0))
                position = float(status.get("time", 0))
                fraction = float(status.get("position", 0))
            except (ValueError, TypeError) as exc:
                raise PlayerError("VLC returned invalid playback timing") from exc
            if not all(math.isfinite(value) for value in (duration, position, fraction)):
                raise PlayerError("VLC returned invalid playback timing")
            if duration > 0:
                self._duration = duration
                self._emit("duration", duration)
                # HTTP time is rounded to integer seconds; fraction is precise.
                if 0 < fraction <= 1:
                    position = fraction * duration
            # VLC can report negative time/position during initial preroll.
            # Those are not usable resume positions, but playback is still valid.
            if position >= 0:
                self._position = position
                self._emit("time-pos", position)
        elif state == "stopped" and self._seen_playing and self._last_state in {"playing", "paused"}:
            # VLC 3 does not expose a distinct EOF event. Only a normal stop near
            # a known end counts as completion; closing/crashing never does.
            if (self._last_state == "playing" and self._duration > 0
                    and (self._position >= self._duration - 1.5 or self._pending_end_seek)
                    and not self._eof):
                self._eof = True
                self._emit("eof-reached", True)
            self._mark_closed()
        self._last_state = str(state)

    async def _poll(self) -> None:
        failures = 0
        try:
            while not self._quitting and not self._closed.is_set():
                await asyncio.sleep(self._poll_interval)
                if self._process is not None and self._process.returncode is not None:
                    if self._process.returncode:
                        self._emit("error", f"VLC exited unexpectedly (code {self._process.returncode})")
                    break
                try:
                    self._update_status(await self._request())
                    failures = 0
                except PlayerError:
                    failures += 1
                    if failures >= 3:
                        self._emit("error", "Lost contact with VLC's local control interface")
                        break
        except asyncio.CancelledError:
            raise
        finally:
            self._mark_closed()

    async def command(self, *args: Any, timeout: float = 5.0) -> Any:
        if not self.running:
            raise PlayerError("VLC is not running")
        if not args:
            raise PlayerError("Missing VLC command")
        name = args[0]
        seek_target: float | None = None
        if name == "quit":
            await self.quit()
            return None
        if name == "show-text":
            return None  # PlaybackSession already shows this in Cue's status.
        if name == "seek":
            seconds = float(args[1])
            mode = args[2] if len(args) > 2 else "absolute"
            if not math.isfinite(seconds) or mode not in {"absolute", "relative"}:
                raise PlayerError("Unsupported VLC seek")
            # VLC 3's Lua common.parsetime accepts integer seconds, not decimal
            # seconds ("80.000" would incorrectly seek to zero). Round forward
            # for absolute skip endpoints so playback clears the interval.
            value = f"{round(seconds):+d}" if mode == "relative" else str(math.ceil(max(0, seconds)))
            seek_target = self._position + int(value) if mode == "relative" else int(value)
            request = self._request("seek", val=value)
        elif name == "cycle" and args[1:] == ("sub",):
            request = self._request("key", val="subtitle-track")
        elif name == "sub-add":
            path = Path(str(args[1])).expanduser().resolve()
            select = len(args) < 3 or args[2] == "select"
            request = self._request("cue_addsubtitle", val=path.as_uri(), select="true" if select else "false")
        elif name == "get_property":
            state = await self._request()
            key = {"time-pos": "time", "duration": "length", "pause": "state"}.get(args[1])
            if key is None:
                raise PlayerError(f"Unsupported VLC property: {args[1]}")
            return state.get(key) == "paused" if key == "state" else state.get(key)
        else:
            raise PlayerError(f"Unsupported VLC command: {name}")
        try:
            state = await asyncio.wait_for(request, timeout)
        except TimeoutError as exc:
            raise PlayerError(f"VLC did not answer {name}") from exc
        if seek_target is not None:
            self._pending_end_seek = self._duration > 0 and seek_target >= self._duration
            self._update_status(state)
        return None

    async def seek(self, seconds: float, mode: str = "absolute") -> None:
        await self.command("seek", seconds, mode)

    async def add_subtitle(self, path: str, *, select: bool = True) -> None:
        await self.command("sub-add", path, "select" if select else "auto")

    async def cycle_subtitles(self) -> None:
        await self.command("cycle", "sub")

    async def show_text(self, text: str, duration_ms: int = 2000) -> None:
        await self.command("show-text", text, duration_ms)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def quit(self, timeout: float = 3.0) -> None:
        self._quitting = True
        if self._poll_task is not None and self._poll_task is not asyncio.current_task():
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        if self._process is not None and self._process.returncode is None:
            with contextlib.suppress(PlayerError):
                await self._request("key", val="quit")
            try:
                await asyncio.wait_for(self._process.wait(), timeout)
            except TimeoutError:
                self.kill()
                await self._process.wait()
        await self._client.aclose()
        self._mark_closed()

    def kill(self) -> None:
        if self._process is not None and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
