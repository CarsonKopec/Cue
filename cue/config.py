"""TOML configuration, stored at ``%APPDATA%\\cue\\config.toml``."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w


class ConfigError(Exception):
    """The config file exists but cannot be used."""


def app_dir() -> Path:
    """Directory holding the config, database and caches.

    ``CUE_HOME`` overrides it, which is handy for portable installs and tests.
    """
    override = os.environ.get("CUE_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
        return Path(base) / "cue"
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "cue"


def config_path() -> Path:
    return app_dir() / "config.toml"


def db_path() -> Path:
    return app_dir() / "cue.db"


def subs_dir() -> Path:
    return app_dir() / "subs"


def plugins_dir() -> Path:
    """Drop-in folder for extra provider modules."""
    return app_dir() / "providers"


def log_path() -> Path:
    return app_dir() / "cue.log"


@dataclass
class SubtitleConfig:
    languages: list[str] = field(default_factory=lambda: ["en"])
    auto_download: bool = True
    opensubtitles_api_key: str = ""


@dataclass
class Config:
    default_provider: str = "jellyfin"
    auto_skip: bool = True
    auto_next: bool = True
    quality: str = "best"
    player: str = "vlc"
    player_args: list[str] = field(default_factory=list)
    download_dir: str = ""
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    subtitles: SubtitleConfig = field(default_factory=SubtitleConfig)
    path: Path | None = None

    @property
    def downloads(self) -> Path:
        if self.download_dir:
            return Path(self.download_dir).expanduser()
        return Path.home() / "Videos" / "Cue"


def default_document() -> dict[str, Any]:
    """The config written on first run. Empty provider settings mean "not set up yet"."""
    return {
        "default_provider": "jellyfin",
        "auto_skip": True,
        "auto_next": True,
        "quality": "best",
        "player": "vlc",
        "player_args": [],
        "download_dir": "",
        "providers": {
            "jellyfin": {"url": "", "api_key": ""},
            "local": {"path": ""},
        },
        "subtitles": {
            "languages": ["en"],
            "auto_download": True,
            "opensubtitles": {"api_key": ""},
        },
    }


def save_document(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        tomli_w.dump(document, f)


def load_config(path: Path | None = None, *, create: bool = True) -> Config:
    """Read the config file, writing the defaults first if it does not exist."""
    path = path or config_path()
    if path.exists():
        try:
            with path.open("rb") as f:
                document = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
    else:
        document = default_document()
        if create:
            save_document(path, document)
    return parse_config(document, path)


def _get(table: dict[str, Any], key: str, kind: type | tuple[type, ...], default: Any, where: str) -> Any:
    value = table.get(key, default)
    if not isinstance(value, kind):
        raise ConfigError(f"{where}{key} has the wrong type ({type(value).__name__})")
    return value


def _str_list(table: dict[str, Any], key: str, default: list[str], where: str) -> list[str]:
    value = _get(table, key, list, default, where)
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}{key} must be a list of strings")
    return list(value)


def parse_config(document: dict[str, Any], path: Path | None = None) -> Config:
    defaults = Config()
    player = _get(document, "player", str, defaults.player, "").lower()
    if player not in {"vlc", "mpv"}:
        raise ConfigError("player must be 'vlc' or 'mpv'")
    providers = _get(document, "providers", dict, {}, "")
    for name, section in providers.items():
        if not isinstance(section, dict):
            raise ConfigError(f"[providers.{name}] must be a table")

    subs = _get(document, "subtitles", dict, {}, "")
    opensubtitles = _get(subs, "opensubtitles", dict, {}, "subtitles.")

    return Config(
        default_provider=_get(document, "default_provider", str, defaults.default_provider, ""),
        auto_skip=_get(document, "auto_skip", bool, defaults.auto_skip, ""),
        auto_next=_get(document, "auto_next", bool, defaults.auto_next, ""),
        quality=str(_get(document, "quality", (str, int), defaults.quality, "")),
        player=player,
        player_args=_str_list(document, "player_args", defaults.player_args, ""),
        download_dir=_get(document, "download_dir", str, "", ""),
        providers={name: dict(section) for name, section in providers.items()},
        subtitles=SubtitleConfig(
            languages=[lang.lower() for lang in _str_list(subs, "languages", ["en"], "subtitles.")],
            auto_download=_get(subs, "auto_download", bool, True, "subtitles."),
            opensubtitles_api_key=_get(opensubtitles, "api_key", str, "", "subtitles.opensubtitles."),
        ),
        path=path,
    )
