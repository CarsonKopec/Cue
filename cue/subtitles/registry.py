"""Where subtitles come from: OpenSubtitles built in, more from plugins.

A source registers a :class:`SubtitleBackend`. Cue creates the source with the settings
in ``[subtitles.<name>]`` (an API key, say), the app's HTTP client and the folder for
subtitle files, and searches every source that's set up at once; the subtitle picker
says which one found each file. ``enabled = false`` in a source's table turns it off.

A plugin lists its sources in ``plugin.toml`` (``subtitles = ["subdl"]``) and registers
them when imported::

    from cue.subtitles.registry import SubtitleBackend, register_subtitles

    register_subtitles(SubtitleBackend("subdl", "SubDL", create=lambda options, http, folder: SubDL(...)))
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from cue.origins import BUILT_IN, current
from cue.providers.base import Setting
from cue.subtitles.base import SubtitleSource

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubtitleBackend:
    """A kind of subtitle source, before it's set up."""

    name: str  # its [subtitles.<name>] table, and the source name on tracks it finds
    title: str  # for people: "SubDL"
    # (its settings, the app's HTTP client, the folder for subtitle files) -> the source
    create: Callable[[Mapping[str, Any], httpx.AsyncClient, Path], SubtitleSource]
    description: str = ""  # what it covers, for the plugins menu
    settings: tuple[Setting, ...] = ()  # those with required=True must be filled in before it's searched
    homepage: str = ""  # where to sign up or get a key

    def missing(self, options: Mapping[str, Any]) -> list[Setting]:
        """Required settings ``options`` leaves empty."""
        return [setting for setting in self.settings
                if setting.required and not str(options.get(setting.key) or "").strip()]


_backends: dict[str, SubtitleBackend] = {}
_origins: dict[str, str] = {}


def register_subtitles(backend: SubtitleBackend, *, origin: str | None = None) -> SubtitleBackend:
    if not backend.name or not isinstance(backend.name, str):
        raise TypeError("a subtitle source needs a non-empty name")
    _backends[backend.name] = backend
    _origins[backend.name] = origin or current()
    return backend


def subtitle_backends() -> dict[str, SubtitleBackend]:
    """Every registered source: built-in ones first, then the rest by name."""
    return dict(sorted(_backends.items(), key=lambda item: (_origins.get(item[0]) != BUILT_IN, item[0])))


def subtitle_origin(name: str) -> str | None:
    return _origins.get(name)


def subtitles_registered_by(origin: str) -> list[str]:
    return sorted(name for name, source in _origins.items() if source == origin)


def unregister_subtitles(origin: str) -> list[str]:
    names = subtitles_registered_by(origin)
    for name in names:
        _backends.pop(name, None)
        _origins.pop(name, None)
    return names


def is_on(options: Mapping[str, Any]) -> bool:
    return options.get("enabled", True) is not False


def create_sources(
    tables: Mapping[str, Mapping[str, Any]], http: httpx.AsyncClient, folder: Path,
) -> tuple[list[SubtitleSource], dict[str, str]]:
    """The sources ready to search, in order, and why each other one isn't."""
    sources: list[SubtitleSource] = []
    problems: dict[str, str] = {}
    for name, backend in subtitle_backends().items():
        options = tables.get(name, {})
        if not is_on(options):
            problems[name] = "off"
            continue
        missing = backend.missing(options)
        if missing:
            problems[name] = "needs " + ", ".join(setting.label.lower() for setting in missing)
            continue
        try:
            sources.append(backend.create(options, http, folder))
        except Exception as exc:  # a plugin's bug must not stop Cue
            log.warning("Subtitle source %s couldn't start: %s", name, exc)
            problems[name] = f"couldn't start: {exc}"
    return sources, problems


def _opensubtitles(options: Mapping[str, Any], http: httpx.AsyncClient, folder: Path) -> SubtitleSource:
    from cue.subtitles.opensubtitles import OpenSubtitles

    return OpenSubtitles(str(options["api_key"]).strip(), http, folder)


register_subtitles(SubtitleBackend(
    "opensubtitles", "OpenSubtitles", _opensubtitles,
    description="Movies and TV in many languages, with exact matches for episodes saved on this PC. "
                "The free tier allows a few downloads a day; Cue keeps every file it gets.",
    settings=(Setting("api_key", "API key", "secret", required=True,
                      help="Free: sign up at opensubtitles.com, then Profile -> API consumers -> New consumer."),),
    homepage="https://www.opensubtitles.com/consumers",
), origin=BUILT_IN)
