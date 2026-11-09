"""The Textual app: screen routing on top of Cue's core (:mod:`cue.core`), which owns the shared services."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import httpx
from textual.app import App, SystemCommand
from textual.binding import Binding
from textual.screen import Screen

from cue.cast import CastServer, devices
from cue.config import Change, Config
from cue.core import Core
from cue.groups import Group, single
from cue.player.base import Player
from cue.player.registry import players as player_backends
from cue.player.relay import LocalRelay, route_player
from cue.player.session import PlaybackSession
from cue.providers.base import Episode, Provider, Show
from cue.screens.cast import DLNA, CastPicker, PairingDialog
from cue.screens.episodes import EpisodesScreen
from cue.screens.home import HomeScreen
from cue.screens.player import NowPlayingScreen
from cue.screens.plugins import PluginsScreen
from cue.screens.results import ResultsScreen
from cue.screens.settings import SettingsScreen
from cue.store import HistoryEntry, Store
from cue.widgets import StatusBar

PlayerFactory = Callable[[], Player]


def _shared(name: str) -> Any:
    """An attribute that lives on the app's core, so screens can keep using ``app.<name>``."""
    return property(lambda self: getattr(self.core, name), lambda self, value: setattr(self.core, name, value))


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
    SubtitlePicker, ConfirmDialog, AddPluginDialog, ProviderSettingsDialog, GroupDialog, NewGroupDialog,
    CastPicker, PairingDialog {
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

    # The shared services, owned by the core.
    config: Config = _shared("config")
    store: Store = _shared("store")
    http: httpx.AsyncClient = _shared("http")
    plugins = _shared("plugins")
    providers: dict[str, Provider] = _shared("providers")
    provider_problems: dict[str, str] = _shared("provider_problems")
    catalog = _shared("catalog")
    groups: list[Group] = _shared("groups")
    skip = _shared("skip")
    subtitles = _shared("subtitles")
    subtitle_problems: dict[str, str] = _shared("subtitle_problems")
    provider_subtitles = _shared("provider_subtitles")
    cast_server: CastServer | None = _shared("cast_server")
    _last_tv: str = _shared("last_tv")

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
        self.core = Core(
            config, store=store, http=http, dropin_dir=dropin_dir, plugin_root=plugin_root,
            on_notice=self._notice, on_status=self.set_status,
        )
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
        self._player_factory = player_factory
        self._initial_query = initial_query
        self._sessions: set[PlaybackSession] = set()
        self._audio_modes: dict[str, str] = {}

    def _notice(self, message: str, *, title: str = "", severity: str = "information",
                timeout: float | None = None) -> None:
        self.notify(message, title=title, severity=severity, timeout=timeout)  # type: ignore[arg-type]

    def _build_groups(self) -> None:
        """Choose the group searches cover from the core's groups, keeping the current one if it still exists."""
        current = self.group
        if current is not None and current.name.startswith("provider:"):
            if current.providers[0] in self.providers:
                return  # cue -p <provider> keeps its one provider
            current = None
        if current is None or not self._choose_group(current.name):
            # Older configs name a default provider: start in its first group.
            self.group = self.core.default_group()

    def _choose_group(self, name: str) -> bool:
        found = self.core.find_group(name)
        if found is not None:
            self.group = found
        return found is not None

    def get_default_screen(self) -> Screen[None]:
        return HomeScreen(initial_query=self._initial_query)

    def on_mount(self) -> None:
        if self.config.cast.enabled:
            self.core.start_cast()
        from cue import update
        from cue.install import is_frozen

        # Only cue.exe checks by itself; a source checkout (and the tests) never goes online for this.
        if self.config.updates.check and is_frozen() and update.due():
            self.run_worker(self._auto_update(), group="updates", exit_on_error=False)

    async def _auto_update(self) -> None:
        """The daily update check: a newer Cue is installed for next time, newer plugins right away."""
        from cue import update

        update.mark_checked()
        outcome = await update.run(
            self.http, self.plugins, self.config.plugins.catalog,
            install=self.config.updates.install, registry=update.user_registry(),
        )
        if outcome.plugins_installed:
            self.reload_providers()
        for title, message in outcome.notices():
            self.notify(message, title=title, timeout=10)

    async def on_unmount(self) -> None:
        self.core.stop_serving()
        for session in self._sessions:
            session.kill()
        await self.core.aclose()

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
        if not self.core.update_config(*changes):
            return False
        self._providers_changed()
        return True

    def reload_providers(self) -> None:
        """Re-create providers after plugins or settings changed; unchanged ones are kept."""
        self.core.reload_providers()
        self._providers_changed()

    def _providers_changed(self) -> None:
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

    def action_open_settings(self) -> None:
        if not isinstance(self.screen, SettingsScreen):
            self.push_screen(SettingsScreen())

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        # One way in: plugins, providers and players are a section of Settings.
        yield SystemCommand(
            "Settings", "Subtitles, plugins, providers and players", self.action_open_settings,
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
        return self.core.audio_modes(provider_name)

    def audio_mode(self, provider_name: str) -> str | None:
        default = self.core.default_audio_mode(provider_name)
        if default is None:
            return None
        return self._audio_modes.get(provider_name, default)

    def set_audio_mode(self, provider_name: str, mode: str) -> None:
        if mode not in self.audio_modes(provider_name):
            raise ValueError(f"Unsupported audio mode: {mode}")
        self._audio_modes[provider_name] = mode
        self.set_status(f"{mode.upper()} selected for new playback and downloads")

    def _playback_provider(self, provider_name: str) -> Provider:
        return self.core.playback_provider(provider_name, self.audio_mode(provider_name))

    def local_relay(self) -> LocalRelay:
        """The relay on 127.0.0.1 that gives simpler players links they can open; started on first use."""
        return self.core.local_relay()

    def play(self, provider_name: str, show: Show, queue: Sequence[Episode], index: int) -> None:
        factory = self._player_factory
        note = ""
        live_subtitles = True
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
                # Players that can't send a source's headers, play HLS or open links get
                # a relay link, one continuous stream or a downloaded file instead.
                return route_player(
                    backend.create(program, options), title=backend.title, headers=backend.headers,
                    hls=bool(options.get("hls", backend.hls)), links=backend.links, relay=self.local_relay,
                    folder=self.core.playback_folder(), quality=self.config.playback.quality or self.config.quality,
                    storage=self.core.storage(), burn_subtitles=self.config.subtitles.burn_in,
                )

            if not backend.controls:
                note = f"Playing in {backend.title}, which Cue can't control: no auto-skip or resume point. Press q when done."
                live_subtitles = False  # given beside a downloaded episode instead, when there is one

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
            live_subtitles=live_subtitles,
        )
        self._sessions.add(session)
        self.push_screen(NowPlayingScreen(session), callback=lambda _: self._sessions.discard(session))

    # -- casting ------------------------------------------------------------------

    def cast(
        self, provider_name: str, show: Show, queue: Sequence[Episode], *, provider: Provider | None = None,
    ) -> None:
        """Cast a queue: to VLC, which opens it from its Network tab, or to a DLNA, AirPlay or Chromecast TV.

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
        queue = list(queue)
        if not devices.supported():
            self.core.cast_to_vlc(server, provider_name, provider, show, queue)
            return

        def chosen(target: devices.Device | str | None) -> None:
            if target == DLNA:
                self.core.cast_to_vlc(server, provider_name, provider, show, queue)
            elif isinstance(target, devices.Device):
                self._last_tv = target.identifier
                self.run_worker(self._cast_to_tv(server, target, provider_name, provider, show, queue),
                                group="cast", exclusive=True)

        self.push_screen(CastPicker(self.airplay_credentials(), server.name, self._last_tv), callback=chosen)

    @staticmethod
    def airplay_credentials() -> Path:
        return Core.airplay_credentials()

    async def _cast_to_tv(
        self, server: CastServer, device: devices.Device, provider_name: str, provider: Provider, show: Show,
        queue: list[Episode],
    ) -> None:
        """Play a queue on a DLNA, AirPlay or Chromecast TV, moving on as each episode finishes."""

        async def ask_code() -> str | None:
            return await self.push_screen_wait(PairingDialog(device.name))

        await self.core.cast_to_tv(server, device, provider_name, provider, show, queue, ask_code=ask_code)

    # -- downloads -----------------------------------------------------------------

    def download(self, provider_name: str, show: Show, episodes: Sequence[Episode]) -> None:
        # Bind before scheduling: a selector change must not affect any episode
        # in a queued download, even if the worker has not started yet.
        provider = self._playback_provider(provider_name)
        self.run_worker(self.core.download(provider_name, show, list(episodes), provider), group="downloads")
