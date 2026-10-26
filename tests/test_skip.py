from __future__ import annotations

import httpx
import pytest

from conftest import json_transport
from cue.providers.base import Episode, Show
from cue.skip import SkipEngine, SkipInterval
from cue.skip.aniskip import AniSkipClient
from cue.skip.mal import Candidate, MalResolver, best_match
from cue.store import Store

ANISKIP_FOUND = {
    "found": True,
    "results": [
        {"interval": {"startTime": 1300.5, "endTime": 1390.0}, "skipType": "ed", "skipId": "b", "episodeLength": 1430},
        {"interval": {"startTime": 13.891, "endTime": 123.834}, "skipType": "op", "skipId": "a", "episodeLength": 1430},
    ],
    "message": "Successfully found skip times",
    "statusCode": 200,
}


async def test_aniskip_parses_intervals() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ANISKIP_FOUND)

    async with httpx.AsyncClient(transport=json_transport({"GET api.aniskip.com/v2/skip-times/21/5": respond})) as c:
        intervals = await AniSkipClient(c).skip_times(21, 5.0)
        assert intervals == [SkipInterval("op", 13.891, 123.834), SkipInterval("ed", 1300.5, 1390.0)]
        assert seen[0].url.params.get_list("types[]") == ["op", "ed"]
        assert seen[0].url.params["episodeLength"] == "0"
        # AniSkip answers 404 when it has nothing.
        assert await AniSkipClient(c).skip_times(21, 6.0) == []


@pytest.mark.parametrize(
    ("query", "year", "expected"),
    [
        ("Sousou no Frieren", None, 52991),
        ("Sousou no Frieren Season 2", None, 59978),
        ("Frieren", None, 52991),
        ("Completely Different Show", None, None),
    ],
)
def test_best_match(query: str, year: int | None, expected: int | None) -> None:
    candidates = [
        Candidate(59978, ("Sousou no Frieren 2nd Season",), 2026),
        Candidate(52991, ("Sousou no Frieren",), 2023),
        Candidate(56885, ("Sousou no Frieren: ●● no Mahou",), 2023),
    ]
    assert best_match(query, candidates, year) == expected


def _resolver_client(calls: list[str]) -> httpx.AsyncClient:
    def track(name: str, response: httpx.Response) -> callable:
        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(name)
            return response

        return respond

    prefix = {
        "categories": [
            {
                "type": "anime",
                "items": [
                    {"id": 59978, "name": "Sousou no Frieren 2nd Season", "payload": {"start_year": 2026}},
                    {"id": 52991, "name": "Sousou no Frieren", "payload": {"start_year": 2023}},
                ],
            }
        ]
    }
    jikan = {
        "data": [
            {"mal_id": 52991, "title": "Sousou no Frieren", "title_english": "Frieren: Beyond Journey's End",
             "titles": [{"type": "Default", "title": "Sousou no Frieren"}], "year": 2023}
        ]
    }
    routes = {
        "POST graphql.anilist.co": track("anilist", httpx.Response(200, json={"data": {"Media": {"idMal": 52991}}})),
        "GET arm.haglund.dev": track("arm", httpx.Response(200, json={"anidb": 17617, "myanimelist": 52991})),
        "GET myanimelist.net/search/prefix.json": track("prefix", httpx.Response(200, json=prefix)),
        "GET api.jikan.moe/v4/anime": track("jikan", httpx.Response(200, json=jikan)),
    }
    return httpx.AsyncClient(transport=json_transport(routes))


async def test_mal_resolution_order_and_cache(store: Store) -> None:
    calls: list[str] = []
    async with _resolver_client(calls) as client:
        resolver = MalResolver(client, store)
        assert await resolver.resolve("jellyfin", Show("a", "Whatever", mal_id=123)) == 123
        assert calls == []
        assert await resolver.resolve("jellyfin", Show("b", "Whatever", external_ids={"AniList": "154587"})) == 52991
        assert await resolver.resolve("jellyfin", Show("c", "Whatever", external_ids={"AniDB": "17617"})) == 52991
        assert calls == ["anilist", "arm"]

        # Romaji titles match on MAL's prefix search; English ones fall through to Jikan.
        assert await resolver.resolve("local", Show("d", "Sousou no Frieren")) == 52991
        assert calls[-1] == "prefix"
        assert await resolver.resolve("local", Show("e", "Frieren: Beyond Journey's End")) == 52991
        assert calls[-2:] == ["prefix", "jikan"]

        # Cached: no more requests.
        before = len(calls)
        assert await resolver.resolve("local", Show("d", "Sousou no Frieren")) == 52991
        assert len(calls) == before
        assert store.get_mal_id("local", "e") == 52991


async def test_mal_search_survives_outages(store: Store) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("down", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
        resolver = MalResolver(client, store)
        assert await resolver.resolve("local", Show("x", "Sousou no Frieren", external_ids={"AniList": "1"})) is None
        assert store.get_mal_id("local", "x") is None


async def test_skip_engine_caches_intervals(store: Store) -> None:
    hits: list[str] = []

    def aniskip(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        return httpx.Response(200, json=ANISKIP_FOUND)

    async with httpx.AsyncClient(transport=json_transport({"GET api.aniskip.com": aniskip})) as client:
        engine = SkipEngine(client, store)
        show, episode = Show("s", "Frieren", mal_id=52991), Episode("s", 3)
        info = await engine.lookup("local", show, episode)
        assert info.mal_id == 52991 and [i.kind for i in info.intervals] == ["op", "ed"]
        assert info.summary == "OP 0:13–2:03 · ED 21:40–23:10"
        again = await engine.lookup("local", show, episode)
        assert again.intervals == info.intervals
        assert hits == ["/v2/skip-times/52991/3"]

        nothing = await engine.lookup("local", Show("t", "Unknown thing"), episode)
        assert nothing.mal_id is None and nothing.summary.startswith("no skip data")
