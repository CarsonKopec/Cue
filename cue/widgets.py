"""Shared widgets: vim-key lists and the status bar."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Self

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable, Input, OptionList, Static

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


class RangeInput(Input):
    """The episode range box. It takes only the characters a range can contain, so
    letter keys such as ``d`` (download) and ``t`` (cast) still reach the screen."""

    ALLOWED = "0123456789.-, "

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(restrict=r"[0-9.,\- ]*", **kwargs)

    def check_consume_key(self, key: str, character: str | None) -> bool:
        return character is not None and character in self.ALLOWED


class StatusBar(Static):
    """Current search group, auto-skip/auto-next state and the latest message."""

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
        group = app.group
        text.append(f"▸ {group.title if group else 'no providers'}", style="bold")
        if group is not None and (len(group.providers) > 1 or group.title != group.providers[0]):
            text.append(f" ({', '.join(group.providers)})", style="dim")
        text.append(f"  │  auto-skip {'on' if app.auto_skip else 'off'}")
        text.append(f"  │  auto-next {'on' if app.config.auto_next else 'off'}")
        if app.status_message:
            text.append(f"  │  {app.status_message}", style="italic")
        return text
