"""Show -> MyAnimeList ID lookup.

Tried in order, first hit wins, and the result is cached in SQLite:

1. the MAL ID the provider already knows,
2. other database IDs from the provider's metadata: AniList through the AniList API,
   AniDB and Kitsu through the ARM ID-mapping service,
3. a title search on MAL's prefix search (as ani-skip does), then on Jikan.

Title matches must clear a fuzzy-score threshold: no skip data beats wrong skip data.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx
from rapidfuzz import fuzz, utils

from cue.providers.base import Show
from cue.store import Store

log = logging.getLogger(__name__)

ANILIST_API = "https://graphql.anilist.co"
ARM_API = "https://arm.haglund.dev/api/v2/ids"
MAL_PREFIX_SEARCH = "https://myanimelist.net/search/prefix.json"
JIKAN_SEARCH = "https://api.jikan.moe/v4/anime"
MATCH_THRESHOLD = 75.0
TIMEOUT = 10

_ANILIST_QUERY = "query ($id: Int) { Media(id: $id, type: ANIME) { idMal } }"
_ARM_SOURCES = {"anidb": "anidb", "kitsu": "kitsu"}
_ORDINAL_SEASON = re.compile(r"\b(\d+)(?:st|nd|rd|th)\s+season\b", re.I)
_SEASON = re.compile(r"\bseason (\d+)\b")


@dataclass(frozen=True)
class Candidate:
    mal_id: int
    titles: tuple[str, ...]
    year: int | None = None


def normalize_title(title: str) -> str:
    """Lower-case, strip punctuation, and spell "2nd Season" as "season 2"."""
    return utils.default_process(_ORDINAL_SEASON.sub(r"season \1", title))


def _season(normalized: str) -> int:
    match = _SEASON.search(normalized)
    return int(match.group(1)) if match else 1


def score(query: str, candidate: Candidate, year: int | None = None) -> float:
    wanted = normalize_title(query)
    best = 0.0
    for title in candidate.titles:
        name = normalize_title(title)
        if not name:
            continue
        value = (fuzz.ratio(wanted, name) + fuzz.token_set_ratio(wanted, name)) / 2
        if _season(wanted) != _season(name):
            value -= 20
        best = max(best, value)
    if year and candidate.year:
        if year == candidate.year:
            best += 5
        elif abs(year - candidate.year) > 1:
            best -= 10
    return best


def best_match(query: str, candidates: Sequence[Candidate], year: int | None = None) -> int | None:
    """The best-scoring candidate's MAL ID, or None if nothing is close enough.

    Ties go to the earlier candidate, i.e. the search service's own ranking.
    """
    best: tuple[float, int] | None = None
    for index, candidate in enumerate(candidates):
        value = score(query, candidate, year)
        if best is None or value > best[0]:
            best = (value, index)
    if best is None or best[0] < MATCH_THRESHOLD:
        return None
    return candidates[best[1]].mal_id


class MalResolver:
    def __init__(self, client: httpx.AsyncClient, store: Store) -> None:
        self._client = client
        self._store = store

    async def resolve(self, provider: str, show: Show) -> int | None:
        cached = self._store.get_mal_id(provider, show.id)
        if cached is not None:
            return cached
        mal_id = show.mal_id or await self.from_external_ids(show.external_ids)
        if mal_id is None:
            mal_id = await self.search(show.title, show.year)
        if mal_id is not None:
            self._store.set_mal_id(provider, show.id, mal_id)
        return mal_id

    async def from_external_ids(self, ids: Mapping[str, str]) -> int | None:
        for key, value in ids.items():
            source = key.casefold()
            if not str(value).isdigit():
                continue
            try:
                if source == "anilist":
                    mal_id = await self._from_anilist(int(value))
                elif source in _ARM_SOURCES:
                    mal_id = await self._from_arm(_ARM_SOURCES[source], int(value))
                else:
                    continue
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                log.info("MAL lookup via %s %s failed: %s", key, value, exc)
                continue
            if mal_id:
                return mal_id
        return None

    async def _from_anilist(self, anilist_id: int) -> int | None:
        response = await self._client.post(
            ANILIST_API, json={"query": _ANILIST_QUERY, "variables": {"id": anilist_id}}, timeout=TIMEOUT
        )
        response.raise_for_status()
        media = (response.json().get("data") or {}).get("Media") or {}
        return media.get("idMal")

    async def _from_arm(self, source: str, value: int) -> int | None:
        response = await self._client.get(ARM_API, params={"source": source, "id": value}, timeout=TIMEOUT)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return (response.json() or {}).get("myanimelist")

    async def search(self, title: str, year: int | None = None) -> int | None:
        for fetch in (self._mal_prefix, self._jikan):
            try:
                candidates = await fetch(title)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                log.info("MAL title search (%s) failed: %s", fetch.__name__, exc)
                continue
            mal_id = best_match(title, candidates, year)
            if mal_id is not None:
                return mal_id
        return None

    async def _mal_prefix(self, title: str) -> list[Candidate]:
        response = await self._client.get(
            MAL_PREFIX_SEARCH, params={"type": "anime", "keyword": title[:100]}, timeout=TIMEOUT
        )
        response.raise_for_status()
        candidates = []
        for category in response.json().get("categories") or ():
            if category.get("type") != "anime":
                continue
            for item in category.get("items") or ():
                payload = item.get("payload") or {}
                candidates.append(Candidate(int(item["id"]), (item.get("name") or "",), payload.get("start_year")))
        return candidates

    async def _jikan(self, title: str) -> list[Candidate]:
        response = await self._client.get(JIKAN_SEARCH, params={"q": title, "limit": 10}, timeout=TIMEOUT)
        response.raise_for_status()
        candidates = []
        for item in response.json().get("data") or ():
            titles = [item.get("title"), item.get("title_english"), *(item.get("title_synonyms") or ())]
            titles += [entry.get("title") for entry in item.get("titles") or ()]
            year = item.get("year") or (((item.get("aired") or {}).get("prop") or {}).get("from") or {}).get("year")
            candidates.append(Candidate(int(item["mal_id"]), tuple(t for t in titles if t), year))
        return candidates
