"""Skip engine: MAL ID -> AniSkip intervals, cached per (mal_id, episode).

The intervals are applied during playback by :mod:`cue.player.session`, which
controls mpv directly, so no mpv Lua script is needed.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from cue.providers.base import Episode, Show
from cue.skip.aniskip import AniSkipClient, SkipInterval
from cue.skip.mal import MalResolver
from cue.store import Store
from cue.util import format_time

KIND_NAMES = {"op": "opening", "ed": "ending"}

__all__ = ["KIND_NAMES", "SkipEngine", "SkipInfo", "SkipInterval"]


@dataclass
class SkipInfo:
    mal_id: int | None
    intervals: list[SkipInterval]

    @property
    def summary(self) -> str:
        if self.mal_id is None:
            return "no skip data (no MAL match)"
        if not self.intervals:
            return "no skip data"
        return " · ".join(f"{i.kind.upper()} {format_time(i.start)}–{format_time(i.end)}" for i in self.intervals)


class SkipEngine:
    def __init__(self, client: httpx.AsyncClient, store: Store) -> None:
        self.mal = MalResolver(client, store)
        self.aniskip = AniSkipClient(client)
        self._store = store

    async def lookup(self, provider: str, show: Show, episode: Episode) -> SkipInfo:
        mal_id = await self.mal.resolve(provider, show)
        if mal_id is None:
            return SkipInfo(None, [])
        cached = self._store.get_skip_times(mal_id, episode.number)
        if cached:
            return SkipInfo(mal_id, [SkipInterval(*row) for row in cached])
        intervals = await self.aniskip.skip_times(mal_id, episode.number)
        if intervals:
            self._store.set_skip_times(mal_id, episode.number, [(i.kind, i.start, i.end) for i in intervals])
        return SkipInfo(mal_id, intervals)
