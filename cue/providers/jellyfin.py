"""Jellyfin provider: a homelab Jellyfin server over its REST API.

Each season is exposed as its own show (``"<series>"`` for season 1, ``"<series>
Season N"`` after that) because MAL, and so AniSkip, number episodes per season.
Provider IDs from the item metadata (AniList, AniDB, ...) are passed on to help the
MAL lookup. Streams are direct-play URLs.
"""

from __future__ import annotations

import asyncio
import hashlib
import platform
from collections.abc import Mapping
from typing import Any

import httpx

from cue import __version__
from cue.providers import register
from cue.providers.base import Episode, ProviderConfigError, ProviderError, Show, Stream

_MAL_KEYS = ("myanimelist", "mal")


def _mal_id(ids: Mapping[str, str]) -> int | None:
    for key, value in ids.items():
        if key.casefold() in _MAL_KEYS and str(value).isdigit():
            return int(value)
    return None


def _quality(source: Mapping[str, Any]) -> str | None:
    for stream in source.get("MediaStreams") or ():
        if stream.get("Type") == "Video" and stream.get("Height"):
            return f"{stream['Height']}p"
    return None


@register
class JellyfinProvider:
    name = "jellyfin"

    def __init__(self, options: Mapping[str, Any], *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = str(options.get("url") or "").rstrip("/")
        self._token = str(options.get("api_key") or "")
        if not self.url or not self._token:
            raise ProviderConfigError("set url and api_key under [providers.jellyfin]")
        self._user_id = str(options.get("user_id") or "") or None
        device = (platform.node() or "Cue").replace('"', "")
        device_id = hashlib.sha1(device.encode()).hexdigest()[:16]
        auth = (
            f'MediaBrowser Client="Cue", Device="{device}", DeviceId="cue-{device_id}", '
            f'Version="{__version__}", Token="{self._token}"'
        )
        self._client = httpx.AsyncClient(
            base_url=self.url, headers={"Authorization": auth}, timeout=20, transport=transport
        )
        # (show id, episode number) -> Jellyfin items; several items means several versions.
        self._items: dict[tuple[str, float], list[dict[str, Any]]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        if self._user_id:
            params.setdefault("userId", self._user_id)
        query = {key: value for key, value in params.items() if value is not None}
        try:
            response = await self._client.get(path, params=query)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"Jellyfin answered {exc.response.status_code} for {path}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Cannot reach Jellyfin at {self.url}: {exc}") from exc
        return response.json()

    async def search(self, query: str) -> list[Show]:
        data = await self._get(
            "/Items",
            searchTerm=query,
            IncludeItemTypes="Series",
            Recursive="true",
            Fields="ProviderIds",  # ProductionYear is always included
            Limit=50,
        )
        groups = await asyncio.gather(*(self._season_shows(series) for series in data.get("Items", [])))
        return [show for group in groups for show in group]

    async def _season_shows(self, series: Mapping[str, Any]) -> list[Show]:
        series_id, name = series["Id"], series.get("Name") or "Untitled"
        series_ids = {k: str(v) for k, v in (series.get("ProviderIds") or {}).items() if v}
        year = series.get("ProductionYear")
        data = await self._get(f"/Shows/{series_id}/Seasons", Fields="ProviderIds,ChildCount")
        seasons = data.get("Items", [])
        if not seasons:
            return [Show(id=f"{series_id}:", title=name, year=year, mal_id=_mal_id(series_ids), external_ids=series_ids)]

        shows = []
        for season in seasons:
            number = season.get("IndexNumber")
            ids = {k: str(v) for k, v in (season.get("ProviderIds") or {}).items() if v}
            if number in (None, 1):
                # The series' IDs describe its first season.
                ids = {**series_ids, **ids}
            if number == 0:
                title = f"{name} Specials"
            elif number is not None and number > 1:
                title = f"{name} Season {number}"
            else:
                title = name
            shows.append(
                Show(
                    id=f"{series_id}:{season['Id']}",
                    title=title,
                    year=season.get("ProductionYear") or year,
                    episode_count=season.get("ChildCount"),
                    mal_id=_mal_id(ids),
                    external_ids=ids,
                )
            )
        return shows

    async def episodes(self, show: Show) -> list[Episode]:
        series_id, _, season_id = show.id.partition(":")
        data = await self._get(f"/Shows/{series_id}/Episodes", seasonId=season_id or None, Fields="MediaSources")
        for key in [key for key in self._items if key[0] == show.id]:
            del self._items[key]

        episodes: dict[float, Episode] = {}
        next_number = 1.0
        for item in data.get("Items", []):
            index = item.get("IndexNumber")
            number = float(index) if index is not None else next_number
            next_number = number + 1
            self._items.setdefault((show.id, number), []).append(item)
            episodes.setdefault(
                number,
                Episode(show_id=show.id, number=number, title=item.get("Name"), season=item.get("ParentIndexNumber")),
            )
        return [episodes[number] for number in sorted(episodes)]

    async def stream(self, episode: Episode) -> list[Stream]:
        key = (episode.show_id, episode.number)
        if key not in self._items:
            await self.episodes(Show(id=episode.show_id, title=""))
        items = self._items.get(key)
        if not items:
            raise ProviderError(f"Episode {episode.label} is not on the server")

        # The token alone is enough; this header is handed to mpv and yt-dlp.
        headers = {"Authorization": f'MediaBrowser Token="{self._token}"'}
        streams = []
        for item in items:
            for source in item.get("MediaSources") or [{"Id": item["Id"]}]:
                url = f"{self.url}/Videos/{item['Id']}/stream?static=true&MediaSourceId={source['Id']}"
                streams.append(Stream(url=url, headers=headers, quality=_quality(source)))
        return streams
