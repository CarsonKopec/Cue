"""TOML configuration, stored at ``%APPDATA%\\cue\\config.toml``."""

from __future__ import annotations

import os
import re
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

# Where the Plugins menu finds plugins to install, unless [plugins] catalog says otherwise.
DEFAULT_CATALOG = "https://github.com/CarsonKopec/cue-plugins/releases/download/catalog/index.json"


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
    """Installed plugins, one folder each."""
    return app_dir() / "plugins"


def dropin_dir() -> Path:
    """Folder for single-file provider modules (``*.py``), the older way to add one."""
    return app_dir() / "providers"


def log_path() -> Path:
    return app_dir() / "cue.log"


@dataclass
class SubtitleConfig:
    languages: list[str] = field(default_factory=lambda: ["en"])
    auto_download: bool = True
    # Each subtitle source's [subtitles.<name>] table: OpenSubtitles, and any from plugins.
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def opensubtitles_api_key(self) -> str:
        return str(self.sources.get("opensubtitles", {}).get("api_key") or "")


@dataclass
class CastConfig:
    enabled: bool = False
    port: int = 8770
    name: str = "Cue"  # how the media server appears in VLC's Network tab
    host: str = ""  # this PC's address for the TV; detected when empty
    interface: str = ""  # or a network adapter by name, such as "Ethernet"


@dataclass
class PluginsConfig:
    catalog: str = DEFAULT_CATALOG  # an https:// address or a local index.json; empty turns it off
    disabled: list[str] = field(default_factory=list)


@dataclass
class PlaybackConfig:
    """Episodes Cue downloads for players that only open files (the Default app)."""

    folder: str = ""  # where they go; empty: the system's temporary folder
    quality: str = ""  # "720", "480", "worst" or "best"; empty: the main quality setting
    keep_hours: float = 0.0  # 0: deleted when you stop or move on; more keeps them for replays
    max_mb: int = 4096  # the most they may take together; the oldest go first; 0: no limit


@dataclass
class UpdatesConfig:
    check: bool = True  # cue.exe looks for a new Cue and new plugins once a day
    install: bool = True  # and installs them; false only says what's available


@dataclass
class Config:
    default_provider: str = "jellyfin"  # older configs; default_group replaced it
    default_group: str = ""  # the group searches start in; empty: the first one
    auto_skip: bool = True
    auto_next: bool = True
    quality: str = "best"
    player: str = "vlc"  # a registered player: vlc, mpv, or one a plugin adds
    player_args: list[str] = field(default_factory=list)
    download_dir: str = ""
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)  # [groups.<name>]: title, media
    players: dict[str, dict[str, Any]] = field(default_factory=dict)  # [players.<name>] options
    subtitles: SubtitleConfig = field(default_factory=SubtitleConfig)
    cast: CastConfig = field(default_factory=CastConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)
    playback: PlaybackConfig = field(default_factory=PlaybackConfig)
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
        "cast": {"enabled": False, "port": 8770},
    }


def save_document(path: Path, document: Mapping[str, Any], newline: str = "\n") -> None:
    """Write ``document`` with ``newline`` line endings (an edited file keeps its own)."""
    text = tomlkit.dumps(document).replace("\r\n", "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text.replace("\n", newline), encoding="utf-8", newline="")
    os.replace(temporary, path)


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
    player = _get(document, "player", str, defaults.player, "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", player):
        raise ConfigError("player must be a player name, such as 'vlc' or 'mpv'")
    providers = _get(document, "providers", dict, {}, "")
    for name, section in providers.items():
        if not isinstance(section, dict):
            raise ConfigError(f"[providers.{name}] must be a table")
        _get(section, "enabled", bool, True, f"providers.{name}.")
        if "groups" in section:
            _str_list(section, "groups", [], f"providers.{name}.")
    groups = _get(document, "groups", dict, {}, "")
    for name, section in groups.items():
        if not isinstance(section, dict) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", name):
            raise ConfigError(f"[groups.{name}] must be a table named with lowercase letters, digits, - or _")
        _get(section, "title", str, "", f"groups.{name}.")
        if section.get("media", "any") not in ("movie", "series", "any"):
            raise ConfigError(f"groups.{name}.media must be 'movie', 'series' or 'any'")
    players = _get(document, "players", dict, {}, "")
    for name, section in players.items():
        if not isinstance(section, dict):
            raise ConfigError(f"[players.{name}] must be a table")
        _get(section, "path", str, "", f"players.{name}.")

    subs = _get(document, "subtitles", dict, {}, "")
    opensubtitles = _get(subs, "opensubtitles", dict, {}, "subtitles.")
    _get(opensubtitles, "api_key", str, "", "subtitles.opensubtitles.")
    for name, section in subs.items():
        if isinstance(section, dict) and not isinstance(section.get("enabled", True), bool):
            raise ConfigError(f"subtitles.{name}.enabled must be true or false")
    cast = _get(document, "cast", dict, {}, "")
    cast_port = _get(cast, "port", int, CastConfig.port, "cast.")
    if isinstance(cast_port, bool) or not 0 < cast_port < 65536:
        raise ConfigError("cast.port must be a port number between 1 and 65535")
    cast_host = _get(cast, "host", str, "", "cast.").strip()
    cast_interface = _get(cast, "interface", str, "", "cast.").strip()
    if cast_host and cast_interface:
        raise ConfigError("set either cast.host or cast.interface, not both")
    plugins = _get(document, "plugins", dict, {}, "")
    updates = _get(document, "updates", dict, {}, "")
    playback = _get(document, "playback", dict, {}, "")
    keep_hours = _get(playback, "keep_hours", (int, float), 0, "playback.")
    max_mb = _get(playback, "max_mb", int, PlaybackConfig.max_mb, "playback.")
    if isinstance(keep_hours, bool) or keep_hours < 0 or isinstance(max_mb, bool) or max_mb < 0:
        raise ConfigError("playback.keep_hours and playback.max_mb can't be negative")

    return Config(
        default_provider=_get(document, "default_provider", str, defaults.default_provider, ""),
        default_group=_get(document, "default_group", str, "", "").strip().lower(),
        groups={name: dict(section) for name, section in groups.items()},
        auto_skip=_get(document, "auto_skip", bool, defaults.auto_skip, ""),
        auto_next=_get(document, "auto_next", bool, defaults.auto_next, ""),
        quality=str(_get(document, "quality", (str, int), defaults.quality, "")),
        player=player,
        player_args=_str_list(document, "player_args", defaults.player_args, ""),
        download_dir=_get(document, "download_dir", str, "", ""),
        providers={name: dict(section) for name, section in providers.items()},
        players={name: dict(section) for name, section in players.items()},
        subtitles=SubtitleConfig(
            languages=[lang.lower() for lang in _str_list(subs, "languages", ["en"], "subtitles.")],
            auto_download=_get(subs, "auto_download", bool, True, "subtitles."),
            sources={name: dict(section) for name, section in subs.items() if isinstance(section, dict)},
        ),
        cast=CastConfig(
            enabled=_get(cast, "enabled", bool, False, "cast."),
            port=cast_port,
            name=_get(cast, "name", str, CastConfig.name, "cast.") or CastConfig.name,
            host=cast_host,
            interface=cast_interface,
        ),
        plugins=PluginsConfig(
            catalog=_get(plugins, "catalog", str, DEFAULT_CATALOG, "plugins.").strip(),
            disabled=_str_list(plugins, "disabled", [], "plugins."),
        ),
        updates=UpdatesConfig(
            check=_get(updates, "check", bool, True, "updates."),
            install=_get(updates, "install", bool, True, "updates."),
        ),
        playback=PlaybackConfig(
            folder=_get(playback, "folder", str, "", "playback.").strip(),
            quality=str(_get(playback, "quality", (str, int), "", "playback.")).strip(),
            keep_hours=float(keep_hours),
            max_mb=max_mb,
        ),
        path=path,
    )


# -- editing ---------------------------------------------------------------------
# The Plugins menu changes the file in place with tomlkit, so comments and layout survive.

Change = Callable[[tomlkit.TOMLDocument], None]


def edit_config(path: Path, *changes: Change) -> Config:
    """Apply ``changes`` to the config file and return the new config.

    The file is rewritten only when the result is still a valid config; otherwise
    :class:`ConfigError` is raised and the file is left as it was.
    """
    newline = "\n"
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:  # newline="": see the file's own line endings
            text = f.read()
        newline = "\r\n" if "\r\n" in text else "\n"
        try:
            document = tomlkit.parse(text)
        except TOMLKitError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
    else:
        document = tomlkit.document()
        document.update(default_document())
    for change in changes:
        change(document)
    config = parse_config(document.unwrap(), path)
    save_document(path, document, newline)
    return config


def _table(document: tomlkit.TOMLDocument, *keys: str) -> tuple[Any, bool]:
    """The table at ``keys`` and whether it had to be created."""
    current: Any = document
    created = False
    for key in keys:
        if key not in current:
            current[key] = tomlkit.table()
            created = True
        current = current[key]
    return current, created


def _end_new_table(table: Any, created: bool) -> None:
    if created:
        table.add(tomlkit.nl())  # a blank line before whatever table follows


def _set_options(section: str, name: str, values: Mapping[str, Any]) -> Change:
    def change(document: tomlkit.TOMLDocument) -> None:
        table, created = _table(document, section, name)
        for key, value in values.items():
            if value is None:
                if key in table:
                    del table[key]
            else:
                table[key] = value
        _end_new_table(table, created)
    return change


def set_provider_options(name: str, values: Mapping[str, Any]) -> Change:
    """Set keys of ``[providers.<name>]``; a value of ``None`` removes the key."""
    return _set_options("providers", name, values)


def set_player_options(name: str, values: Mapping[str, Any]) -> Change:
    """Set keys of ``[players.<name>]``; a value of ``None`` removes the key."""
    return _set_options("players", name, values)


def set_subtitle_options(name: str, values: Mapping[str, Any]) -> Change:
    """Set keys of ``[subtitles.<name>]`` (a subtitle source); a value of ``None`` removes the key."""
    return _set_options("subtitles", name, values)


def set_subtitle_enabled(name: str, enabled: bool) -> Change:
    """Turn a subtitle source on (dropping ``enabled = false``) or off."""
    return _set_options("subtitles", name, {"enabled": None if enabled else False})


def set_player(name: str) -> Change:
    def change(document: tomlkit.TOMLDocument) -> None:
        document["player"] = name
    return change


def set_provider_enabled(name: str, enabled: bool) -> Change:
    """Turn a provider on (creating its table if needed) or off (``enabled = false``)."""
    def change(document: tomlkit.TOMLDocument) -> None:
        table, created = _table(document, "providers", name)
        if enabled:
            if "enabled" in table:
                del table["enabled"]
        else:
            table["enabled"] = False
        _end_new_table(table, created)
    return change


def set_default_group(name: str) -> Change:
    def change(document: tomlkit.TOMLDocument) -> None:
        document["default_group"] = name
    return change


def set_provider_groups(name: str, groups: list[str], *, exists: bool) -> Change:
    """Put a provider in exactly ``groups``. A provider without a table yet gets one that
    stays off (``enabled = false``), so choosing its groups doesn't turn it on."""
    values: dict[str, Any] = {"groups": list(groups)}
    if not exists:
        values["enabled"] = False
    return _set_options("providers", name, values)


def set_group_title(name: str, title: str) -> Change:
    return _set_options("groups", name, {"title": title})


def remove_group(name: str) -> Change:
    """Delete ``[groups.<name>]`` and take the group out of every provider's list."""
    def change(document: tomlkit.TOMLDocument) -> None:
        if "groups" in document and name in document["groups"]:
            del document["groups"][name]
            if not document["groups"]:
                del document["groups"]
        for table in document.get("providers", {}).values():
            if name in table.get("groups", []):
                table["groups"] = [group for group in table["groups"] if group != name]
        if document.get("default_group") == name:
            del document["default_group"]
    return change


def set_plugin_disabled(name: str, disabled: bool) -> Change:
    def change(document: tomlkit.TOMLDocument) -> None:
        table, created = _table(document, "plugins")
        current = [str(item) for item in table.get("disabled", [])]
        table["disabled"] = sorted({*current, name}) if disabled else [item for item in current if item != name]
        _end_new_table(table, created)
    return change
