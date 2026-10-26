"""Movies and general television must not inherit anime-only assumptions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cue.player.base import PlayerError
from cue.player.session import PlaybackSession
from cue.providers.base import Episode, Show, Stream
from cue.skip import SkipInfo, SkipInterval
from cue.subtitles.opensubtitles import OpenSubtitles, file_hash


def movie_result(*, exact: bool = False) -> dict:
    return {
        "attributes": {
            "language": "en", "release": "Movie release", "download_count": 10,
            "moviehash_match": exact, "files": [{"file_id": 7}],
            # Movie responses have no usable TV episode number. This must not
            # be compared with the synthetic Episode(1) used for playback.
            "feature_details": {"feature_type": "Movie", "episode_number": 0},
        }
    }


@pytest.mark.parametrize("year", [1999, None])
async def test_movie_subtitles_search_by_movie_without_episode_filters(tmp_path: Path, year):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [movie_result()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = OpenSubtitles("key", client, tmp_path)
        tracks = await source.find(
            Show("movie:1", "The Matrix", year=year, media_type="movie"),
            Episode("movie:1", 1, season=1), ["en"],
        )
    assert [track.id for track in tracks] == ["7"]
    params = dict(requests[0].url.params)
    expected = {"query": "the matrix", "type": "movie", "languages": "en"}
    if year is not None:
        expected["year"] = str(year)
    assert params == expected


async def test_movie_subtitles_keep_exact_hash_matching(tmp_path: Path):
    video = tmp_path / "movie.mp4"
    video.write_bytes(bytes(range(256)) * 600)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [movie_result(exact=True)]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = OpenSubtitles("key", client, tmp_path / "subs")
        tracks = await source.find(
            Show("movie:1", "The Matrix", year=1999, media_type="movie"),
            Episode("movie:1", 1), ["en"], file_path=str(video),
        )
    assert len(requests) == 1
    assert dict(requests[0].url.params) == {"moviehash": file_hash(video), "languages": "en"}
    assert [track.id for track in tracks] == ["7"]


async def test_legacy_episode_subtitle_search_retains_season(tmp_path: Path):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = OpenSubtitles("key", client, tmp_path)
        await source.find(Show("series:1:season:2", "Example"), Episode("series:1:season:2", 3, season=2), ["en"])
    assert dict(requests[0].url.params) == {
        "query": "example", "type": "episode", "episode_number": "3", "season_number": "2", "languages": "en",
    }


@pytest.mark.parametrize("series_title, expected_query", [
    ("Breaking Bad", "breaking bad"),
    (None, "breaking bad — season 2"),
])
async def test_episode_subtitles_use_only_explicit_parent_title(tmp_path: Path, series_title, expected_query):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = OpenSubtitles("key", client, tmp_path)
        await source.find(
            Show("series:1:season:2", "Breaking Bad — Season 2", series_title=series_title),
            Episode("series:1:season:2", 3, season=2), ["en"],
        )
    assert requests[0].url.params["query"] == expected_query
    assert requests[0].url.params["season_number"] == "2"


@pytest.mark.parametrize("capability", [False, True, None])
async def test_provider_can_disable_anime_skip_lookup(store, capability):
    provider = SimpleNamespace(name="general")
    if capability is not None:
        provider.skip_supported = capability
    calls = []
    intervals = [SkipInterval("op", 10, 80)]

    async def lookup(*args):
        calls.append(args)
        return SkipInfo(123, intervals)

    show, episode = Show("series:1", "Death Note"), Episode("series:1", 1)
    session = PlaybackSession(
        provider_name="general", provider=provider, show=show, queue=[episode], store=store,
        player_factory=lambda: None, skip=SimpleNamespace(lookup=lookup),
    )
    await session._load_skip(session._generation, episode)
    if capability is False:
        assert calls == []
        assert session._intervals == []
        assert session.state.skip == "no skip data for this provider"
    else:
        assert calls == [("general", show, episode)]
        assert session._intervals == intervals


@pytest.mark.parametrize(("media_type", "title"), [("movie", "The Matrix"), ("episode", "The Matrix — Episode 1")])
async def test_player_title_names_movies_without_an_episode_number(store, media_type, title):
    launched = {}

    class Player:
        def observe(self, *args): pass
        def on_close(self, callback): pass
        async def quit(self): pass

        async def launch(self, url, **options):
            launched.update(options)
            raise PlayerError("stop after launch")

    class Provider:
        name = "general"

        async def stream(self, episode):
            return [Stream("https://video.test/movie.m3u8")]

    session = PlaybackSession(
        provider_name="general", provider=Provider(), show=Show("movie:1", "The Matrix", media_type=media_type),
        queue=[Episode("movie:1", 1)], store=store, player_factory=Player,
    )
    await session.run()
    assert launched["title"] == title
