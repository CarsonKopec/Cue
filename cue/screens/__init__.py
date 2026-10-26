"""Screens: Home, Results, Episodes and Now Playing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.screen import Screen

from cue.widgets import StatusBar

if TYPE_CHECKING:
    from cue.app import CueApp


class CueScreen(Screen[None]):
    @property
    def cue(self) -> CueApp:
        return self.app  # type: ignore[return-value]

    def on_screen_resume(self) -> None:
        for bar in self.query(StatusBar):
            bar.refresh()
