"""SQLite layer: watch history, resume positions, MAL IDs, skip times and subtitles.

Lives at ``%APPDATA%\\cue\\cue.db``.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
  provider TEXT, show_id TEXT, title TEXT,
  last_episode REAL, position REAL, updated_at INTEGER,
  PRIMARY KEY (provider, show_id)
);
CREATE TABLE IF NOT EXISTS mal_ids (
  provider TEXT, show_id TEXT, mal_id INTEGER,
  PRIMARY KEY (provider, show_id)
);
CREATE TABLE IF NOT EXISTS skip_times (
  mal_id INTEGER, episode REAL, kind TEXT,  -- 'op' | 'ed'
  start REAL, "end" REAL,
  PRIMARY KEY (mal_id, episode, kind)
);
CREATE TABLE IF NOT EXISTS subtitles (
  provider TEXT, show_id TEXT, episode REAL, language TEXT,
  source TEXT, path TEXT,
  PRIMARY KEY (provider, show_id, episode, language)
);
-- Per-episode watched markers for the Episodes grid; history only keeps the latest episode.
CREATE TABLE IF NOT EXISTS watched (
  provider TEXT, show_id TEXT, episode REAL, watched_at INTEGER,
  PRIMARY KEY (provider, show_id, episode)
);
"""


@dataclass(frozen=True)
class HistoryEntry:
    provider: str
    show_id: str
    title: str
    last_episode: float
    position: float
    updated_at: int


@dataclass(frozen=True)
class CachedSubtitle:
    language: str
    source: str
    path: str


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- history -----------------------------------------------------------

    def get_history(self, provider: str, show_id: str) -> HistoryEntry | None:
        row = self._db.execute(
            "SELECT provider, show_id, title, last_episode, position, updated_at"
            " FROM history WHERE provider = ? AND show_id = ?",
            (provider, show_id),
        ).fetchone()
        return HistoryEntry(*row) if row else None

    def save_progress(self, provider: str, show_id: str, title: str, episode: float, position: float) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO history (provider, show_id, title, last_episode, position, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (provider, show_id) DO UPDATE SET"
                " title = excluded.title, last_episode = excluded.last_episode,"
                " position = excluded.position, updated_at = excluded.updated_at",
                (provider, show_id, title, episode, position, int(time.time())),
            )

    def continue_watching(self, limit: int = 20) -> list[HistoryEntry]:
        rows = self._db.execute(
            "SELECT provider, show_id, title, last_episode, position, updated_at"
            " FROM history ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [HistoryEntry(*row) for row in rows]

    # -- watched markers ---------------------------------------------------

    def mark_watched(self, provider: str, show_id: str, episode: float) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO watched (provider, show_id, episode, watched_at) VALUES (?, ?, ?, ?)",
                (provider, show_id, episode, int(time.time())),
            )

    def watched(self, provider: str, show_id: str) -> set[float]:
        rows = self._db.execute(
            "SELECT episode FROM watched WHERE provider = ? AND show_id = ?", (provider, show_id)
        ).fetchall()
        return {row[0] for row in rows}

    # -- MAL IDs -----------------------------------------------------------

    def get_mal_id(self, provider: str, show_id: str) -> int | None:
        row = self._db.execute(
            "SELECT mal_id FROM mal_ids WHERE provider = ? AND show_id = ?", (provider, show_id)
        ).fetchone()
        return row[0] if row else None

    def set_mal_id(self, provider: str, show_id: str, mal_id: int) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO mal_ids (provider, show_id, mal_id) VALUES (?, ?, ?)",
                (provider, show_id, mal_id),
            )

    # -- skip times --------------------------------------------------------

    def get_skip_times(self, mal_id: int, episode: float) -> list[tuple[str, float, float]]:
        rows = self._db.execute(
            'SELECT kind, start, "end" FROM skip_times WHERE mal_id = ? AND episode = ? ORDER BY start',
            (mal_id, episode),
        ).fetchall()
        return [(kind, start, end) for kind, start, end in rows]

    def set_skip_times(self, mal_id: int, episode: float, intervals: Iterable[tuple[str, float, float]]) -> None:
        with self._db:
            self._db.execute("DELETE FROM skip_times WHERE mal_id = ? AND episode = ?", (mal_id, episode))
            self._db.executemany(
                'INSERT OR REPLACE INTO skip_times (mal_id, episode, kind, start, "end") VALUES (?, ?, ?, ?, ?)',
                [(mal_id, episode, kind, start, end) for kind, start, end in intervals],
            )

    # -- subtitles ---------------------------------------------------------

    def get_subtitle(self, provider: str, show_id: str, episode: float, language: str) -> CachedSubtitle | None:
        row = self._db.execute(
            "SELECT language, source, path FROM subtitles"
            " WHERE provider = ? AND show_id = ? AND episode = ? AND language = ?",
            (provider, show_id, episode, language),
        ).fetchone()
        return CachedSubtitle(*row) if row else None

    def set_subtitle(
        self, provider: str, show_id: str, episode: float, language: str, source: str, path: str
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO subtitles (provider, show_id, episode, language, source, path)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (provider, show_id, episode, language, source, path),
            )
