"""The Textual app: shared services and screen routing."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import httpx
from textual.app import App, SystemCommand
from textual.binding import Binding
from textual.screen import Screen

from cue import __version__
from cue.cast import CastServer
from cue.cast.network import AdapterError, adapter_address
from cue.config import Change, Config, ConfigError, config_path, db_path, edit_config, subs_dir
from cue.config import dropin_dir as default_dropin_dir
from cue.config import plugins_dir as default_plugins_dir
from cue.download import DownloadError, download
from cue.groups import Group, single
from cue.groups import build as build_groups
from cue.player.base import Player
from cue.player.registry import players as player_backends
from cue.player.session import PlaybackSession
from cue.plugins import CatalogEntry, PluginManager
from cue.providers import create_providers, provider_options
from cue.providers.base import Episode, Provider, ProviderError, Show
from cue.screens.episodes import EpisodesScreen
from cue.screens.home import HomeScreen
from cue.screens.player import NowPlayingScreen
from cue.screens.plugins import PluginsScreen
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
    SubtitlePicker, ConfirmDialog, AddPluginDialog, ProviderSettingsDialog, GroupDialog, NewGroupDialog {
        align: center middle;
    }
    .dialog {
        width: 90%;
        max-width: 96;
        height: auto;
        max-height: 90%;
        padding: 0 1;
        border: round $accent;
        background: $surface;
    }
    .dialog .field {
        margin-top: 1;
    }
    .dialog-text {
        margin-top: 1;
    }
    .dialog-buttons {
        height: auto;
        margin-top: 1;
        align-horizontal: right;
    }
    .dialog-buttons Button {
        margin-left: 1;
    }
    #plugins-view {
        height: 1fr;
    }
    #plugins-view TabPane {
        padding: 0;
    }
    #provider-table, #group-table, #player-table, #plugin-table {
        height: 1fr;
        margin-top: 1;
    }
    #provider-detail, #group-detail, #player-detail, #plugin-detail {
        height: auto;
        min-height: 2;
        margin-top: 1;
    }
    #group-members {
        height: auto;
        max-height: 12;
    }
    .warning {
        color: $warning;
    }
    .error {
        color: $error;
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
        Binding("tab", "switch_group", "Group", priority=True),
        Binding("a", "toggle_auto_skip", "Auto-skip"),
    ]

    def __init__(
        self,
        config: Config,
        *,
        store: Store | None = None,
        http: httpx.AsyncClient | None = None,
        player_factory: PlayerFactory | None = None,
        dropin_dir: Path | None = None,
        plugin_root: Path | None = None,
        initial_query: str | None = None,
        provider: str | None = None,
        group: str | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self._owns_store = store is None
        self.store = store if store is not None else Store(db_path())
        self.http = http or httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers={"User-Agent": f"Cue/{__version__}"}
        )
        self._dropin_dir = dropin_dir if dropin_dir is not None else default_dropin_dir()
        self.plugins = PluginManager(
            plugin_root if plugin_root is not None else default_plugins_dir(), config.plugins.disabled
        )
        self.plugins.clean_up()
        self.plugins.load_enabled()
        self.providers, self.provider_problems = create_providers(self._provider_tables(), self._dropin_dir)
        self._provider_options = {name: provider_options(config.providers.get(name, {})) for name in self.providers}
        self._retired: list[Provider] = []  # replaced providers; a playing episode may still use one
        self.catalog: list[CatalogEntry] | None = None  # loaded by the Plugins menu
        self.groups: list[Group] = []
        self.group: Group | None = None  # what searches cover
        self.auto_skip = config.auto_skip
        self.status_message = ""
        self._build_groups()
        if provider:
            if provider in self.providers:
                self.group = single(provider)
            else:
                self.status_message = f"provider {provider!r} is not available"
        elif group:
            if not self._choose_group(group):
                self.status_message = f"group {group!r} has no providers that are set up"
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
        self.cast_server: CastServer | None = None

    def _build_groups(self) -> None:
        """Work out the groups from the ready providers, keeping the current one if it still exists."""
        self.groups = build_groups(self.providers, self.config.providers, self.config.groups)
        current = self.group
        if current is not None and current.name.startswith("provider:"):
            if current.providers[0] in self.providers:
                return  # cue -p <provider> keeps its one provider
            current = None
        if current is None or not self._choose_group(current.name):
            self.group = None
            # Older configs name a default provider: start in its first group.
            fallback = next((g.name for g in self.groups if self.config.default_provider in g.providers), "")
            for name in (self.config.default_group, fallback):
                if name and self._choose_group(name):
                    return
            self.group = self.groups[0] if self.groups else None

    def _choose_group(self, name: str) -> bool:
        found = next((group for group in self.groups if group.name == name), None)
        if found is not None:
            self.group = found
        return found is not None

    def get_default_screen(self) -> Screen[None]:
        return HomeScreen(initial_query=self._initial_query)

    def on_mount(self) -> None:
        if self.config.cast.enabled:
            self._start_cast()

    async def on_unmount(self) -> None:
        if self.cast_server is not None:
            self.cast_server.stop()
        for session in self._sessions:
            session.kill()
        for provider in [*self.providers.values(), *self._retired]:
            close = getattr(provider, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()
        await self.http.aclose()
        if self._owns_store:
            self.store.close()

    # -- status ----------------------------------------------------------------

    def setup_hint(self) -> str:
        lines = []
        if not self.providers:
            lines.append("No providers are set up yet. Press Esc, then p, to set one up or install a plugin.")
        lines += [f"{name}: {problem}" for name, problem in self.provider_problems.items()]
        return "\n".join(lines)

    def set_status(self, message: str) -> None:
        self.status_message = message
        for screen in self.screen_stack:
            for bar in screen.query(StatusBar):
                bar.refresh()

    def action_switch_group(self) -> None:
        if not self.groups:
            self.set_status("no providers configured")
            return
        names = [group.name for group in self.groups]
        current = self.group.name if self.group else None
        position = names.index(current) + 1 if current in names else 0
        self.group = self.groups[position % len(self.groups)]
        self.set_status(f"searching {self.group.title}")

    def action_toggle_auto_skip(self) -> None:
        self.auto_skip = not self.auto_skip
        self.set_status(f"auto-skip {'on' if self.auto_skip else 'off'}")

    # -- plugins and settings ------------------------------------------------------

    def update_config(self, *changes: Change) -> bool:
        """Save ``changes`` to the config file and apply them; False (with a notice) if refused."""
        path = self.config.path or config_path()
        try:
            config = edit_config(path, *changes)
        except (ConfigError, OSError) as exc:
            self.notify(str(exc), title="Settings not saved", severity="error", timeout=8)
            return False
        self.config = config
        self.plugins.disabled = set(config.plugins.disabled)
        self.reload_providers()
        return True

    def _provider_tables(self) -> dict[str, dict[str, Any]]:
        """Config tables of providers that may start; a turned-off plugin's providers are left out."""
        hidden = {name for plugin in self.plugins.plugins() if not plugin.enabled for name in plugin.manifest.providers}
        return {name: table for name, table in self.config.providers.items() if name not in hidden}

    def reload_providers(self) -> None:
        """Re-create providers after plugins or settings changed; unchanged ones are kept."""
        current = {
            name: (provider, self._provider_options[name])
            for name, provider in self.providers.items() if name in self._provider_options
        }
        ready, self.provider_problems = create_providers(self._provider_tables(), self._dropin_dir, reuse=current)
        self._retired += [provider for name, provider in self.providers.items() if ready.get(name) is not provider]
        self.providers = ready
        self._provider_options = {name: provider_options(self.config.providers.get(name, {})) for name in ready}
        self._build_groups()
        self.set_status(self.status_message)

    # -- routing -----------------------------------------------------------------

    def open_results(self, query: str) -> None:
        if self.group is None:
            self.notify(self.setup_hint() or "No providers configured.", severity="warning", timeout=8)
            return
        self.push_screen(ResultsScreen(self.group, query))

    def open_plugins(self) -> None:
        self.push_screen(PluginsScreen())

    def action_open_plugins(self) -> None:
        if not isinstance(self.screen, PluginsScreen):
            self.open_plugins()

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "Plugins, providers and players", "Install plugins, set up providers, choose a player",
            self.action_open_plugins,
        )

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
        note = ""
        if factory is None:
            backend = player_backends().get(self.config.player)
            if backend is None:
                problem = f"The {self.config.player} player isn't installed. Choose one under Players (p on the Home screen)."
            elif not backend.supported:
                problem = f"{backend.title} doesn't run on this system. Choose another under Players (p on the Home screen)."
            else:
                options = self.config.players.get(backend.name, {})
                program = backend.program(options)
                problem = "" if program else f"{backend.title} not found. {backend.missing}".strip()
            if problem:
                self.notify(problem, title="Cannot play", severity="error", timeout=10)
                return

            def factory() -> Player:
                return backend.create(program, options)

            if not backend.controls:
                note = f"Playing in {backend.title}, which Cue can't control: no auto-skip or resume point. Press q when done."

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
            player_note=note,
        )
        self._sessions.add(session)
        self.push_screen(NowPlayingScreen(session), callback=lambda _: self._sessions.discard(session))

    # -- casting ------------------------------------------------------------------

    def _start_cast(self) -> None:
        settings = self.config.cast
        try:
            # A chosen address or adapter is also the only one Cue listens on;
            # otherwise Cue listens on every adapter and announces the main one.
            address = settings.host or (adapter_address(settings.interface) if settings.interface else "")
        except AdapterError as exc:
            self.notify(f"Casting is unavailable: {exc}", title="Cast", severity="error", timeout=15)
            return
        server = CastServer(
            loop=asyncio.get_running_loop(),
            store=self.store,
            provider_subtitles=self.provider_subtitles,
            subtitle_languages=self.config.subtitles.languages,
            quality=self.config.quality,
            name=settings.name,
            host=address,
            port=settings.port,
            bind=address,
            on_status=self.set_status,
        )
        try:
            server.start()
        except OSError as exc:
            server.stop()
            self.notify(
                f"Casting is unavailable: cannot listen on {address or 'this PC'}, port {settings.port} ({exc}). "
                "Check [cast] host and port in the config.",
                title="Cast", severity="error", timeout=15,
            )
            return
        self.cast_server = server

    def cast(
        self, provider_name: str, show: Show, queue: Sequence[Episode], *, provider: Provider | None = None,
    ) -> None:
        """List a queue on the TV; VLC there opens it from its Network tab.

        ``provider`` keeps a playing session's SUB/DUB binding when it moves to the TV.
        """
        server = self.cast_server
        if server is None:
            hint = (
                "Casting is unavailable; see the earlier error."
                if self.config.cast.enabled
                else "Casting is off. Add a [cast] table with enabled = true to your config, then restart Cue."
            )
            self.notify(hint, title="Cast", severity="warning", timeout=10)
            return
        # Bound now, like playback: a later SUB/DUB change must not alter this queue.
        provider = provider if provider is not None else self._playback_provider(provider_name)
        url = server.publish(provider_name, provider, show, queue)
        self.notify(
            f"On the TV, open VLC → Network → {server.name} → {show.title}.\nDirect link: {url}",
            title="Ready to cast", timeout=15,
        )
        count = len(queue)
        self.set_status(f"casting {show.title} ({count} item{'s' if count != 1 else ''}) to {server.base_url}")

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
