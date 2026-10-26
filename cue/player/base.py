"""The player interface used by playback sessions."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol


class PlayerError(Exception):
    """A media player could not start or complete a control command."""


class Player(Protocol):
    """Common lifecycle and controls implemented by Cue's player backends.

    Property names and command names retain the original mpv vocabulary; each
    backend translates them into its native controls.
    """

    @property
    def running(self) -> bool: ...

    def observe(self, name: str, callback: Callable[[Any], None]) -> None: ...

    def on_close(self, callback: Callable[[], None]) -> None: ...

    async def launch(
        self,
        url: str,
        *,
        start: float | None = None,
        headers: Mapping[str, str] | None = None,
        title: str | None = None,
        extra_args: Sequence[str] = (),
    ) -> None: ...

    async def command(self, *args: Any, timeout: float = 5.0) -> Any: ...

    async def quit(self, timeout: float = 3.0) -> None: ...

    def kill(self) -> None: ...
