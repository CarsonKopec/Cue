"""Players that Cue starts but can't control, for plugins that add other programs."""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from cue.player.base import PlayerError


class ProcessPlayer:
    """Runs a player program with a command line from :meth:`arguments`.

    Cue learns only when the program exits, so there is no position, seeking,
    auto-skip or subtitle switching; Stop closes the program. Set ``headers`` to the
    lower-case header names the program can send (``None`` for any); a stream that
    needs another header is refused with a clear message instead of failing to play.
    """

    headers: frozenset[str] | None = frozenset()
    title = "This player"

    def __init__(self, program: str, options: Mapping[str, Any] | None = None) -> None:
        self.program = program
        self.options = dict(options or {})
        self._process: asyncio.subprocess.Process | None = None
        self._observers: dict[str, list[Callable[[Any], None]]] = {}
        self._close_callbacks: list[Callable[[], None]] = []
        self._watcher: asyncio.Task[None] | None = None

    def arguments(
        self, url: str, *, start: float | None, headers: Mapping[str, str], title: str | None,
        extra_args: Sequence[str],
    ) -> list[str]:
        """The command line after the program path."""
        raise NotImplementedError

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def observe(self, name: str, callback: Callable[[Any], None]) -> None:
        self._observers.setdefault(name, []).append(callback)

    def on_close(self, callback: Callable[[], None]) -> None:
        self._close_callbacks.append(callback)

    def _emit(self, name: str, value: Any) -> None:
        for callback in self._observers.get(name, []):
            callback(value)

    async def launch(
        self, url: str, *, start: float | None = None, headers: Mapping[str, str] | None = None,
        title: str | None = None, extra_args: Sequence[str] = (),
    ) -> None:
        if self._process is not None:
            raise PlayerError(f"{self.title} was already started")
        headers = dict(headers or {})
        if self.headers is not None:
            unsupported = sorted(name for name in headers if name.lower() not in self.headers)
            if unsupported:
                raise PlayerError(
                    f"{self.title} can't send the {', '.join(unsupported)} header this source needs; "
                    "pick VLC or mpv for it."
                )
        if any(char in str(value) for value in headers.values() for char in "\r\n\0"):
            raise PlayerError("Invalid playback HTTP header")
        args = [self.program, *self.arguments(url, start=start, headers=headers, title=title, extra_args=extra_args)]
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # its own window, not Cue's console
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **kwargs,
            )
        except OSError as exc:
            raise PlayerError(f"Could not start {self.title}: {exc.strerror or exc}") from exc
        self._watcher = asyncio.create_task(self._watch(self._process))

    def failure(self, code: int, lines: list[str], seconds: float) -> str | None:
        """The error to show when the program exits, if it failed; ``lines`` is the end of its stderr."""
        return lines[-1] if code != 0 and lines else None

    async def _watch(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        began = asyncio.get_running_loop().time()
        tail = b""
        # Keep reading: a program that fills the pipe while nobody reads it stops dead.
        while chunk := await process.stderr.read(65536):
            tail = (tail + chunk)[-8192:]
        code = await process.wait()
        text = tail.decode("utf-8", "replace").replace("\r", "\n")
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        message = self.failure(code, lines, asyncio.get_running_loop().time() - began)
        if message:
            self._emit("error", f"{self.title}: {message}")
        for callback in self._close_callbacks:
            callback()

    async def command(self, *args: Any, timeout: float = 5.0) -> Any:
        raise PlayerError(f"{self.title} can't be controlled from Cue")

    async def quit(self, timeout: float = 3.0) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except TimeoutError:
            self.kill()

    def kill(self) -> None:
        if self._process is not None and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
