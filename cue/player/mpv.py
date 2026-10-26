"""Launch mpv and control it over JSON IPC.

mpv is started as ``mpv --input-ipc-server=\\\\.\\pipe\\cue-<pid> --force-window=immediate <url>``
and driven through a small asyncio client on that named pipe (a Unix socket off
Windows), so it shares Textual's event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from subprocess import DEVNULL
from typing import Any

from cue.player.base import PlayerError

log = logging.getLogger(__name__)

_LIMIT = 1 << 20  # longest IPC line we accept


class MpvError(PlayerError):
    """mpv could not be started, or stopped answering."""


def ipc_address() -> str:
    name = f"cue-{os.getpid()}"
    if sys.platform == "win32":
        return rf"\\.\pipe\{name}"
    return os.path.join(tempfile.gettempdir(), f"{name}.sock")


async def open_ipc(address: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if sys.platform != "win32":
        return await asyncio.open_unix_connection(address, limit=_LIMIT)
    loop = asyncio.get_running_loop()
    if not hasattr(loop, "create_pipe_connection"):
        raise MpvError("mpv control needs the Windows proactor event loop")
    reader = asyncio.StreamReader(limit=_LIMIT, loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    transport, _ = await loop.create_pipe_connection(lambda: protocol, address)
    return reader, asyncio.StreamWriter(transport, protocol, reader, loop)


class MpvPlayer:
    """One mpv process and its IPC connection.

    Register :meth:`observe` and :meth:`on_close` callbacks before :meth:`launch`.
    """

    def __init__(
        self, command: str | Sequence[str], *, address: str | None = None, connect_timeout: float = 10.0
    ) -> None:
        self._command = [command] if isinstance(command, str) else list(command)
        self.address = address or ipc_address()
        self._connect_timeout = connect_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._ids = itertools.count(1)
        self._observers: dict[str, list[Callable[[Any], None]]] = {}
        self._close_callbacks: list[Callable[[], None]] = []
        self._closed = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._writer is not None and not self._closed.is_set()

    def observe(self, name: str, callback: Callable[[Any], None]) -> None:
        self._observers.setdefault(name, []).append(callback)

    def on_close(self, callback: Callable[[], None]) -> None:
        self._close_callbacks.append(callback)

    def build_args(
        self,
        url: str,
        *,
        start: float | None = None,
        headers: Mapping[str, str] | None = None,
        title: str | None = None,
        extra_args: Sequence[str] = (),
    ) -> list[str]:
        args = [
            *self._command,
            f"--input-ipc-server={self.address}",
            "--force-window=immediate",
            "--keep-open=yes",  # so eof-reached fires instead of mpv exiting
            "--no-terminal",
            "--fs",
        ]
        if start:
            args.append(f"--start={start:.2f}")
        if title:
            args.append(f"--force-media-title={title}")
        for name, value in (headers or {}).items():
            args.append(f"--http-header-fields-append={name}: {value}")
        args.extend(extra_args)
        args += ["--", url]
        return args

    async def launch(self, url: str, **options: Any) -> None:
        if self._process is not None:
            raise MpvError("this player was already launched")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.build_args(url, **options), stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL
            )
        except OSError as exc:
            raise MpvError(f"Cannot start mpv: {exc}") from exc
        reader = await self._connect()
        self._reader_task = asyncio.create_task(self._read_loop(reader))
        for index, name in enumerate(self._observers, start=1):
            if name == "error":
                continue  # Cue's adapter-level error notification, not an mpv property.
            await self.command("observe_property", index, name)

    async def _connect(self) -> asyncio.StreamReader:
        assert self._process is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._connect_timeout
        while True:
            try:
                reader, self._writer = await open_ipc(self.address)
                return reader
            except OSError as exc:
                if self._process.returncode is not None:
                    raise MpvError(f"mpv exited (code {self._process.returncode}) before it was ready") from exc
                if loop.time() > deadline:
                    self.kill()
                    raise MpvError("mpv did not open its IPC pipe in time") from exc
                await asyncio.sleep(0.1)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while line := await reader.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                self._dispatch(message)
        except (OSError, ValueError, asyncio.IncompleteReadError):
            pass
        finally:
            self._closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(MpvError("mpv closed"))
            self._pending.clear()
            for callback in self._close_callbacks:
                try:
                    callback()
                except Exception:
                    log.exception("mpv close callback failed")

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "event" not in message:
            future = self._pending.pop(message.get("request_id", -1), None)
            if future is not None and not future.done():
                if message.get("error") == "success":
                    future.set_result(message.get("data"))
                else:
                    future.set_exception(MpvError(str(message.get("error"))))
            return
        if message["event"] == "property-change":
            for callback in self._observers.get(message.get("name", ""), ()):
                try:
                    callback(message.get("data"))
                except Exception:
                    log.exception("mpv observer for %s failed", message.get("name"))

    async def command(self, *args: Any, timeout: float = 5.0) -> Any:
        if not self.running or self._writer is None:
            raise MpvError("mpv is not running")
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._writer.write(json.dumps({"command": list(args), "request_id": request_id}).encode() + b"\n")
            await self._writer.drain()
            return await asyncio.wait_for(future, timeout)
        except OSError as exc:
            raise MpvError("mpv closed") from exc
        except TimeoutError as exc:
            raise MpvError(f"mpv did not answer {args[0]!r}") from exc
        finally:
            self._pending.pop(request_id, None)

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
        """Ask mpv to quit, kill it if it will not, and wait until it is gone."""
        if self.running:
            with contextlib.suppress(MpvError):
                await self.command("quit", timeout=1.0)
        if self._process is not None and self._process.returncode is None:
            try:
                await asyncio.wait_for(self._process.wait(), timeout)
            except TimeoutError:
                self.kill()
                await self._process.wait()
        if self._writer is not None:
            self._writer.close()
        if self._reader_task is not None:
            with contextlib.suppress(Exception):
                await self._reader_task

    def kill(self) -> None:
        if self._process is not None and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
