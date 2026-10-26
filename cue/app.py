"""The Textual app: shared services and screen routing."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
from textual.app import App
from textual.binding import Binding
from textual.screen import Screen

from cue import __version__
from cue.config import Config, config_path, db_path, plugins_dir, subs_dir
from cue.download import DownloadError, download
from cue.player.base import Player
from cue.player.locate import find_mpv, find_vlc
from cue.player.mpv import MpvPlayer
from cue.player.session import PlaybackSession
from cue.player.vlc import VlcPlayer
from cue.providers import create_providers
from cue.providers.base import Episode, Provider, ProviderError, Show
from cue.screens.episodes import EpisodesScreen
from cue.screens.home import HomeScreen
from cue.screens.player import NowPlayingScreen
from cue.screens.results import ResultsScreen
from cue.selection import select_quality
from cue.skip import SkipEngine
from cue.store import HistoryEntry, Store
from cue.subtitles import SubtitleService
from cue.subtitles.opensubtitles import OpenSubtitles
from cue.subtitles.provider import ProviderSubtitles
from cue.util import safe_filename
from cue.widgets import StatusBar

PlayerFactory = Callable[[], Player]


class CueApp(App[None]):
    TITLE = "Cue"
    CSS = """
    .view {
        height: 1fr;
        padding: 0 1;
    }
    .section {
        margin-top: 1;
        text-style: bold;
        color: $accent;
    }
    .hint {
        color: $text-muted;
    }
    #search, #filter, #range {
        margin-top: 1;
    }
    #continue, #results, #grid {
        height: 1fr;
        margin-top: 1;
    }
    #home-hint, #results-hint {
        margin-top: 1;
    }
    #show-info {
        margin-top: 1;
    }
    #audio-mode {
        width: 42;
        margin-top: 1;
    }
    #detail {
        height: auto;
        min-height: 1;
    }
    LoadingIndicator {
        height: 3;
    }
    #np-show {
        margin-top: 1;
        color: $accent;
    }
    #np-progress {
        margin-top: 1;
        width: 100%;
    }
    #np-progress Bar {
        width: 1fr;
    }
    #np-skip {
        margin-top: 1;
    }
    #np-message {
        margin-top: 1;
    }
    SubtitlePicker {
        align: center middle;
    }
    #picker {
        width: 90%;
        max-width: 120;
        height: 70%;
        padding: 0 1;
        border: round $accent;
        background: $surface;
    }
    #tracks {
        height: 1fr;
        margin-top: 1;
    }
    """
    BINDINGS = [
        Binding("tab", "switch_provider", "Provider", priority=True),
        Binding("a", "toggle_auto_skip", "Auto-skip"),
    ]

    def __init__(
        self,
        config: Config,
        *,
        store: Store | None = None,
        http: httpx.AsyncClient | None = None,
        player_factory: PlayerFactory | None = None,
        plugin_dir: Path | None = None,
        initial_query: str | None = None,
        provider: str | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self._owns_store = store is None
        self.store = store if store is not None else Store(db_path())
        self.http = http or httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers={"User-Agent": f"Cue/{__version__}"}
        )
        self.providers, self.provider_problems = create_providers(
            config.providers, plugin_dir if plugin_dir is not None else plugins_dir()
        )
        self.provider_name = self._initial_provider(provider)
        self.auto_skip = config.auto_skip
        self.status_message = ""
        if provider and provider not in self.providers:
            self.status_message = f"provider {provider!r} is not available"
        self.skip = SkipEngine(self.http, self.store)
        api_key = config.subtitles.opensubtitles_api_key
        self.subtitles = (
            SubtitleService(OpenSubtitles(api_key, self.http, subs_dir()), self.store, config.subtitles.languages)
            if api_key
            else None
        )
        self.provider_subtitles = ProviderSubtitles(self.http, subs_dir() / "provider")
        self._player_factory = player_factory
        self._initial_query = initial_query
        self._sessions: set[PlaybackSession] = set()
        self._audio_modes: dict[str, str] = {}

    def _initial_provider(self, requested: str | None) -> str | None:
        for name in (requested, self.config.default_provider):
            if name in self.providers:
                return name
        return next(iter(self.providers), None)

    def get_default_screen(self) -> Screen[None]:
        return HomeScreen(initial_query=self._initial_query)

    async def on_unmount(self) -> None:
        for session in self._sessions:
            session.kill()
        for provider in self.providers.values():
            close = getattr(provider, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()
        await self.http.aclose()
        if self._owns_store:
            self.store.close()

    # -- status ----------------------------------------------------------------

    @property
    def provider(self) -> Provider | None:
        return self.providers.get(self.provider_name) if self.provider_name else None

    def setup_hint(self) -> str:
        lines = []
        if not self.providers:
            path = self.config.path or config_path()
            lines.append(f"No providers are set up yet. Fill in [providers.jellyfin] or [providers.local] in {path}")
        lines += [f"{name}: {problem}" for name, problem in self.provider_problems.items()]
        return "\n".join(lines)

    def set_status(self, message: str) -> None:
        self.status_message = message
        for screen in self.screen_stack:
            for bar in screen.query(StatusBar):
                bar.refresh()

    def action_switch_provider(self) -> None:
        names = list(self.providers)
        if not names:
            self.set_status("no providers configured")
            return
        position = names.index(self.provider_name) + 1 if self.provider_name in names else 0
        self.provider_name = names[position % len(names)]
        self.set_status(f"searching {self.provider_name}")

    def action_toggle_auto_skip(self) -> None:
        self.auto_skip = not self.auto_skip
        self.set_status(f"auto-skip {'on' if self.auto_skip else 'off'}")

    # -- routing -----------------------------------------------------------------

    def open_results(self, query: str) -> None:
        if self.provider_name is None:
            self.notify(self.setup_hint() or "No providers configured.", severity="warning", timeout=8)
            return
        self.push_screen(ResultsScreen(self.provider_name, query))

    def open_show(self, provider_name: str, show: Show, *, resume: bool = False) -> None:
        self.push_screen(EpisodesScreen(provider_name, show, resume=resume))

    def resume(self, entry: HistoryEntry) -> None:
        if entry.provider not in self.providers:
            self.notify(f"The {entry.provider} provider is not set up.", severity="warning")
            return
        self.open_show(entry.provider, Show(id=entry.show_id, title=entry.title), resume=True)

    def audio_modes(self, provider_name: str) -> tuple[str, ...]:
        """Providers may opt into the audio selector with immutable bindings."""
        provider = self.providers[provider_name]
        if not callable(getattr(provider, "for_language", None)):
            return ()
        return tuple(getattr(provider, "audio_modes", ()))

    def audio_mode(self, provider_name: str) -> str | None:
        modes = self.audio_modes(provider_name)
        if not modes:
            return None
        default = getattr(self.providers[provider_name], "language", modes[0])
        return self._audio_modes.get(provider_name, default)

    def set_audio_mode(self, provider_name: str, mode: str) -> None:
        if mode not in self.audio_modes(provider_name):
            raise ValueError(f"Unsupported audio mode: {mode}")
        self._audio_modes[provider_name] = mode
        self.set_status(f"{mode.upper()} selected for new playback and downloads")

    def _playback_provider(self, provider_name: str) -> Provider:
        provider = self.providers[provider_name]
        mode = self.audio_mode(provider_name)
        return provider.for_language(mode) if mode is not None else provider

    def play(self, provider_name: str, show: Show, queue: Sequence[Episode], index: int) -> None:
        factory = self._player_factory
        if factory is None:
            use_vlc = self.config.player == "vlc"
            executable = find_vlc() if use_vlc else find_mpv()
            if executable is None:
                hint = (
                    "VLC not found. Install VLC, put vlc.exe next to cue.exe, or set CUE_VLC to its path."
                    if use_vlc
                    else "mpv not found. Put mpv.exe next to cue.exe, add it to PATH, or set CUE_MPV."
                )
                self.notify(
                    hint,
                    title="Cannot play",
                    severity="error",
                    timeout=10,
                )
                return

            def factory() -> Player:
                return VlcPlayer(executable) if use_vlc else MpvPlayer(executable)

        session = PlaybackSession(
            provider_name=provider_name,
            provider=self._playback_provider(provider_name),
            show=show,
            queue=queue,
            index=index,
            store=self.store,
            player_factory=factory,
            skip=self.skip,
            subtitles=self.subtitles,
            provider_subtitles=self.provider_subtitles,
            subtitle_languages=self.config.subtitles.languages,
            auto_subtitles=self.config.subtitles.auto_download,
            auto_skip=lambda: self.auto_skip,
            auto_next=self.config.auto_next,
            quality=self.config.quality,
            player_args=self.config.player_args,
        )
        self._sessions.add(session)
        self.push_screen(NowPlayingScreen(session), callback=lambda _: self._sessions.discard(session))

    # -- downloads -----------------------------------------------------------------

    def download(self, provider_name: str, show: Show, episodes: Sequence[Episode]) -> None:
        # Bind before scheduling: a selector change must not affect any episode
        # in a queued download, even if the worker has not started yet.
        provider = self._playback_provider(provider_name)
        self.run_worker(self._download(provider_name, show, list(episodes), provider), group="downloads")

    async def _download(
        self, provider_name: str, show: Show, episodes: list[Episode], provider: Provider | None = None
    ) -> None:
        provider = provider if provider is not None else self._playback_provider(provider_name)
        mode = getattr(provider, "language", None) if self.audio_modes(provider_name) else None
        suffix = f" [{mode.upper()}]" if mode else ""
        folder = self.config.downloads / safe_filename(show.title)
        saved = 0
        for position, episode in enumerate(episodes, start=1):
            prefix = f"downloading {position}/{len(episodes)}: episode {episode.label}{suffix}"
            self.set_status(prefix)
            try:
                stream = select_quality(await provider.stream(episode), self.config.quality)
                if stream is None:
                    raise DownloadError("no stream")
                name = f"{safe_filename(show.title)} - {episode.label.zfill(2)}{suffix}"
                await download(stream, folder, name, lambda percent, p=prefix: self.set_status(f"{p} {percent:.0f}%"))
                saved += 1
            except (ProviderError, DownloadError) as exc:
                self.notify(f"Episode {episode.label}: {exc}", title="Download", severity="error")
        if saved:
            self.set_status(f"saved {saved} episode{'s' if saved != 1 else ''} to {folder}")
        else:
            self.set_status("")
