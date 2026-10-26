"""Local provider: shows from folders on disk.

File names such as ``Show Name - 01.mkv`` (also ``S01E02``, ``Episode 3``, and bare
numbers inside a show folder) are parsed and grouped into shows.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process, utils

from cue.providers import register
from cue.providers.base import Episode, ProviderConfigError, ProviderError, Show, Stream

VIDEO_EXTENSIONS = frozenset(
    {".mkv", ".mp4", ".avi", ".webm", ".m4v", ".mov", ".ts", ".wmv", ".flv", ".mpg", ".mpeg", ".ogv"}
)
RESCAN_AFTER = 30.0  # seconds between library scans

_NUM = r"\d{1,4}(?:\.\d{1,2})?"
_VERSION = r"(?:v\d)?"
_PATTERNS = (
    # Show.Name.S01E02, Show Name S1 E2
    re.compile(rf"^(?P<title>.*?)(?:^|[\s._-]+)S(?P<season>\d{{1,2}})[\s._-]*E(?P<ep>{_NUM}){_VERSION}(?!\d)", re.I),
    # Show Name - 01, Show Name - Episode 01
    re.compile(rf"^(?P<title>.+?)\s+-\s+(?:(?:episode|ep|e)\.?\s*)?(?P<ep>{_NUM}){_VERSION}(?:\s|$)", re.I),
    # Show Name Episode 3, Show_Name_E03
    re.compile(rf"^(?P<title>.*?)(?:^|[\s._-]+)(?:episode|ep|e)[\s._]?(?P<ep>{_NUM}){_VERSION}(?!\d)", re.I),
    # Show Name 03, but not a trailing year
    re.compile(rf"^(?P<title>.+?)[\s._-]+(?!(?:19|20)\d\d$)(?P<ep>{_NUM}){_VERSION}$", re.I),
    # 03 on its own: the title comes from the folder
    re.compile(rf"^(?P<title>)(?P<ep>{_NUM}){_VERSION}$", re.I),
)
_BRACKETS = re.compile(r"\[[^\]]*\]|\{[^}]*\}")
_PARENS = re.compile(r"\(([^)]*)\)")
_YEAR = re.compile(r"(?:19|20)\d\d")
_QUALITY = re.compile(r"\b(2160|1440|1080|720|576|480|360)p\b|\b(4k|uhd)\b", re.I)
_SEASON_FOLDER = re.compile(r"^(?:season|s)[\s._-]*(\d{1,2})$", re.I)


@dataclass(frozen=True)
class ParsedName:
    title: str
    episode: float
    season: int | None = None
    year: int | None = None
    quality: str | None = None


@dataclass
class LocalFile:
    path: Path
    quality: str | None


@dataclass
class LocalShow:
    show: Show
    season: int | None
    files: dict[float, list[LocalFile]] = field(default_factory=dict)


def _strip_tags(text: str) -> tuple[str, int | None]:
    """Remove ``[group]``/``(1080p)`` style tags, keeping a ``(2019)`` year."""
    year: int | None = None

    def paren(match: re.Match[str]) -> str:
        nonlocal year
        inner = match.group(1).strip()
        if _YEAR.fullmatch(inner):
            year = int(inner)
        return " "

    text = _PARENS.sub(paren, _BRACKETS.sub(" ", text))
    return re.sub(r"\s+", " ", text).strip(" -_."), year


def _tidy(title: str) -> str:
    title = title.replace("_", " ")
    if " " not in title:
        title = title.replace(".", " ")
    return re.sub(r"\s+", " ", title).strip(" -_.")


def _quality(text: str) -> str | None:
    match = _QUALITY.search(text)
    if not match:
        return None
    return f"{match.group(1)}p" if match.group(1) else "2160p"


def parse_path(path: Path) -> ParsedName:
    """Work out show title, episode number and season from a video file's path."""
    cleaned, year = _strip_tags(path.stem)
    match = next((m for m in (p.search(cleaned) for p in _PATTERNS) if m), None)
    if match is None:
        # No episode number: treat the file as a one-episode show (a movie, an OVA).
        return ParsedName(_tidy(cleaned) or path.stem, 1.0, None, year, _quality(path.stem))

    title = _tidy(match.group("title"))
    season_text = match.groupdict().get("season")
    season = int(season_text) if season_text else None

    # Folders fill the gaps: "Show (2019)/Season 2/01.mkv".
    folder = path.parent
    folder_season = _SEASON_FOLDER.match(folder.name)
    if folder_season or folder.name.casefold() == "specials":
        if season is None:
            season = int(folder_season.group(1)) if folder_season else 0
        folder = folder.parent
    folder_title, folder_year = _strip_tags(folder.name)
    folder_title = _tidy(folder_title)
    if not title:
        title, year = folder_title, year or folder_year
    elif year is None and utils.default_process(folder_title) == utils.default_process(title):
        year = folder_year
    return ParsedName(title, float(match.group("ep")), season, year, _quality(path.stem))


def _show_key(title: str, season: int | None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", utils.default_process(title)).strip("-") or "untitled"
    return slug if season in (None, 1) else f"{slug}-s{season}"


def _display_title(title: str, season: int | None) -> str:
    if season == 0:
        return f"{title} Specials"
    if season is not None and season > 1:
        return f"{title} Season {season}"
    return title


def scan(roots: list[Path]) -> dict[str, LocalShow]:
    """Walk the media folders and group video files into shows."""
    library: dict[str, LocalShow] = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for filename in filenames:
                path = Path(dirpath, filename)
                if path.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                parsed = parse_path(path)
                key = _show_key(parsed.title, parsed.season)
                entry = library.get(key)
                if entry is None:
                    show = Show(id=key, title=_display_title(parsed.title, parsed.season), year=parsed.year)
                    entry = library[key] = LocalShow(show, parsed.season)
                entry.show.year = entry.show.year or parsed.year
                entry.files.setdefault(parsed.episode, []).append(LocalFile(path, parsed.quality))
    for entry in library.values():
        entry.show.episode_count = len(entry.files)
    return library


def _resolution(quality: str | None) -> int:
    return int(quality.rstrip("p")) if quality else 0


@register
class LocalProvider:
    name = "local"

    def __init__(self, options: Mapping[str, Any]) -> None:
        raw = options.get("path", "")
        paths = [raw] if isinstance(raw, str) else list(raw)
        self.roots = [Path(p).expanduser() for p in paths if p]
        if not self.roots:
            raise ProviderConfigError("set path under [providers.local] to your media folder")
        self._library: dict[str, LocalShow] = {}
        self._scanned_at: float | None = None
        self._lock = asyncio.Lock()

    async def _shows(self) -> dict[str, LocalShow]:
        async with self._lock:
            if self._scanned_at is None or time.monotonic() - self._scanned_at > RESCAN_AFTER:
                existing = [root for root in self.roots if root.is_dir()]
                if not existing:
                    raise ProviderError(f"Media folder not found: {self.roots[0]}")
                self._library = await asyncio.to_thread(scan, existing)
                self._scanned_at = time.monotonic()
            return self._library

    async def _entry(self, show_id: str) -> LocalShow:
        entry = (await self._shows()).get(show_id)
        if entry is None:
            raise ProviderError("That show is no longer in the media folder")
        return entry

    async def search(self, query: str) -> list[Show]:
        library = await self._shows()
        if query.strip() in ("", "*"):
            return sorted((entry.show for entry in library.values()), key=lambda s: s.title.casefold())
        choices = {key: entry.show.title for key, entry in library.items()}
        matches = process.extract(
            query, choices, scorer=fuzz.WRatio, processor=utils.default_process, score_cutoff=60, limit=None
        )
        return [library[key].show for _title, _score, key in matches]

    async def episodes(self, show: Show) -> list[Episode]:
        entry = await self._entry(show.id)
        return [Episode(show_id=show.id, number=number, season=entry.season) for number in sorted(entry.files)]

    async def stream(self, episode: Episode) -> list[Stream]:
        entry = await self._entry(episode.show_id)
        files = entry.files.get(episode.number)
        if not files:
            raise ProviderError(f"Episode {episode.label} is no longer in the media folder")
        files = sorted(files, key=lambda f: _resolution(f.quality), reverse=True)
        return [Stream(url=str(f.path), quality=f.quality) for f in files]
