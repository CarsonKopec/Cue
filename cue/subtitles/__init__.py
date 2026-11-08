"""Subtitle flow: SQLite cache -> every subtitle source at once -> download -> record.

Sources are OpenSubtitles and any a plugin adds (see :mod:`cue.subtitles.registry`).
They're searched together, best source first, and one that fails doesn't stop the
others. Every download is cached, so a subtitle file is never fetched twice (the free
OpenSubtitles tier has a daily quota).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence

from cue.providers.base import Episode, Show
from cue.store import Store
from cue.subtitles.base import SubtitleError, SubtitleSource, SubtitleTrack

__all__ = ["SubtitleError", "SubtitleService", "SubtitleSource", "SubtitleTrack"]

log = logging.getLogger(__name__)


class SubtitleService:
    def __init__(self, sources: SubtitleSource | Sequence[SubtitleSource], store: Store, languages: list[str]) -> None:
        self.sources = [sources] if isinstance(sources, SubtitleSource) else list(sources)
        if not self.sources:
            raise ValueError("a subtitle service needs at least one source")
        self.languages = languages or ["en"]
        self._store = store

    @property
    def source(self) -> SubtitleSource:
        """The first (preferred) source."""
        return self.sources[0]

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
            tracks = await self._find(show, episode, missing, file_path)
            failures: list[SubtitleError] = []
            for language in missing:
                for track in (t for t in tracks if t.language == language):
                    try:
                        found[language] = await self._download(provider, show, episode, track)
                        break
                    except SubtitleError as exc:  # a quota or a dead file: the next match may work
                        log.info("Subtitle from %s failed: %s", track.source, exc)
                        failures.append(exc)
            if failures and not any(language in found for language in missing):
                raise failures[0]  # nothing worked: say why (an OpenSubtitles quota, say)
        return [(language, found[language]) for language in self.languages if language in found]

    async def search(self, show: Show, episode: Episode, *, file_path: str | None = None) -> list[SubtitleTrack]:
        return await self._find(show, episode, self.languages, file_path)

    async def choose(self, provider: str, show: Show, episode: Episode, track: SubtitleTrack) -> str:
        """Use ``track`` for this episode from now on; reuses the file if it is the cached one."""
        cached = self._store.get_subtitle(provider, show.id, episode.number, track.language)
        if cached and cached.source == _source_key(track) and os.path.isfile(cached.path):
            return cached.path
        return await self._download(provider, show, episode, track)

    async def _find(
        self, show: Show, episode: Episode, languages: list[str], file_path: str | None,
    ) -> list[SubtitleTrack]:
        """Every source's matches, in source order; an error only if every source failed."""
        results = await asyncio.gather(
            *(source.find(show, episode, languages, file_path=file_path) for source in self.sources),
            return_exceptions=True,
        )
        tracks: list[SubtitleTrack] = []
        problems: list[str] = []
        for source, result in zip(self.sources, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                log.warning("Subtitle search on %s failed: %s", source.name, result)
                problems.append(f"{source.name}: {result}")
                continue
            for track in result:
                track.source = track.source or source.name
            tracks.extend(result)
        if not tracks and len(problems) == len(self.sources):
            raise SubtitleError("; ".join(problems))
        return tracks

    async def _download(self, provider: str, show: Show, episode: Episode, track: SubtitleTrack) -> str:
        source = next((s for s in self.sources if s.name == track.source), None)
        if source is None:
            raise SubtitleError(f"{track.source or 'That subtitle source'} isn't available any more")
        path = await source.fetch(track)
        self._store.set_subtitle(provider, show.id, episode.number, track.language, _source_key(track), path)
        return path


def _source_key(track: SubtitleTrack) -> str:
    return f"{track.source}:{track.id}"
