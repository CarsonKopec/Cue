"""Cue's core: the services the terminal app and ``cue serve`` share, with no user interface.

A core owns the providers and search groups, plugins, subtitle sources, skip times, the
database, the local relay and the cast server. It also does the work behind casting to
TVs, downloads and settings changes. Whatever drives it hears back through two
callbacks: ``on_notice`` for a message worth showing on its own (an error, a TV that
started playing) and ``on_status`` for a line of progress. The Textual app
(:mod:`cue.app`) and the web server (:mod:`cue.web`) each run on one core.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx

from cue import __version__
from cue.cast import CastServer, devices
from cue.cast.network import AdapterError, adapter_address
from cue.config import Change, Config, ConfigError, app_dir, config_path, db_path, edit_config, subs_dir
from cue.config import dropin_dir as default_dropin_dir
from cue.config import plugins_dir as default_plugins_dir
from cue.download import DownloadError, download
from cue.groups import Group
from cue.groups import build as build_groups
from cue.player.locate import find_ffmpeg
from cue.player.relay import LocalRelay, Storage, downloads_dir, tidy
from cue.plugins import CatalogEntry, PluginManager
from cue.providers import create_providers, provider_options
from cue.providers.base import Episode, Provider, ProviderError, Show, Stream, is_local_file
from cue.selection import select_quality
from cue.skip import SkipEngine
from cue.store import Store
from cue.subtitles import SubtitleService
from cue.subtitles.provider import ProviderSubtitles
from cue.subtitles.registry import create_sources
from cue.util import safe_filename

log = logging.getLogger(__name__)

AskCode = Callable[[], Awaitable[str | None]]  # the code an AirPlay TV shows while pairing; None cancels


class Notice(Protocol):
    def __call__(
        self, message: str, *, title: str = "", severity: str = "information", timeout: float | None = None,
    ) -> None: ...


def _log_notice(message: str, *, title: str = "", severity: str = "information", timeout: float | None = None) -> None:
    level = {"error": logging.ERROR, "warning": logging.WARNING}.get(severity, logging.INFO)
    log.log(level, "%s%s", f"{title}: " if title else "", message)


class Core:
    def __init__(
        self,
        config: Config,
        *,
        store: Store | None = None,
        http: httpx.AsyncClient | None = None,
        dropin_dir: Path | None = None,
        plugin_root: Path | None = None,
        on_notice: Notice | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.on_notice: Notice = on_notice or _log_notice
        self.on_status: Callable[[str], None] = on_status or (lambda text: None)
        self._owns_store = store is None
        self.store = store if store is not None else Store(db_path())
        self.http = http or httpx.AsyncClient(
            timeout=20, follow_redirects=True, headers={"User-Agent": f"Cue/{__version__}"}
        )
        self.dropin_dir = dropin_dir if dropin_dir is not None else default_dropin_dir()
        self.plugins = PluginManager(
            plugin_root if plugin_root is not None else default_plugins_dir(), config.plugins.disabled
        )
        self.plugins.clean_up()
        self.plugins.load_enabled()
        self.providers, self.provider_problems = create_providers(self.provider_tables(), self.dropin_dir)
        self._provider_options = {name: provider_options(config.providers.get(name, {})) for name in self.providers}
        self._retired: list[Provider] = []  # replaced providers; a playing episode may still use one
        self.catalog: list[CatalogEntry] | None = None  # loaded by the Plugins menu
        self.groups: list[Group] = []
        self.build_groups()
        self.skip = SkipEngine(self.http, self.store)
        self.subtitles: SubtitleService | None = None
        self.subtitle_problems: dict[str, str] = {}  # why each subtitle source that isn't searched isn't
        self.build_subtitles()
        self.provider_subtitles = ProviderSubtitles(self.http, subs_dir() / "provider")
        self.cast_server: CastServer | None = None
        self.last_tv = ""  # the TV cast to last, offered first next time
        self._relay: LocalRelay | None = None  # started the first time a player needs it
        self._relay_lock = threading.Lock()
        self._serving = True

    # -- providers, groups and subtitles ----------------------------------------------

    def provider_tables(self) -> dict[str, dict[str, Any]]:
        """Config tables of providers that may start; a turned-off plugin's providers are left out."""
        hidden = {name for plugin in self.plugins.plugins() if not plugin.enabled for name in plugin.manifest.providers}
        return {name: table for name, table in self.config.providers.items() if name not in hidden}

    def reload_providers(self) -> None:
        """Re-create providers after plugins or settings changed; unchanged ones are kept."""
        current = {
            name: (provider, self._provider_options[name])
            for name, provider in self.providers.items() if name in self._provider_options
        }
        ready, self.provider_problems = create_providers(self.provider_tables(), self.dropin_dir, reuse=current)
        self._retired += [provider for name, provider in self.providers.items() if ready.get(name) is not provider]
        self.providers = ready
        self._provider_options = {name: provider_options(self.config.providers.get(name, {})) for name in ready}
        self.build_groups()
        self.build_subtitles()

    def build_groups(self) -> None:
        """Work out the search groups from the providers that are ready."""
        self.groups = build_groups(self.providers, self.config.providers, self.config.groups)

    def find_group(self, name: str) -> Group | None:
        return next((group for group in self.groups if group.name == name), None)

    def default_group(self) -> Group | None:
        """Where searches start: ``default_group``, else the first group with the older
        ``default_provider``, else the first group."""
        fallback = next((g.name for g in self.groups if self.config.default_provider in g.providers), "")
        for name in (self.config.default_group, fallback):
            found = self.find_group(name) if name else None
            if found is not None:
                return found
        return self.groups[0] if self.groups else None

    def build_subtitles(self) -> None:
        """The subtitle service over every source that's set up (none: no service)."""
        hidden = {name for plugin in self.plugins.plugins() if not plugin.enabled for name in plugin.manifest.subtitles}
        tables = {name: table for name, table in self.config.subtitles.sources.items() if name not in hidden}
        sources, self.subtitle_problems = create_sources(tables, self.http, subs_dir())
        self.subtitles = SubtitleService(sources, self.store, self.config.subtitles.languages) if sources else None

    def update_config(self, *changes: Change) -> bool:
        """Save ``changes`` to the config file and apply them; False (with a notice) if refused."""
        path = self.config.path or config_path()
        try:
            config = edit_config(path, *changes)
        except (ConfigError, OSError) as exc:
            self.on_notice(str(exc), title="Settings not saved", severity="error", timeout=8)
            return False
        self.config = config
        self.plugins.disabled = set(config.plugins.disabled)
        self.reload_providers()
        return True

    # -- playback ------------------------------------------------------------------------

    def audio_modes(self, provider_name: str) -> tuple[str, ...]:
        """SUB/DUB and the like: providers opt in with ``audio_modes`` and ``for_language``."""
        provider = self.providers[provider_name]
        if not callable(getattr(provider, "for_language", None)):
            return ()
        return tuple(getattr(provider, "audio_modes", ()))

    def default_audio_mode(self, provider_name: str) -> str | None:
        modes = self.audio_modes(provider_name)
        if not modes:
            return None
        return getattr(self.providers[provider_name], "language", modes[0])

    def playback_provider(self, provider_name: str, mode: str | None) -> Provider:
        """The provider bound to an audio mode, so later changes don't alter what's playing."""
        provider = self.providers[provider_name]
        return provider.for_language(mode) if mode is not None else provider

    def playback_folder(self) -> Path:
        folder = self.config.playback.folder
        return Path(folder).expanduser() if folder else downloads_dir()

    def storage(self) -> Storage:
        return Storage(self.config.playback.keep_hours, self.config.playback.max_mb)

    def local_relay(self) -> LocalRelay:
        """The relay on 127.0.0.1 that gives simpler players links they can open; started on first use."""
        with self._relay_lock:
            if self._relay is None:
                relay = LocalRelay(ffmpeg=find_ffmpeg())
                relay.start()
                self._relay = relay
            return self._relay

    # -- casting ---------------------------------------------------------------------------

    def start_cast(self, *, source_subtitles: bool = False) -> str:
        """Start the cast server from ``[cast]``; returns "" or, when it can't listen, why (also as a notice).

        With ``source_subtitles``, an episode whose provider has no subtitles gets them
        from the subtitle sources (OpenSubtitles and plugins), as playback on a PC does.
        """
        settings = self.config.cast
        try:
            # A chosen address or adapter is also the only one Cue listens on;
            # otherwise Cue listens on every adapter and announces the main one.
            address = settings.host or (adapter_address(settings.interface) if settings.interface else "")
        except AdapterError as exc:
            problem = f"Casting is unavailable: {exc}"
            self.on_notice(problem, title="Cast", severity="error", timeout=15)
            return problem
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
            on_status=lambda text: self.on_status(text),
            **({"find_subtitles": self._source_subtitles} if source_subtitles else {}),
        )
        try:
            server.start()
        except OSError as exc:
            server.stop()
            problem = (
                f"Casting is unavailable: cannot listen on {address or 'this PC'}, port {settings.port} ({exc}). "
                "Check [cast] host and port in the config."
            )
            self.on_notice(problem, title="Cast", severity="error", timeout=15)
            return problem
        self.cast_server = server
        return ""

    async def _source_subtitles(
        self, provider_name: str, show: Show, episode: Episode, stream: Stream,
    ) -> list[tuple[str, str]]:
        """``(language, path)`` from the subtitle sources, for a cast whose provider has none."""
        service = self.subtitles
        if service is None or not self.config.subtitles.auto_download:
            return []
        file_path = stream.url if is_local_file(stream.url) else None
        return await service.ensure(provider_name, show, episode, file_path=file_path)

    @staticmethod
    def airplay_credentials() -> Path:
        return app_dir() / "airplay.json"

    def cast_to_vlc(
        self, server: CastServer, provider_name: str, provider: Provider, show: Show, queue: list[Episode],
    ) -> str:
        """Publish a queue for VLC to open from its Network tab; returns its first item's link."""
        url = server.publish(provider_name, provider, show, queue)
        self.on_notice(
            f"On the TV, open VLC → Network → {server.name} → {show.title}.\nDirect link: {url}",
            title="Ready to cast", timeout=15,
        )
        count = len(queue)
        self.on_status(f"casting {show.title} ({count} item{'s' if count != 1 else ''}) to {server.base_url}")
        return url

    async def cast_to_tv(
        self, server: CastServer, device: devices.Device, provider_name: str, provider: Provider, show: Show,
        queue: list[Episode], *, ask_code: AskCode, start: int | None = None,
        playing: Callable[[int, devices.Media], None] | None = None,
    ) -> str | None:
        """Play a queue on a DLNA, AirPlay or Chromecast TV, moving on as each episode finishes.

        It starts at ``start``, or else at the episode with a resume point. ``ask_code``
        gets the code an AirPlay TV shows the first time; ``playing`` hears of each
        episode as the TV starts it. Returns why casting stopped early, if it did.
        """
        generation, index = server.publish_queue(provider_name, provider, show, queue)
        if start is not None:
            index = start
        credentials = self.airplay_credentials()
        receiver: devices.Receiver | None = None
        paired = False
        try:
            while index < server.queue_length(generation):
                try:
                    if device.needs_pairing and not paired:
                        raise devices.PairingNeeded(f"{device.name} wants a code first")
                    if receiver is None:
                        self.on_status(f"connecting to {device.name}…")
                        receiver = await devices.connect(device, credentials)
                    media = await server.media(generation, index)
                    self.on_notice(f"{media.title}", title=f"Playing on {device.name}", timeout=6)
                    self.on_status(f"casting {media.title} to {device.name}")
                    if playing is not None:
                        playing(index, media)
                    await receiver.play(media)
                except devices.PairingNeeded:
                    if paired:
                        raise
                    if receiver is not None:
                        await receiver.close()
                        receiver = None
                    paired = await self.pair(device, credentials, ask_code)
                    if not paired:
                        self.on_status("casting cancelled")
                        return "Casting cancelled"
                    continue
                if not server.finished(generation, index):
                    break  # stopped on the TV, or something else took over
                index += 1
            self.on_status(f"done casting to {device.name}")
            return None
        except (devices.DeviceError, ProviderError) as exc:
            self.on_notice(str(exc), title=f"Casting to {device.name} stopped", severity="error", timeout=12)
            self.on_status(f"casting to {device.name} stopped")
            return str(exc)
        finally:
            if receiver is not None:
                await receiver.close()

    async def pair(self, device: devices.Device, credentials: Path, ask_code: AskCode) -> bool:
        self.on_status(f"pairing with {device.name}: a code should appear on the TV")
        await devices.pair(device, credentials, ask_code)
        self.on_notice(f"Paired with {device.name}", title="Cast", timeout=6)
        return True

    # -- downloads -------------------------------------------------------------------------

    async def download(self, provider_name: str, show: Show, episodes: Sequence[Episode], provider: Provider) -> None:
        """Save episodes under ``downloads/<show>``; ``provider`` is already bound to an audio mode."""
        mode = getattr(provider, "language", None) if self.audio_modes(provider_name) else None
        suffix = f" [{mode.upper()}]" if mode else ""
        folder = self.config.downloads / safe_filename(show.title)
        saved = 0
        for position, episode in enumerate(episodes, start=1):
            prefix = f"downloading {position}/{len(episodes)}: episode {episode.label}{suffix}"
            self.on_status(prefix)
            try:
                stream = select_quality(await provider.stream(episode), self.config.quality)
                if stream is None:
                    raise DownloadError("no stream")
                name = f"{safe_filename(show.title)} - {episode.label.zfill(2)}{suffix}"
                await download(stream, folder, name, lambda percent, p=prefix: self.on_status(f"{p} {percent:.0f}%"))
                saved += 1
            except (ProviderError, DownloadError) as exc:
                self.on_notice(f"Episode {episode.label}: {exc}", title="Download", severity="error")
        if saved:
            self.on_status(f"saved {saved} episode{'s' if saved != 1 else ''} to {folder}")
        else:
            self.on_status("")

    # -- shutting down -----------------------------------------------------------------------

    def stop_serving(self) -> None:
        """Stop the cast server and the local relay, and tidy downloads that have had their time."""
        if not self._serving:
            return
        self._serving = False
        if self.cast_server is not None:
            self.cast_server.stop()
        if self._relay is not None:
            self._relay.stop()
        with contextlib.suppress(OSError):  # downloads for the Default app that have had their time
            tidy(self.playback_folder(), self.storage())

    async def aclose(self) -> None:
        self.stop_serving()
        for provider in [*self.providers.values(), *self._retired]:
            close = getattr(provider, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()
        await self.http.aclose()
        if self._owns_store:
            self.store.close()
