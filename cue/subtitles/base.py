"""SubtitleSource protocol and the track model.

Subtitles are a layer of their own, separate from video providers, so every show gets
captions the same way wherever its video comes from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from cue.providers.base import Episode, Show


class SubtitleError(Exception):
    """A subtitle source failed (network, quota, bad key)."""


@dataclass
class SubtitleTrack:
    language: str            # ISO 639-1, e.g. "en"
    label: str               # "English (SDH)", release name, etc.
    url: str | None = None   # remote file
    path: str | None = None  # already on disk
    source: str = ""         # which subtitle source found it
    id: str = ""             # the source's own ID for this file


@runtime_checkable
class SubtitleSource(Protocol):
    name: str

    async def find(
        self, show: Show, episode: Episode, languages: list[str], *, file_path: str | None = None
    ) -> list[SubtitleTrack]:
        """Best match first. ``file_path`` is the episode's file when it is on disk,
        which allows an exact (hash) match."""
        ...

    async def fetch(self, track: SubtitleTrack) -> str: ...  # returns local path
