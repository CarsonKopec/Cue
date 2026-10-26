"""Optional subtitle fallback through the OpenSubtitles REST API.

Local files are matched by the OpenSubtitles file hash (exact match); everything else
by movie title/year or title + season/episode. Downloads land in
``subs\\<show>\\<episode>.<lang>.srt``.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Any

import httpx

from cue import __version__
from cue.providers.base import Episode, Show
from cue.subtitles.base import SubtitleError, SubtitleTrack
from cue.util import safe_filename

API = "https://api.opensubtitles.com/api/v1"
_CHUNK = 64 * 1024


def file_hash(path: str | os.PathLike[str]) -> str:
    """The OpenSubtitles hash: file size plus the 64-bit little-endian word sums of the
    first and last 64 KiB."""
    size = os.path.getsize(path)
    if size < _CHUNK * 2:
        raise ValueError("file too small to hash")
    value = size
    with open(path, "rb") as f:
        for offset in (0, size - _CHUNK):
            f.seek(offset)
            value += sum(struct.unpack(f"<{_CHUNK // 8}Q", f.read(_CHUNK)))
    return f"{value & 0xFFFFFFFFFFFFFFFF:016x}"


class OpenSubtitles:
    name = "opensubtitles"

    def __init__(self, api_key: str, client: httpx.AsyncClient, subs_dir: Path) -> None:
        self._client = client
        self._subs_dir = subs_dir
        self._headers = {"Api-Key": api_key, "User-Agent": f"Cue v{__version__}", "Accept": "application/json"}
        self._targets: dict[str, Path] = {}  # track id -> where find() wants it saved

    async def _search(self, **params: Any) -> list[dict[str, Any]]:
        # The API redirects unless parameters are sorted and lower-case.
        query = {key: str(value).lower() for key, value in sorted(params.items()) if value is not None}
        try:
            response = await self._client.get(f"{API}/subtitles", params=query, headers=self._headers, timeout=15)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SubtitleError(f"OpenSubtitles search failed ({exc.response.status_code})") from exc
        except httpx.HTTPError as exc:
            raise SubtitleError(f"Cannot reach OpenSubtitles: {exc}") from exc
        return response.json().get("data") or []

    async def find(
        self, show: Show, episode: Episode, languages: list[str], *, file_path: str | None = None
    ) -> list[SubtitleTrack]:
        languages_param = ",".join(sorted(set(languages)))
        results: list[dict[str, Any]] = []
        if file_path:
            try:
                moviehash = file_hash(file_path)
            except (OSError, ValueError):
                moviehash = None
            if moviehash:
                found = await self._search(moviehash=moviehash, languages=languages_param)
                results = [r for r in found if r.get("attributes", {}).get("moviehash_match")]
        if not results and show.media_type == "movie":
            results = await self._search(
                query=show.title,
                type="movie",
                year=show.year,
                languages=languages_param,
            )
        elif not results and float(episode.number).is_integer():
            found = await self._search(
                query=show.series_title or show.title,
                type="episode",
                episode_number=int(episode.number),
                season_number=episode.season,
                languages=languages_param,
            )
            results = [r for r in found if _episode_matches(r, episode)]

        tracks = []
        for result in sorted(results, key=_rank):
            attributes = result.get("attributes", {})
            files = attributes.get("files") or ()
            if not files:
                continue
            track = SubtitleTrack(
                language=str(attributes.get("language") or "").lower(),
                label=_label(attributes),
                source=self.name,
                id=str(files[0]["file_id"]),
            )
            self._targets[track.id] = (
                self._subs_dir / safe_filename(show.title) / f"{episode.label}.{track.language}.srt"
            )
            tracks.append(track)
        return tracks

    async def fetch(self, track: SubtitleTrack) -> str:
        target = self._targets.get(track.id) or self._subs_dir / "other" / f"{track.id}.{track.language}.srt"
        try:
            response = await self._client.post(
                f"{API}/download",
                json={"file_id": int(track.id), "sub_format": "srt"},
                headers=self._headers,
                timeout=15,
            )
            if response.status_code in (401, 403, 406, 429):
                message = _message(response) or "download refused"
                raise SubtitleError(f"OpenSubtitles: {message}")
            response.raise_for_status()
            link = response.json()["link"]
            content = await self._client.get(link, timeout=30)
            content.raise_for_status()
        except httpx.HTTPError as exc:
            raise SubtitleError(f"Subtitle download failed: {exc}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.content)
        return str(target)


def _episode_matches(result: dict[str, Any], episode: Episode) -> bool:
    details = result.get("attributes", {}).get("feature_details") or {}
    number = details.get("episode_number")
    return number is None or float(number) == episode.number


def _rank(result: dict[str, Any]) -> tuple[bool, int]:
    attributes = result.get("attributes", {})
    return (not attributes.get("moviehash_match"), -int(attributes.get("download_count") or 0))


def _label(attributes: dict[str, Any]) -> str:
    parts = [attributes.get("release") or "untitled release"]
    if attributes.get("moviehash_match"):
        parts.append("exact match")
    if attributes.get("hearing_impaired"):
        parts.append("SDH")
    parts.append(f"{int(attributes.get('download_count') or 0):,} downloads")
    return " · ".join(parts)


def _message(response: httpx.Response) -> str | None:
    try:
        return response.json().get("message")
    except ValueError:
        return None
