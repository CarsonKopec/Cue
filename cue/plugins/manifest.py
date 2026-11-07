"""``plugin.toml``: what a plugin is called, its version and the providers and players it adds."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cue import __version__

MANIFEST = "plugin.toml"
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,39}")
_VERSION = re.compile(r"\d{1,6}(?:\.\d{1,6}){0,3}")


class PluginError(Exception):
    """A plugin is invalid, could not be fetched, or could not be installed."""


@dataclass(frozen=True)
class Manifest:
    """The ``[plugin]`` table of a plugin's ``plugin.toml``."""

    name: str
    version: str
    title: str
    description: str = ""
    author: str = ""
    homepage: str = ""
    requires: str = "0"  # the oldest Cue version it works with
    providers: tuple[str, ...] = ()
    players: tuple[str, ...] = ()

    @property
    def module(self) -> str:
        """The plugin's Python package name under ``cue_plugins``."""
        return self.name.replace("-", "_")


def version_key(version: str) -> tuple[int, ...]:
    """``"1.2"`` -> ``(1, 2, 0, 0)``, for comparing versions."""
    if not _VERSION.fullmatch(version):
        raise PluginError(f"{version!r} is not a version number like 1.2.0")
    parts = [int(part) for part in version.split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def is_newer(version: str, than: str) -> bool:
    return version_key(version) > version_key(than)


def _text(table: Mapping[str, Any], key: str, where: str, default: str | None = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise PluginError(f"{where}: {key} must be text" if key in table else f"{where}: {key} is missing")
    return value.strip()


def parse_manifest(table: Mapping[str, Any], where: str = MANIFEST) -> Manifest:
    """Check a ``[plugin]`` table (or a catalog entry with the same keys)."""
    name = _text(table, "name", where)
    if not _NAME.fullmatch(name):
        raise PluginError(f"{where}: name must be lowercase letters, digits, - or _, starting with a letter")
    version = _text(table, "version", where)
    version_key(version)
    requires = _text(table, "requires", where, "0")
    version_key(requires)
    providers, players = table.get("providers", []), table.get("players", [])
    for key, names in (("providers", providers), ("players", players)):
        if not isinstance(names, list) or not all(isinstance(item, str) and item for item in names):
            raise PluginError(f"{where}: {key} must be a list of names")
    homepage = _text(table, "homepage", where, "")
    if homepage and not homepage.startswith(("https://", "http://")):
        raise PluginError(f"{where}: homepage must be a web address")
    return Manifest(
        name=name,
        version=version,
        title=_text(table, "title", where, "") or name,
        description=_text(table, "description", where, ""),
        author=_text(table, "author", where, ""),
        homepage=homepage,
        requires=requires,
        providers=tuple(providers),
        players=tuple(players),
    )


def read_manifest(folder: Path) -> Manifest:
    path = folder / MANIFEST
    try:
        with path.open("rb") as f:
            document = tomllib.load(f)
    except FileNotFoundError:
        raise PluginError(f"{folder.name} has no {MANIFEST}") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PluginError(f"{path}: {exc}") from exc
    table = document.get("plugin")
    if not isinstance(table, dict):
        raise PluginError(f"{path}: the [plugin] table is missing")
    return parse_manifest(table, str(path))


def check_compatible(manifest: Manifest) -> None:
    if is_newer(manifest.requires, __version__):
        raise PluginError(f"{manifest.name} needs Cue {manifest.requires} or newer; this is Cue {__version__}")
