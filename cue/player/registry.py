"""Player registry: the built-in VLC and mpv players, and any that plugins add.

A plugin adds a player by registering a :class:`PlayerBackend`::

    from cue.player.registry import PlayerBackend, register_player

    register_player(PlayerBackend(
        name="myplayer", title="My Player",
        locate=lambda options: shutil.which("myplayer"),
        create=lambda program, options: MyPlayer(program),
    ))

``create`` returns an object with the :class:`cue.player.base.Player` methods.
:class:`cue.player.process.ProcessPlayer` covers players that Cue can start but not
control. The config's ``player`` key picks one by name, and ``[players.<name>]``
holds its options; ``path`` there overrides where the program is.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cue.origins import current
from cue.player.base import Player
from cue.player.locate import find_mpv, find_vlc
from cue.providers.base import Setting

PATH_SETTING = Setting("path", "Program", help="Leave empty to find it automatically.")


@dataclass(frozen=True)
class PlayerBackend:
    name: str
    title: str
    locate: Callable[[Mapping[str, Any]], str | None]  # the program's path, or None when it isn't installed
    create: Callable[[str, Mapping[str, Any]], Player]  # (program path, [players.<name>] table) -> a player
    missing: str = ""  # how to get it, shown when it can't be found
    description: str = ""
    controls: bool = True  # False: Cue can only start it, so there's no auto-skip and no position to resume
    platforms: tuple[str, ...] = ()  # sys.platform values it runs on; empty means all
    settings: tuple[Setting, ...] = ()  # besides "path"
    has_program: bool = True  # False for players with no program to point at, such as the system's default app
    # What it can open, so Cue knows when to step in (see cue.player.relay):
    headers: frozenset[str] | None = None  # lower-case HTTP headers it can send; None: it checks itself
    hls: bool = True  # plays HLS (.m3u8) links; otherwise Cue hands it one continuous stream
    links: bool = True  # opens web links at all; otherwise Cue downloads the episode first

    @property
    def supported(self) -> bool:
        return not self.platforms or sys.platform in self.platforms

    @property
    def all_settings(self) -> tuple[Setting, ...]:
        """Its settings form: the program's path when it has one, then its own."""
        return ((PATH_SETTING,) if self.has_program else ()) + self.settings

    def program(self, options: Mapping[str, Any]) -> str | None:
        """The configured ``path`` if it exists, else whatever ``locate`` finds."""
        configured = str(options.get("path") or "").strip().strip('"')
        if configured and self.has_program:
            return configured if Path(configured).expanduser().is_file() else None
        return self.locate(options)


_players: dict[str, PlayerBackend] = {}
_origins: dict[str, str] = {}


def register_player(backend: PlayerBackend) -> PlayerBackend:
    if not backend.name or not isinstance(backend.name, str):
        raise TypeError("a player needs a non-empty name")
    _players[backend.name] = backend
    _origins[backend.name] = current()
    return backend


def players() -> dict[str, PlayerBackend]:
    return dict(_players)


def player_origin(name: str) -> str | None:
    return _origins.get(name)


def players_registered_by(origin: str) -> list[str]:
    return sorted(name for name, source in _origins.items() if source == origin)


def unregister_players(origin: str) -> list[str]:
    names = players_registered_by(origin)
    for name in names:
        _players.pop(name, None)
        _origins.pop(name, None)
    return names


def _vlc(program: str, options: Mapping[str, Any]) -> Player:
    from cue.player.vlc import VlcPlayer

    return VlcPlayer(program)


def _mpv(program: str, options: Mapping[str, Any]) -> Player:
    from cue.player.mpv import MpvPlayer

    return MpvPlayer(program)


register_player(PlayerBackend(
    "vlc", "VLC", lambda options: find_vlc(), _vlc,
    missing="Install VLC from videolan.org, put vlc.exe next to cue.exe, or set its path here.",
    description="Full control: resume, auto-skip, subtitles. Sources that need other headers, such as "
                "Jellyfin's login, play through Cue's local relay.",
    headers=frozenset({"referer", "user-agent"}),
))
register_player(PlayerBackend(
    "mpv", "mpv", lambda options: find_mpv(), _mpv,
    missing="Put mpv.exe next to cue.exe, add it to PATH, or set its path here.",
    description="Full control, and any HTTP header a source needs (Jellyfin's login, for one).",
))


def _other(program: str, options: Mapping[str, Any]) -> Player:
    from cue.player.command import CommandPlayer

    return CommandPlayer(program, options)


register_player(PlayerBackend(
    "other", "Another program", lambda options: None, _other,
    missing="Set the player's program in the Players tab (Enter).",
    description="Any video player: Cue runs it with a link it can open, without the headers a source needs. "
                "Cue can't skip intros or save your place in it.",
    controls=False,
    settings=(
        Setting("arguments", "Arguments", default='"{url}"',
                help="{url} is the link; {title}, {start} (seconds) and {start_ms} are also filled in."),
        Setting("hls", "It plays HLS (.m3u8) links", "bool", True,
                help="Off: Cue sends one continuous stream instead (needs FFmpeg for most sources)."),
    ),
    headers=frozenset(),
))
