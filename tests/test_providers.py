from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from conftest import json_transport
from cue.providers import create_providers, discover
from cue.providers.base import Episode, Provider, ProviderError, Show
from cue.providers.jellyfin import JellyfinProvider
from cue.providers.local import LocalProvider, parse_path


@pytest.mark.parametrize(
    ("path", "title", "episode", "season"),
    [
        ("[SubsPlease] Sousou no Frieren - 01 (1080p) [ABCD].mkv", "Sousou no Frieren", 1, None),
        ("Show Name - 12.5.mkv", "Show Name", 12.5, None),
        ("Show Name - 03v2 - The Title.mkv", "Show Name", 3, None),
        ("Re:Zero - Starting Life in Another World - 05.mkv", "Re:Zero - Starting Life in Another World", 5, None),
        ("Mob.Psycho.100.S02E07.1080p.WEB.mkv", "Mob Psycho 100", 7, 2),
        ("Show Name Episode 4.mp4", "Show Name", 4, None),
        ("Show_Name_E08.mkv", "Show Name", 8, None),
        ("Show Name 09.mkv", "Show Name", 9, None),
        ("Spy x Family/Season 2/01.mkv", "Spy x Family", 1, 2),
        ("Some Show (2019)/02.mkv", "Some Show", 2, None),
    ],
)
def test_parse_path(path: str, title: str, episode: float, season: int | None) -> None:
    parsed = parse_path(Path(path))
    assert (parsed.title, parsed.episode, parsed.season) == (title, episode, season)


def test_parse_path_year_and_quality() -> None:
    parsed = parse_path(Path("Some Show (2019)/Some Show - 02 [1080p].mkv"))
    assert parsed.year == 2019 and parsed.quality == "1080p"
    assert parse_path(Path("A Movie (2020).mkv")).episode == 1


async def test_local_provider(media_dir: Path) -> None:
    provider = LocalProvider({"path": str(media_dir)})
    assert isinstance(provider, Provider)

    everything = await provider.search("*")
    assert [s.title for s in everything] == ["Mob Psycho 100", "Sousou no Frieren", "Spy x Family Season 2"]

    shows = await provider.search("frieren")
    assert [s.title for s in shows] == ["Sousou no Frieren"]
    frieren = shows[0]
    assert frieren.episode_count == 3

    episodes = await provider.episodes(frieren)
    assert [e.number for e in episodes] == [1, 2, 3]
    streams = await provider.stream(episodes[1])
    assert [s.quality for s in streams] == ["1080p", "720p"]
    assert streams[0].url.endswith("Sousou no Frieren - 02 (1080p) [ABCD1234].mkv")

    with pytest.raises(ProviderError):
        await provider.episodes(Show(id="gone", title="Gone"))


def test_local_provider_needs_a_path() -> None:
    with pytest.raises(ProviderError, match="path"):
        LocalProvider({"path": ""})


async def test_local_provider_missing_folder(tmp_path: Path) -> None:
    provider = LocalProvider({"path": str(tmp_path / "nope")})
    with pytest.raises(ProviderError, match="not found"):
        await provider.search("x")


def _jellyfin(requests: list[httpx.Request]) -> JellyfinProvider:
    def record(response: dict) -> callable:
        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=response)

        return respond

    routes = {
        "GET jf.test/Items": record(
            {
                "Items": [
                    {
                        "Id": "series1",
                        "Name": "Frieren: Beyond Journey's End",
                        "ProductionYear": 2023,
                        "ProviderIds": {"AniList": "154587", "Tvdb": "424536"},
                    }
                ]
            }
        ),
        "GET jf.test/Shows/series1/Seasons": record(
            {
                "Items": [
                    {"Id": "s1", "IndexNumber": 1, "ChildCount": 28, "ProviderIds": {}},
                    {"Id": "s2", "IndexNumber": 2, "ChildCount": 10, "ProductionYear": 2026,
                     "ProviderIds": {"AniList": "182255"}},
                    {"Id": "s0", "IndexNumber": 0, "ChildCount": 1},
                ]
            }
        ),
        "GET jf.test/Shows/series1/Episodes": record(
            {
                "Items": [
                    {"Id": "e1", "IndexNumber": 1, "ParentIndexNumber": 1, "Name": "The Journey's End",
                     "MediaSources": [{"Id": "m1", "MediaStreams": [{"Type": "Video", "Height": 1080}]}]},
                    {"Id": "e2", "IndexNumber": 2, "ParentIndexNumber": 1, "Name": "It Didn't Have to Be Magic"},
                ]
            }
        ),
    }
    return JellyfinProvider({"url": "https://jf.test/", "api_key": "secret"}, transport=json_transport(routes))


async def test_jellyfin_search_splits_seasons() -> None:
    requests: list[httpx.Request] = []
    provider = _jellyfin(requests)
    shows = await provider.search("frieren")
    assert [s.title for s in shows] == [
        "Frieren: Beyond Journey's End",
        "Frieren: Beyond Journey's End Season 2",
        "Frieren: Beyond Journey's End Specials",
    ]
    first, second, _specials = shows
    assert first.id == "series1:s1" and first.episode_count == 28 and first.year == 2023
    assert first.external_ids == {"AniList": "154587", "Tvdb": "424536"}
    assert second.external_ids == {"AniList": "182255"} and second.year == 2026
    assert requests[0].url.params["searchTerm"] == "frieren"
    assert 'Token="secret"' in requests[0].headers["Authorization"]
    await provider.aclose()


async def test_jellyfin_episodes_and_streams() -> None:
    provider = _jellyfin([])
    show = Show(id="series1:s1", title="Frieren")
    episodes = await provider.episodes(show)
    assert [(e.number, e.title, e.season) for e in episodes] == [
        (1, "The Journey's End", 1),
        (2, "It Didn't Have to Be Magic", 1),
    ]
    streams = await provider.stream(episodes[0])
    assert streams[0].url == "https://jf.test/Videos/e1/stream?static=true&MediaSourceId=m1"
    assert streams[0].headers == {"Authorization": 'MediaBrowser Token="secret"'}
    assert streams[0].quality == "1080p"
    # No media sources listed: the item itself is the source.
    streams = await provider.stream(Episode(show_id="series1:s1", number=2))
    assert streams[0].url.endswith("/Videos/e2/stream?static=true&MediaSourceId=e2")
    with pytest.raises(ProviderError):
        await provider.stream(Episode(show_id="series1:s1", number=9))
    await provider.aclose()


async def test_jellyfin_errors_become_provider_errors() -> None:
    provider = JellyfinProvider(
        {"url": "https://jf.test", "api_key": "k"},
        transport=httpx.MockTransport(lambda request: httpx.Response(401)),
    )
    with pytest.raises(ProviderError, match="401"):
        await provider.search("x")
    await provider.aclose()


def test_registry_finds_builtins_and_plugins(tmp_path: Path) -> None:
    plugins = tmp_path / "providers"
    plugins.mkdir()
    (plugins / "dummy.py").write_text(
        "from cue.providers import register\n"
        "@register\n"
        "class Dummy:\n"
        "    name = 'dummy'\n"
        "    def __init__(self, options): self.options = options\n"
        "    async def search(self, query): return []\n"
        "    async def episodes(self, show): return []\n"
        "    async def stream(self, episode): return []\n",
        encoding="utf-8",
    )
    assert {"jellyfin", "local", "dummy"} <= set(discover(plugins))

    ready, problems = create_providers({"dummy": {"x": 1}, "local": {"path": ""}, "jellyfin": {}}, plugins)
    assert list(ready) == ["dummy"] and ready["dummy"].options == {"x": 1}
    assert set(problems) == {"local", "jellyfin"}


def test_anime_server_html_extracts_server_choices() -> None:
    from cue.providers.anime import _extract_server_choices

    html = '''
    <div class="item server-item" data-type="sub" data-server-name="ZokoAnime"
         data-hash="aHR0cHM6Ly96b2tvYW5pbWUudmlkZW8vc3RyZWFtL21hbC8zNDU3Mi84NS9zdWI=">
      <a href="javascript:;" class="btn">ZokoAnime</a>
    </div>
    <div class="item server-item" data-type="dub" data-server-name="HD-1"
         data-hash="aHR0cHM6Ly9tZWdhcGxheS5idXp6L3N0cmVhbS9zLTIvMjc0NjEvZHViP3M9dGNkbg==">
      <a href="javascript:;" class="btn">HD-1</a>
    </div>
    '''
    assert _extract_server_choices(html) == [
        ("https://zokoanime.video/stream/mal/34572/85/sub", "ZokoAnime", "sub"),
        ("https://megaplay.buzz/stream/s-2/27461/dub?s=tcdn", "HD-1", "dub"),
    ]


def test_anime_server_html_ignores_invalid_hashes() -> None:
    from cue.providers.anime import _extract_server_choices

    html = '<div class="item server-item" data-type="sub" data-server-name="Bad" data-hash="not-base64!!!"></div>'
    assert _extract_server_choices(html) == []
