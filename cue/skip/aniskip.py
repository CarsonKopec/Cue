"""AniSkip API client — the community database ani-skip uses, and Cue's only skip source."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from cue.providers.base import format_number

API = "https://api.aniskip.com/v2"


@dataclass(frozen=True)
class SkipInterval:
    kind: str  # 'op' | 'ed'
    start: float
    end: float


class AniSkipClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def skip_times(self, mal_id: int, episode: float) -> list[SkipInterval]:
        """Opening/ending intervals for one episode; empty when AniSkip has none."""
        response = await self._client.get(
            f"{API}/skip-times/{mal_id}/{format_number(episode)}",
            params=[("types[]", "op"), ("types[]", "ed"), ("episodeLength", "0")],
            timeout=10,
        )
        if response.status_code in (400, 404):
            return []
        response.raise_for_status()
        data = response.json()
        if not data.get("found"):
            return []

        intervals = []
        for result in data.get("results") or ():
            kind = result.get("skipType")
            interval = result.get("interval") or {}
            start, end = interval.get("startTime"), interval.get("endTime")
            if kind in ("op", "ed") and start is not None and end is not None and end > start:
                intervals.append(SkipInterval(kind, float(start), float(end)))
        return sorted(intervals, key=lambda i: i.start)
