"""Subtitle flow: SQLite cache -> OpenSubtitles search -> download -> record.

Every download is cached, so a subtitle file is never fetched twice (the free
OpenSubtitles tier has a daily quota).
"""

from __future__ import annotations

import os

from cue.providers.base import Episode, Show
from cue.store import Store
from cue.subtitles.base import SubtitleError, SubtitleSource, SubtitleTrack

__all__ = ["SubtitleError", "SubtitleService", "SubtitleSource", "SubtitleTrack"]


class SubtitleService:
    def __init__(self, source: SubtitleSource, store: Store, languages: list[str]) -> None:
        self.source = source
        self.languages = languages or ["en"]
        self._store = store

    async def ensure(
        self, provider: str, show: Show, episode: Episode, *, file_path: str | None = None
    ) -> list[tuple[str, str]]:
        """``(language, path)`` for each configured language that has subtitles, in config order."""
        found: dict[str, str] = {}
        for language in self.languages:
            cached = self._store.get_subtitle(provider, show.id, episode.number, language)
            if cached and os.path.isfile(cached.path):
                found[language] = cached.path

        missing = [language for language in self.languages if language not in found]
        if missing:
            tracks = await self.source.find(show, episode, missing, file_path=file_path)
            for language in missing:
                track = next((t for t in tracks if t.language == language), None)
                if track is not None:
                    found[language] = await self._download(provider, show, episode, track)
        return [(language, found[language]) for language in self.languages if language in found]

    async def search(self, show: Show, episode: Episode, *, file_path: str | None = None) -> list[SubtitleTrack]:
        return await self.source.find(show, episode, self.languages, file_path=file_path)

    async def choose(self, provider: str, show: Show, episode: Episode, track: SubtitleTrack) -> str:
        """Use ``track`` for this episode from now on; reuses the file if it is the cached one."""
        cached = self._store.get_subtitle(provider, show.id, episode.number, track.language)
        if cached and cached.source == _source_key(track) and os.path.isfile(cached.path):
            return cached.path
        return await self._download(provider, show, episode, track)

    async def _download(self, provider: str, show: Show, episode: Episode, track: SubtitleTrack) -> str:
        path = await self.source.fetch(track)
        self._store.set_subtitle(provider, show.id, episode.number, track.language, _source_key(track), path)
        return path


def _source_key(track: SubtitleTrack) -> str:
    return f"{track.source}:{track.id}"
