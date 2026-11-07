"""Installing what a person asks for: a catalog name, a link to a zip, a zip file or a folder."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from cue.config import Change, Config, set_player, set_plugin_disabled, set_provider_enabled
from cue.player.registry import players
from cue.plugins.catalog import CatalogEntry, check_address, fetch_archive, is_web_address
from cue.plugins.manager import InstalledPlugin
from cue.plugins.manifest import PluginError, is_newer

WARNING = (
    "Plugins are Python code that runs on this PC with your account's access. "
    "Install only plugins you trust."
)


@dataclass(frozen=True)
class Source:
    """Where a plugin would come from, known before anything is downloaded."""

    location: str  # shown to the person before they confirm
    entry: CatalogEntry | None = None

    @property
    def title(self) -> str:
        if self.entry is not None:
            return f"{self.entry.manifest.title} {self.entry.manifest.version}"
        return Path(self.location).name if not is_web_address(self.location) else self.location


def find_source(text: str, catalog: list[CatalogEntry] | None) -> Source:
    """Read what was typed: a catalog name first, then an https:// link, then a local path."""
    text = text.strip().strip('"')
    if not text:
        raise PluginError("type a plugin name, a link to a .zip, or a path")
    for entry in catalog or []:
        if entry.manifest.name == text.lower():
            return Source(entry.url, entry)
    if is_web_address(text):
        check_address(text)
        return Source(text)
    path = Path(text).expanduser()
    if path.exists():
        return Source(str(path.resolve()))
    if catalog is None:
        raise PluginError(f"no file or folder {text!r}, and the catalog couldn't be loaded")
    raise PluginError(f"no plugin called {text!r} in the catalog, and no such file or folder")


async def fetch(http: httpx.AsyncClient, source: Source) -> bytes | Path:
    """The plugin's files: downloaded (and checked against the catalog), or the local path."""
    if source.entry is not None:
        return await fetch_archive(http, source.entry.url, source.entry.sha256)
    if is_web_address(source.location):
        return await fetch_archive(http, source.location)
    return Path(source.location)


def after_install(plugin: InstalledPlugin, config: Config) -> list[Change]:
    """Config changes that put a newly installed plugin to use: its providers turned on,
    the plugin itself on, and its player chosen when the configured one is missing."""
    changes = [set_provider_enabled(name, True) for name in plugin.providers]
    if plugin.name in config.plugins.disabled:
        changes.append(set_plugin_disabled(plugin.name, False))
    if plugin.players and config.player not in players():
        changes.append(set_player(plugin.players[0]))
    return changes


def after_remove(plugin: InstalledPlugin, provided: list[str], config: Config) -> list[Change]:
    """Config changes after removing a plugin: its providers off (settings kept), and VLC
    again if its player was in use."""
    names = set(provided) | set(plugin.manifest.providers)
    changes = [set_provider_enabled(name, False) for name in sorted(names) if name in config.providers]
    if config.player not in players():
        changes.append(set_player("vlc"))
    return changes


def updates(installed: list[InstalledPlugin], catalog: list[CatalogEntry]) -> dict[str, CatalogEntry]:
    """Catalog entries newer than the installed plugin of the same name."""
    by_name = {entry.manifest.name: entry for entry in catalog}
    return {
        plugin.name: by_name[plugin.name]
        for plugin in installed
        if plugin.valid and plugin.name in by_name
        and is_newer(by_name[plugin.name].manifest.version, plugin.manifest.version)
    }
