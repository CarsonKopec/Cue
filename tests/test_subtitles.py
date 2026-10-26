from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import httpx
import pytest

from conftest import json_transport
from cue.providers.base import Episode, Show
from cue.store import Store
from cue.subtitles import SubtitleError, SubtitleService
from cue.subtitles.opensubtitles import OpenSubtitles, file_hash


def reference_hash(path: Path) -> str:
    data = path.read_bytes()
    value = len(data)
    for chunk in (data[:65536], data[-65536:]):
        for (word,) in struct.iter_unpack("<Q", chunk):
            value = (value + word) % 2**64
    return f"{value:016x}"


def test_file_hash(tmp_path: Path) -> None:
    path = tmp_path / "video.mkv"
    path.write_bytes(os.urandom(300_000))
    assert file_hash(path) == reference_hash(path)
    small = tmp_path / "small.mkv"
    small.write_bytes(b"x" * 1000)
    with pytest.raises(ValueError):
        file_hash(small)


def _result(file_id: int, language: str, downloads: int, *, hash_match: bool = False, episode: int = 1) -> dict:
    return {
        "id": str(file_id),
        "attributes": {
            "language": language,
            "download_count": downloads,
            "moviehash_match": hash_match,
            "hearing_impaired": False,
            "release": f"Release {file_id}",
            "files": [{"file_id": file_id, "file_name": f"{file_id}.srt"}],
            "feature_details": {"episode_number": episode},
        },
    }


class FakeOpenSubtitles:
    def __init__(self) -> None:
        self.searches: list[httpx.QueryParams] = []
        self.downloads: list[int] = []
        self.quota_left = 10

    def search(self, request: httpx.Request) -> httpx.Response:
        self.searches.append(request.url.params)
        assert request.headers["Api-Key"] == "key"
        assert request.headers["User-Agent"].startswith("Cue v")
        if "moviehash" in request.url.params:
            return httpx.Response(200, json={"data": [_result(7, "en", 5, hash_match=True), _result(8, "en", 900)]})
        return httpx.Response(
            200,
            json={"data": [_result(1, "en", 10), _result(2, "en", 500), _result(3, "de", 50), _result(4, "en", 999, episode=2)]},
        )

    def download(self, request: httpx.Request) -> httpx.Response:
        if self.quota_left == 0:
            return httpx.Response(406, json={"message": "You have downloaded your allowed 5 subtitles for 24h"})
        self.quota_left -= 1
        file_id = json.loads(request.content)["file_id"]
        self.downloads.append(file_id)
        return httpx.Response(200, json={"link": f"https://dl.test/{file_id}.srt", "remaining": self.quota_left})

    def file(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=f"1\n00:00:01,000 --> 00:00:02,000\n{request.url.path}\n".encode())

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=json_transport(
                {
                    "GET api.opensubtitles.com/api/v1/subtitles": self.search,
                    "POST api.opensubtitles.com/api/v1/download": self.download,
                    "GET dl.test": self.file,
                }
            )
        )


async def test_search_by_title_ranks_by_downloads(tmp_path: Path) -> None:
    fake = FakeOpenSubtitles()
    async with fake.client() as client:
        source = OpenSubtitles("key", client, tmp_path / "subs")
        tracks = await source.find(Show("s", "Sousou no Frieren"), Episode("s", 1, season=1), ["en", "de"])
    assert [t.id for t in tracks] == ["2", "3", "1"]  # episode 2's result is dropped
    params = fake.searches[0]
    assert params["query"] == "sousou no frieren"
    assert params["episode_number"] == "1" and params["season_number"] == "1"
    assert params["languages"] == "de,en"
    assert list(params.keys()) == sorted(params.keys())


async def test_local_files_match_by_hash(tmp_path: Path) -> None:
    video = tmp_path / "ep1.mkv"
    video.write_bytes(os.urandom(200_000))
    fake = FakeOpenSubtitles()
    async with fake.client() as client:
        source = OpenSubtitles("key", client, tmp_path / "subs")
        tracks = await source.find(Show("s", "Frieren"), Episode("s", 1), ["en"], file_path=str(video))
    assert [t.id for t in tracks] == ["7"]
    assert "exact match" in tracks[0].label
    assert fake.searches[0]["moviehash"] == file_hash(video)


async def test_service_downloads_once_and_caches(tmp_path: Path, store: Store) -> None:
    fake = FakeOpenSubtitles()
    async with fake.client() as client:
        service = SubtitleService(OpenSubtitles("key", client, tmp_path / "subs"), store, ["en", "de"])
        show, episode = Show("s", "Sousou no Frieren"), Episode("s", 1)
        found = await service.ensure("local", show, episode)
        assert [language for language, _ in found] == ["en", "de"]
        english = Path(found[0][1])
        assert english == tmp_path / "subs" / "Sousou no Frieren" / "1.en.srt"
        assert english.read_text().endswith("/2.srt\n")
        assert fake.downloads == [2, 3]

        again = await service.ensure("local", show, episode)
        assert again == found
        assert fake.downloads == [2, 3] and len(fake.searches) == 1

        # Picking the cached track again reuses the file; a different one is fetched.
        tracks = await service.search(show, episode)
        cached = next(t for t in tracks if t.id == "2")
        assert await service.choose("local", show, episode, cached) == str(english)
        assert fake.downloads == [2, 3]
        other = next(t for t in tracks if t.id == "1")
        await service.choose("local", show, episode, other)
        assert fake.downloads == [2, 3, 1]
        assert english.read_text().endswith("/1.srt\n")


async def test_quota_errors_are_reported(tmp_path: Path, store: Store) -> None:
    fake = FakeOpenSubtitles()
    fake.quota_left = 0
    async with fake.client() as client:
        service = SubtitleService(OpenSubtitles("key", client, tmp_path / "subs"), store, ["en"])
        with pytest.raises(SubtitleError, match="allowed 5 subtitles"):
            await service.ensure("local", Show("s", "Frieren"), Episode("s", 1))
