"""Results: the provider's matches, narrowed further with a fuzzy filter."""

from __future__ import annotations

from collections.abc import Sequence

from rapidfuzz import fuzz, process, utils
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, Label, LoadingIndicator, OptionList, Static

from cue.providers.base import ProviderError, Show
from cue.screens import CueScreen
from cue.widgets import StatusBar, VimOptionList


def fuzzy_filter(query: str, shows: Sequence[Show]) -> list[Show]:
    """Shows ranked by fuzzy match against ``query``; all of them when it is empty."""
    if not query.strip():
        return list(shows)
    choices = {index: show.title for index, show in enumerate(shows)}
    matches = process.extract(
        query, choices, scorer=fuzz.WRatio, processor=utils.default_process, score_cutoff=60, limit=None
    )
    return [shows[index] for _title, _score, index in matches]


def describe_show(show: Show) -> Text:
    text = Text(show.title, style="bold")
    if show.year:
        text.append(f"  ({show.year})", style="dim")
    if show.episode_count is not None:
        plural = "" if show.episode_count == 1 else "s"
        text.append(f"  · {show.episode_count} episode{plural}", style="dim")
    return text


class ResultsScreen(CueScreen):
    BINDINGS = [
        Binding("slash", "focus_filter", "Filter"),
        Binding("q", "back", "Back"),
        Binding("down", "focus_list", show=False),
        Binding("escape", "focus_list", show=False),
    ]

    def __init__(self, provider_name: str, search_query: str) -> None:
        super().__init__()
        self.provider_name = provider_name
        self.search_query = search_query
        self._shows: list[Show] = []
        self._visible: list[Show] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="results-view", classes="view"):
            yield Label(f'{self.provider_name} · "{self.search_query}"', classes="section")
            yield Input(placeholder="Filter (fuzzy)…", id="filter")
            yield LoadingIndicator()
            yield VimOptionList(id="results")
            yield Static("", id="results-hint", classes="hint")
        yield StatusBar()
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Results"
        self.run_worker(self._search(), exclusive=True)

    async def _search(self) -> None:
        provider = self.cue.providers[self.provider_name]
        try:
            shows = await provider.search(self.search_query)
        except ProviderError as exc:
            shows = []
            self.notify(str(exc), title="Search failed", severity="error")
        finally:
            self.query_one(LoadingIndicator).display = False
        self._shows = shows
        self._show(shows)
        self.query_one("#results", OptionList).focus()

    def _show(self, shows: list[Show]) -> None:
        self._visible = shows
        self.query_one("#results", OptionList).set_options(describe_show(show) for show in shows)
        hint = "" if shows else ("No matches for the filter." if self._shows else "No results.")
        self.query_one("#results-hint", Static).update(hint)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._show(fuzzy_filter(event.value, self._shows))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_focus_list()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.cue.open_show(self.provider_name, self._visible[event.option_index])

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_focus_list(self) -> None:
        options = self.query_one("#results", OptionList)
        if options.option_count:
            options.focus()
        else:
            self.set_focus(None)  # leave the filter box so single-key bindings work

    def action_back(self) -> None:
        self.app.pop_screen()
