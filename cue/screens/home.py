"""Home: search box, Continue Watching, provider indicator (in the status bar)."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from cue.providers.base import format_number
from cue.screens import CueScreen
from cue.store import HistoryEntry
from cue.util import format_time
from cue.widgets import StatusBar, VimOptionList


def describe_history(entry: HistoryEntry) -> Text:
    text = Text(entry.title, style="bold")
    episode = format_number(entry.last_episode)
    if entry.position > 0:
        text.append(f"  episode {episode} at {format_time(entry.position)}", style="dim")
    else:
        text.append(f"  watched episode {episode}, next one up", style="dim")
    text.append(f"  [{entry.provider}]", style="dim italic")
    return text


class HomeScreen(CueScreen):
    BINDINGS = [
        Binding("slash", "focus_search", "Search"),
        Binding("q", "app.quit", "Quit"),
        Binding("down", "focus_list", show=False),
        Binding("escape", "focus_list", show=False),
    ]

    def __init__(self, initial_query: str | None = None) -> None:
        super().__init__()
        self._initial_query = initial_query
        self._entries: list[HistoryEntry] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="home", classes="view"):
            yield Input(placeholder="Search…  (Enter searches, Tab switches provider)", id="search")
            yield Label("Continue Watching", classes="section")
            yield VimOptionList(id="continue")
            yield Static("", id="home-hint", classes="hint")
        yield StatusBar()
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Home"
        self.refresh_continue()
        self.query_one("#search", Input).focus()
        if self._initial_query:
            query, self._initial_query = self._initial_query, None
            self.cue.open_results(query)

    def on_screen_resume(self) -> None:
        self.refresh_continue()

    def refresh_continue(self) -> None:
        self._entries = self.cue.store.continue_watching()
        options = [
            Option(describe_history(entry), disabled=entry.provider not in self.cue.providers)
            for entry in self._entries
        ]
        self.query_one("#continue", OptionList).set_options(options)
        hint = self.cue.setup_hint()
        if not hint and not self._entries:
            hint = "Nothing here yet. Search for a show above."
        self.query_one("#home-hint", Static).update(hint)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            self.cue.open_results(query)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.cue.resume(self._entries[event.option_index])

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_focus_list(self) -> None:
        options = self.query_one("#continue", OptionList)
        if options.option_count:
            options.focus()
        else:
            self.set_focus(None)  # leave the search box so single-key bindings work
