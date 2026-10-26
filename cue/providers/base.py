"""Provider protocol and the data models every media source speaks."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


def format_number(number: float) -> str:
    """Episode number for display and file names: ``3`` or ``12.5``."""
    return str(int(number)) if float(number).is_integer() else f"{number:g}"


def is_local_file(url: str) -> bool:
    return "://" not in url and os.path.isfile(url)


@dataclass
class Show:
    id: str
    title: str
    year: int | None = None
    episode_count: int | None = None
    mal_id: int | None = None      # if the provider already knows it
    # Other database IDs from the provider's metadata, e.g. {"AniList": "21"}; used to find the MAL ID.
    external_ids: dict[str, str] = field(default_factory=dict)
    # Movies still use a one-item playback queue, but subtitle searches must not
    # mistake that item for a television episode.
    media_type: str = "episode"
    # The provider's explicit parent-series title, when the display title also
    # includes a season label. Never inferred by removing words from a title.
    series_title: str | None = None


@dataclass
class Episode:
    show_id: str
    number: float                  # float to allow 12.5 specials
    title: str | None = None
    season: int | None = None      # when the source knows it; helps subtitle matching

    @property
    def label(self) -> str:
        return format_number(self.number)


@dataclass
class StreamSubtitle:
    """An explicit subtitle track advertised alongside a provider's media."""

    url: str
    language: str = ""
    label: str = ""
    headers: dict[str, str] | None = None


@dataclass
class Stream:
    url: str
    headers: dict[str, str] | None = None
    # Legacy providers may supply plain subtitle URLs; new providers retain metadata.
    subtitles: list[str] | None = None
    quality: str | None = None
    subtitle_tracks: list[StreamSubtitle] | None = None


class ProviderError(Exception):
    """A provider could not talk to its source or find what was asked for."""


class ProviderConfigError(ProviderError):
    """The provider's ``[providers.<name>]`` table is missing required settings."""


@runtime_checkable
class Provider(Protocol):
    """A media source.

    Implementations are constructed with their ``[providers.<name>]`` config table and
    raise :class:`ProviderConfigError` when it is incomplete. They may also define
    ``async def aclose()``, which is called when Cue exits.
    Set ``skip_supported = False`` to opt out of anime-specific skip lookups.
    """

    name: str
    async def search(self, query: str) -> list[Show]: ...
    async def episodes(self, show: Show) -> list[Episode]: ...
    async def stream(self, episode: Episode) -> list[Stream]: ...
