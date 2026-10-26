"""Shared widgets: vim-key lists and the status bar."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Self

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable, OptionList, Static

if TYPE_CHECKING:
    from cue.app import CueApp


class VimOptionList(OptionList):
    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def set_options(self, options: Iterable[Any]) -> Self:
        super().set_options(options)
        if self.option_count:
            self.action_first()  # so Enter works straight away
        return self


class EpisodeGrid(DataTable):
    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("h", "cursor_left", show=False),
        Binding("l", "cursor_right", show=False),
    ]


class StatusBar(Static):
    """Current provider, auto-skip/auto-next state and the latest message."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
    }
    """

    def render(self) -> Text:
        app: CueApp = self.app  # type: ignore[assignment]
        text = Text()
        text.append(f"▸ {app.provider_name or 'no provider'}", style="bold")
        text.append(f"  │  auto-skip {'on' if app.auto_skip else 'off'}")
        text.append(f"  │  auto-next {'on' if app.config.auto_next else 'off'}")
        if app.status_message:
            text.append(f"  │  {app.status_message}", style="italic")
        return text
