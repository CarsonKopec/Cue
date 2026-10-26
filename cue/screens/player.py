"""Now Playing: show/episode, player progress, skip status, next up."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Label, OptionList, ProgressBar, Static

from cue.player.session import PlaybackSession, PlaybackState
from cue.screens import CueScreen
from cue.subtitles import SubtitleError, SubtitleTrack
from cue.util import format_time
from cue.widgets import StatusBar, VimOptionList


class SubtitlePicker(ModalScreen[SubtitleTrack | None]):
    """Pick a server subtitle or optional OpenSubtitles result for this episode."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", show=False),
    ]

    def __init__(self, tracks: list[SubtitleTrack]) -> None:
        super().__init__()
        self.tracks = tracks

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Label("Choose subtitles", classes="section")
            yield VimOptionList(
                *(Text.assemble(
                    (f"[{track.language or 'unknown'}] ", "bold"), track.label,
                    (f" · {'server' if track.source == 'provider' else track.source}", "dim"),
                ) for track in self.tracks),
                id="tracks",
            )

    def on_mount(self) -> None:
        tracks = self.query_one("#tracks", OptionList)
        tracks.action_first()
        tracks.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.tracks[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)


class NowPlayingScreen(CueScreen):
    BINDINGS = [
        Binding("n", "next", "Next"),
        Binding("p", "previous", "Previous"),
        Binding("r", "replay", "Replay"),
        Binding("s", "skip", "Skip 85s"),
        Binding("c", "cycle_subtitles", "Subs"),
        Binding("C,shift+c", "pick_subtitles", "Pick subs"),
        Binding("q", "stop", "Stop"),
    ]

    def __init__(self, session: PlaybackSession) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="now-playing", classes="view"):
            yield Static("", id="np-show")
            yield Static("", id="np-episode")
            yield ProgressBar(total=None, show_eta=False, show_percentage=False, id="np-progress")
            yield Static("", id="np-time")
            yield Static("", id="np-skip")
            yield Static("", id="np-subs")
            yield Static("", id="np-next")
            yield Static("", id="np-message", classes="hint")
        yield StatusBar()
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Now Playing"
        self.session.listener = self.render_state
        self.render_state(self.session.state)
        self.run_worker(self._run(), group="playback")

    def on_unmount(self) -> None:
        self.session.listener = None
        self.session.kill()

    async def _run(self) -> None:
        await self.session.run()
        if self.session.state.error:
            self.notify(self.session.state.error, title="Playback stopped", severity="error")
        while self.app.screen is not self and len(self.app.screen_stack) > 1:
            self.app.pop_screen()  # e.g. a subtitle picker left open
        if self.app.screen is self:
            self.app.pop_screen()

    def render_state(self, state: PlaybackState) -> None:
        try:
            self._show_state(state)
        except NoMatches:
            pass  # screen already gone

    def _show_state(self, state: PlaybackState) -> None:
        self.query_one("#np-show", Static).update(Text(state.show.title, style="bold"))
        episode = state.episode
        if episode is not None:
            heading = Text("Movie" if state.show.media_type == "movie" else f"Episode {episode.label}")
            mode = getattr(self.session.provider, "language", None)
            if mode:
                heading.append(f"  [{mode.upper()}]", style="bold")
            if episode.title:
                heading.append(f" — {episode.title}", style="italic")
            self.query_one("#np-episode", Static).update(heading)

        bar = self.query_one("#np-progress", ProgressBar)
        if state.duration:
            bar.update(total=state.duration, progress=min(state.position or 0, state.duration))
        else:
            bar.update(total=None)
        self.query_one("#np-time", Static).update(f"{format_time(state.position)} / {format_time(state.duration)}")

        self.query_one("#np-skip", Static).update(f"Skip: {state.skip or '—'}")
        self.query_one("#np-subs", Static).update(f"Subtitles: {state.subtitles or '—'}")
        upcoming = state.next_up
        if upcoming is None:
            next_text = "Next up: —"
        else:
            next_text = f"Next up: episode {upcoming.label}" + (f" — {upcoming.title}" if upcoming.title else "")
            if not self.session.auto_next:
                next_text += "  (auto-next off: press n)"
        self.query_one("#np-next", Static).update(next_text)
        self.query_one("#np-message", Static).update(state.message)

    def action_next(self) -> None:
        self.session.next()

    def action_previous(self) -> None:
        self.session.previous()

    def action_replay(self) -> None:
        self.session.replay()

    async def action_skip(self) -> None:
        await self.session.manual_skip()

    async def action_cycle_subtitles(self) -> None:
        await self.session.cycle_subtitles()

    def action_pick_subtitles(self) -> None:
        self.run_worker(self._pick_subtitles(), exclusive=True, group="subtitles")

    async def _pick_subtitles(self) -> None:
        self.notify("Finding subtitle tracks…")
        try:
            tracks = await self.session.subtitle_choices()
        except SubtitleError as exc:
            self.notify(str(exc), title="Subtitles", severity="error")
            return
        if not tracks:
            self.notify(
                "No external subtitle tracks found. Use VLC's Subtitle menu for embedded tracks; "
                "an OpenSubtitles API key enables an additional search.",
                title="Subtitles", timeout=8,
            )
            return
        track = await self.app.push_screen_wait(SubtitlePicker(tracks))
        if track is None:
            return
        try:
            await self.session.use_subtitle(track)
        except SubtitleError as exc:
            self.notify(str(exc), title="Subtitles", severity="error")

    def action_stop(self) -> None:
        self.session.stop()
