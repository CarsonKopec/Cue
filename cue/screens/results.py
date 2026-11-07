"""Results: every provider in the group searched at once, merged, then narrowed with a fuzzy filter."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from rapidfuzz import fuzz, process, utils
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, Label, LoadingIndicator, OptionList, Static

from cue.groups import Group
from cue.providers.base import ProviderError, Show
from cue.screens import CueScreen
from cue.widgets import StatusBar, VimOptionList

log = logging.getLogger(__name__)


def _ranked(query: str, titles: Sequence[str], cutoff: float) -> list[int]:
    """Indexes of ``titles`` by fuzzy match against ``query``, best first; ties keep their order."""
    choices = dict(enumerate(titles))
    matches = process.extract(
        query, choices, scorer=fuzz.WRatio, processor=utils.default_process, score_cutoff=cutoff, limit=None
    )
    return [index for _title, _score, index in sorted(matches, key=lambda match: (-match[1], match[2]))]


def fuzzy_filter(query: str, shows: Sequence[Show]) -> list[Show]:
    """Shows ranked by fuzzy match against ``query``; all of them when it is empty."""
    if not query.strip():
        return list(shows)
    return [shows[index] for index in _ranked(query, [show.title for show in shows], 60)]


def describe_show(show: Show, provider: str | None = None) -> Text:
    text = Text(show.title, style="bold")
    if show.year:
        text.append(f"  ({show.year})", style="dim")
    if show.media_type == "movie":
        text.append("  · movie", style="dim")
    elif show.episode_count is not None:
        plural = "" if show.episode_count == 1 else "s"
        text.append(f"  · {show.episode_count} episode{plural}", style="dim")
    if provider:
        text.append(f"  [{provider}]", style="dim italic")
    return text


class ResultsScreen(CueScreen):
    BINDINGS = [
        Binding("slash", "focus_filter", "Filter"),
        Binding("q", "back", "Back"),
        Binding("down", "focus_list", show=False),
        Binding("escape", "focus_list", show=False),
    ]

    def __init__(self, group: Group, search_query: str) -> None:
        super().__init__()
        self.group = group
        self.search_query = search_query
        self._results: list[tuple[str, Show]] = []  # (provider name, show), best match first
        self._visible: list[tuple[str, Show]] = []
        self._problems: list[str] = []
        self._waiting = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="results-view", classes="view"):
            yield Label(f'{self.group.title} · "{self.search_query}"', classes="section")
            yield Input(placeholder="Filter (fuzzy)…", id="filter")
            yield LoadingIndicator()
            yield VimOptionList(id="results")
            yield Static("", id="results-hint", classes="hint")
        yield StatusBar()
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Results"
        self.run_worker(self._search(), exclusive=True)

    async def _ask(self, name: str) -> tuple[str, list[Show], str | None]:
        provider = self.cue.providers[name]
        try:
            return name, self.group.shows(provider, await provider.search(self.search_query)), None
        except ProviderError as exc:
            return name, [], str(exc)
        except Exception as exc:  # a plugin's bug shouldn't hide the other providers' results
            log.exception("Search failed in %s", name)
            return name, [], f"{type(exc).__name__}: {exc}"

    async def _search(self) -> None:
        names = [name for name in self.group.providers if name in self.cue.providers]
        self._waiting = len(names)
        tasks = [asyncio.create_task(self._ask(name)) for name in names]
        try:
            for finished in asyncio.as_completed(tasks):
                name, shows, problem = await finished
                self._waiting -= 1
                if problem:
                    self._problems.append(f"{name}: {problem}")
                self._results.extend((name, show) for show in shows)
                self._rank()
                self._apply_filter()
                if self._results and not self.query_one("#filter", Input).value:  # not while filtering
                    self.query_one("#results", OptionList).focus()
        finally:
            for task in tasks:
                task.cancel()
            self.query_one(LoadingIndicator).display = False
            self._update_hint()

    def _rank(self) -> None:
        titles = [show.title for _name, show in self._results]
        order = _ranked(self.search_query, titles, 0) if self.search_query.strip() else list(range(len(titles)))
        self._results = [self._results[index] for index in order]

    def _apply_filter(self) -> None:
        text = self.query_one("#filter", Input).value
        if text.strip():
            order = _ranked(text, [show.title for _name, show in self._results], 60)
            self._show([self._results[index] for index in order])
        else:
            self._show(list(self._results))

    def _show(self, results: list[tuple[str, Show]]) -> None:
        self._visible = results
        tagged = len(self.group.providers) > 1
        self.query_one("#results", OptionList).set_options(
            describe_show(show, name if tagged else None) for name, show in results
        )
        self._update_hint()

    def _update_hint(self) -> None:
        lines = []
        if not self._visible and not self._waiting:
            lines.append("No matches for the filter." if self._results else "No results.")
        elif self._waiting:
            lines.append(f"Still searching {self._waiting} more provider{'s' if self._waiting != 1 else ''}…")
        lines += self._problems
        self.query_one("#results-hint", Static).update("\n".join(lines))

    def on_input_changed(self, event: Input.Changed) -> None:
        self._apply_filter()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_focus_list()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        name, show = self._visible[event.option_index]
        self.cue.open_show(name, show)

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
