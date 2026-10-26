"""Episodes: a grid with watched markers, plus a range input for batch play/download."""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Footer, Header, Input, LoadingIndicator, Select, Static

from cue.providers.base import Episode, ProviderError, Show
from cue.screens import CueScreen
from cue.selection import SelectionError, parse_episode_range
from cue.util import format_time
from cue.widgets import EpisodeGrid, StatusBar

CELL_WIDTH = 9  # characters per grid cell, padding included


class EpisodesScreen(CueScreen):
    BINDINGS = [
        Binding("slash", "focus_range", "Range"),
        Binding("d", "download", "Download"),
        Binding("b", "toggle_audio", "SUB/DUB"),
        Binding("q", "back", "Back"),
        Binding("escape", "focus_grid", show=False),
    ]

    def __init__(self, provider_name: str, show: Show, *, resume: bool = False) -> None:
        super().__init__()
        self.provider_name = provider_name
        self.show = show
        self.episodes: list[Episode] = []
        self._resume = resume
        self._columns = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="episodes-view", classes="view"):
            yield Static(self._show_info(), id="show-info")
            modes = self.cue.audio_modes(self.provider_name)
            if modes:
                labels = {"sub": "SUB · original audio", "dub": "DUB · dubbed audio"}
                yield Select(
                    [(labels.get(mode, mode.upper()), mode) for mode in modes],
                    value=self.cue.audio_mode(self.provider_name),
                    allow_blank=False,
                    id="audio-mode",
                )
            yield Input(placeholder="Episodes: 3 · 1-5 · -1 (latest) · 1-3,7   Enter plays, d downloads", id="range")
            yield LoadingIndicator()
            yield EpisodeGrid(id="grid", show_header=False, cursor_type="cell")
            yield Static("", id="detail", classes="hint")
        yield StatusBar()
        yield Footer()

    def _show_info(self) -> Text:
        text = Text(self.show.title, style="bold")
        if self.show.year:
            text.append(f"  ({self.show.year})", style="dim")
        return text

    def on_mount(self) -> None:
        self.sub_title = self.show.title
        self.run_worker(self._load(), exclusive=True)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "toggle_audio" and not self.cue.audio_modes(self.provider_name):
            return None
        return True

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "audio-mode" and isinstance(event.value, str):
            if event.value != self.cue.audio_mode(self.provider_name):
                self.cue.set_audio_mode(self.provider_name, event.value)

    def action_toggle_audio(self) -> None:
        modes = self.cue.audio_modes(self.provider_name)
        if modes:
            current = self.cue.audio_mode(self.provider_name)
            self.query_one("#audio-mode", Select).value = modes[(modes.index(current) + 1) % len(modes)]

    async def _load(self) -> None:
        provider = self.cue.providers[self.provider_name]
        try:
            self.episodes = await provider.episodes(self.show)
        except ProviderError as exc:
            self.notify(str(exc), title="Could not load episodes", severity="error")
            self.episodes = []
        finally:
            self.query_one(LoadingIndicator).display = False
        if not self.episodes:
            self.query_one("#detail", Static).update("No episodes.")
            return
        self._build_grid()
        self.query_one(EpisodeGrid).focus()
        if self._resume:
            self._resume = False
            self._resume_playback()

    # -- grid ----------------------------------------------------------------

    def _column_count(self) -> int:
        width = self.query_one(EpisodeGrid).size.width or self.app.size.width
        return max(1, (width - 2) // CELL_WIDTH)

    def _build_grid(self, keep: float | None = None) -> None:
        grid = self.query_one(EpisodeGrid)
        if keep is None and grid.row_count:
            current = self._highlighted()
            keep = current.number if current else None
        self._columns = columns = self._column_count()
        watched = self.cue.store.watched(self.provider_name, self.show.id)
        entry = self.cue.store.get_history(self.provider_name, self.show.id)
        in_progress = entry.last_episode if entry and entry.position > 0 else None
        width = max(len(episode.label) for episode in self.episodes)

        grid.clear(columns=True)
        grid.add_columns(*(f"c{i}" for i in range(columns)))
        for start in range(0, len(self.episodes), columns):
            row: list[Text | str] = [
                self._cell(episode, width, episode.number in watched, episode.number == in_progress)
                for episode in self.episodes[start : start + columns]
            ]
            row += [""] * (columns - len(row))
            grid.add_row(*row)
        if keep is not None:
            self._move_to(keep)
        self._update_detail()

    @staticmethod
    def _cell(episode: Episode, width: int, watched: bool, in_progress: bool) -> Text:
        label = episode.label.rjust(width)
        if in_progress:
            return Text(f"▶ {label}", style="bold yellow")
        if watched:
            return Text(f"✓ {label}", style="green")
        return Text(f"  {label}")

    def _index_at(self, coordinate: Coordinate) -> int | None:
        index = coordinate.row * self._columns + coordinate.column
        return index if 0 <= index < len(self.episodes) else None

    def _highlighted(self) -> Episode | None:
        index = self._index_at(self.query_one(EpisodeGrid).cursor_coordinate)
        return None if index is None else self.episodes[index]

    def _move_to(self, number: float) -> None:
        for index, episode in enumerate(self.episodes):
            if episode.number == number:
                row, column = divmod(index, self._columns)
                self.query_one(EpisodeGrid).cursor_coordinate = Coordinate(row, column)
                return

    def _update_detail(self) -> None:
        episode = self._highlighted()
        if episode is None:
            self.query_one("#detail", Static).update("")
            return
        text = Text("Movie" if self.show.media_type == "movie" else f"Episode {episode.label}", style="bold")
        if episode.title:
            text.append(f" — {episode.title}")
        if episode.number in self.cue.store.watched(self.provider_name, self.show.id):
            text.append("  · watched", style="green")
        entry = self.cue.store.get_history(self.provider_name, self.show.id)
        if entry and entry.last_episode == episode.number and entry.position > 0:
            text.append(f"  · stopped at {format_time(entry.position)}", style="yellow")
        self.query_one("#detail", Static).update(text)

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        self._update_detail()

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        index = self._index_at(event.coordinate)
        if index is not None:
            self.cue.play(self.provider_name, self.show, self.episodes, index)

    def on_resize(self, event: events.Resize) -> None:
        if self.episodes and self._column_count() != self._columns:
            self._build_grid()

    def on_screen_resume(self) -> None:
        if self.episodes:
            self._build_grid()  # watched markers may have changed during playback

    # -- range input -----------------------------------------------------------

    def _selection(self) -> list[Episode] | None:
        text = self.query_one("#range", Input).value.strip()
        if not text:
            return None
        numbers = parse_episode_range(text, [episode.number for episode in self.episodes])
        by_number = {episode.number: episode for episode in self.episodes}
        return [by_number[number] for number in numbers]

    def on_input_submitted(self, event: Input.Submitted) -> None:
        try:
            chosen = self._selection()
        except SelectionError as exc:
            self.notify(str(exc), severity="warning")
            return
        if chosen:
            self.cue.play(self.provider_name, self.show, chosen, 0)

    def action_download(self) -> None:
        try:
            chosen = self._selection()
        except SelectionError as exc:
            self.notify(str(exc), severity="warning")
            return
        if chosen is None:
            highlighted = self._highlighted()
            chosen = [highlighted] if highlighted else []
        if chosen:
            self.cue.download(self.provider_name, self.show, chosen)

    # -- resume ----------------------------------------------------------------

    def _resume_playback(self) -> None:
        """Continue where the history left off: the unfinished episode, else the next one."""
        entry = self.cue.store.get_history(self.provider_name, self.show.id)
        numbers = [episode.number for episode in self.episodes]
        if entry is None:
            index = 0
        elif entry.position > 0 and entry.last_episode in numbers:
            index = numbers.index(entry.last_episode)
        else:
            later = [i for i, number in enumerate(numbers) if number > entry.last_episode]
            if not later:
                self._move_to(numbers[-1])
                self.notify(f"You're caught up on {self.show.title}.")
                return
            index = later[0]
        self._move_to(numbers[index])
        self.cue.play(self.provider_name, self.show, self.episodes, index)

    def action_focus_range(self) -> None:
        self.query_one("#range", Input).focus()

    def action_focus_grid(self) -> None:
        self.query_one(EpisodeGrid).focus()

    def action_back(self) -> None:
        self.app.pop_screen()
