"""Offline coverage for HydraHD's observed catalog routes and season identity."""

from __future__ import annotations

import asyncio
import inspect
from html import escape

import httpx
import pytest

from cue.providers import hydra
from cue.providers.base import Episode, ProviderConfigError, ProviderError, Show, Stream


SERIES = "/watchseries/breaking-bad-online-free"
MOVIE = "/movie/122655-watch-el-camino-a-breaking-bad-movie-2019-online"


def card(path: str, title: str = "Breaking Bad", year: int = 2008) -> str:
    return (
        '<figure class="figured"><a class="hthis" '
        f'href="{escape(path, quote=True)}" title="{escape(title, quote=True)} online free">'
        f'<span>HD</span><span>{year}</span><img alt="{escape(title, quote=True)}"></a></figure>'
    )


def episode_link(season: int | str, episode: int | str, title: str = "Pilot", slug: str = "breaking-bad") -> str:
    return (
        '<a href="#" class="dynamic-ep-link visitedep" '
        f'data-slug="{slug}" data-season="{season}" data-episode="{episode}">'
        f'Episode {episode}<span class="tv_episode_name">2008-01-20 - {title}</span></a>'
    )


def series_page(*episodes: tuple[int, int, str], title: str = "Breaking Bad") -> str:
    return f"<h1>{title}</h1>" + "".join(episode_link(*episode) for episode in episodes)


@pytest.fixture
async def provider_factory():
    providers = []

    def make(handler, **options):
        async def html_handler(request):
            response = handler(request)
            if inspect.isawaitable(response):
                response = await response
            # Response(text=...) defaults to text/plain in httpx; these are HTML fixtures.
            if response.headers.get("content-type", "").startswith("text/plain"):
                response.headers["content-type"] = "text/html; charset=utf-8"
            return response

        provider = hydra.HydraProvider(
            {"base_url": "https://catalog.test", **options}, transport=httpx.MockTransport(html_handler),
        )
        providers.append(provider)
        return provider

    yield make
    for provider in providers:
        await provider.aclose()


@pytest.mark.parametrize("url", ["file:///local", "javascript:void(0)", "https://user:secret@host.test", "https://@host.test", " https://host.test", "https://[broken", "https://host.test:wrong", "https://host.test/path", "https://host.test?q=1", "https://host.test/#x", 4, None])
def test_invalid_base_url_is_configuration_error(url):
    with pytest.raises(ProviderConfigError):
        hydra.HydraProvider({"base_url": url})


@pytest.mark.parametrize("timeout", [0, -1, "never", None, float("nan"), float("inf")])
def test_invalid_timeout_is_configuration_error(timeout):
    with pytest.raises(ProviderConfigError, match="timeout"):
        hydra.HydraProvider({"timeout": timeout})


async def test_public_defaults_and_no_anime_audio_or_skip_options(provider_factory):
    provider = provider_factory(lambda _: httpx.Response(200, text=""), base_url="https://hydrahd.com")
    assert provider.base_url == "https://hydrahd.com"
    assert provider.name == "hydra"
    assert provider.skip_supported is False
    assert not hasattr(provider, "audio_modes")
    assert not hasattr(provider, "language")
    assert not provider._client.follow_redirects
    assert not provider._resolver_client.follow_redirects


def test_cards_use_image_title_year_and_reject_other_links():
    parser = hydra._SearchParser("https://hydrahd.com/index.php?menu=search")
    parser.feed(
        card(SERIES, "Breaking & Bad")
        + card(SERIES, "Duplicate")
        + card(MOVIE, "El Camino", 2019)
        + card("/person/123", "A person")
        + card("https://advertisement.test" + SERIES)
        + card("javascript:alert(1)")
        + card(SERIES + "/season/1/episode/1")
        + card("https://hydrahd.com:444" + SERIES)
        + card("https://user:secret@hydrahd.com" + SERIES)
    )
    assert [(x.path, x.title, x.year) for x in parser.cards] == [
        (SERIES, "Breaking & Bad", 2008), (MOVIE, "El Camino", 2019),
    ]


def test_cards_accept_known_detail_domain_and_anchor_title_fallback():
    parser = hydra._SearchParser("https://hydrahd.com")
    parser.feed(card("https://hydrahd.gd" + MOVIE, "A Movie").replace('<img alt="A Movie">', ""))
    assert parser.cards[0].path == MOVIE
    assert parser.cards[0].title == "A Movie"


def test_cards_trim_observed_href_whitespace_without_accepting_internal_whitespace():
    parser = hydra._SearchParser("https://hydrahd.com")
    parser.feed(card(" \t" + MOVIE + " \n") + card(SERIES.replace("breaking", "break ing")))
    assert [item.path for item in parser.cards] == [MOVIE]


def test_episode_parser_validates_slug_numbers_and_class_tokens():
    parser = hydra._EpisodeParser(SERIES)
    parser.feed(
        series_page((2, 2, "Two"), (1, 1, "Pilot"), (1, 1, "Duplicate"), (0, 0, "Special"))
        + episode_link(1, 3, slug="unrelated-show")
        + episode_link("x", 4)
        + episode_link(1, "1.5")
        + episode_link(1, -1)
        + episode_link(1, 6).replace("dynamic-ep-link", "not-dynamic-ep-link")
    )
    assert parser.title == "Breaking Bad"
    assert parser.episodes == {2: {2: "Two"}, 1: {1: "Pilot"}, 0: {0: "Special"}}


async def test_search_expands_seasons_separates_history_and_keeps_movie_metadata(provider_factory):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/index.php":
            assert request.url.params == httpx.QueryParams({"menu": "search", "query": "Breaking & Bad"})
            return httpx.Response(200, text=card(SERIES) + card(MOVIE, "El Camino", 2019))
        assert request.url.path == SERIES
        return httpx.Response(200, text=series_page((2, 1, "Second season"), (1, 2, "Next"), (1, 1, "Pilot")))

    provider = provider_factory(respond)
    shows = await provider.search("  Breaking & Bad  ")
    assert [show.id for show in shows] == [f"{SERIES}#season=1", f"{SERIES}#season=2", MOVIE]
    assert [show.title for show in shows] == ["Breaking Bad — Season 1", "Breaking Bad — Season 2", "El Camino"]
    assert [show.episode_count for show in shows] == [2, 1, 1]
    assert shows[0].series_title == "Breaking Bad"
    assert shows[2].media_type == "movie"
    assert shows[2].year == 2019
    first, second = await provider.episodes(shows[0]), await provider.episodes(shows[1])
    assert [(ep.number, ep.season, ep.title) for ep in first] == [(1, 1, "Pilot"), (2, 1, "Next")]
    assert first[0].show_id != second[0].show_id
    assert first[0].number == second[0].number == 1
    assert len(requests) == 2  # expanded details are reused by the episode screen


async def test_movie_history_resume_restores_metadata_without_network(provider_factory):
    def respond(_):
        pytest.fail("A movie has one queue item; listing it does not need a request")

    provider = provider_factory(respond)
    restored = Show(MOVIE, "El Camino")
    episodes = await provider.episodes(restored)
    assert restored.media_type == "movie"
    assert restored.episode_count == 1
    assert restored.year == 2019
    assert episodes == [Episode(MOVIE, 1, title="El Camino")]


async def test_series_resume_restores_season_and_matching_title(provider_factory):
    provider = provider_factory(lambda _: httpx.Response(200, text=series_page((2, 1, "Seven Thirty-Seven"))))
    restored = Show(f"{SERIES}#season=2", "Breaking Bad — Season 2")
    episodes = await provider.episodes(restored)
    assert restored.series_title == "Breaking Bad"
    assert restored.media_type == "episode"
    assert episodes == [Episode(restored.id, 1, "Seven Thirty-Seven", season=2)]


async def test_search_tolerates_one_broken_series_but_not_all_failures(provider_factory):
    with_movie = True

    def respond(request):
        if request.url.path == "/index.php":
            return httpx.Response(200, text=card(SERIES) + (card(MOVIE) if with_movie else ""))
        return httpx.Response(503)

    provider = provider_factory(respond)
    assert [show.id for show in await provider.search("query")] == [MOVIE]
    with_movie = False
    with pytest.raises(ProviderError, match="503"):
        await provider.search("query")


async def test_search_bounds_series_fanout_and_result_pages(provider_factory):
    active, peak, details = 0, 0, 0

    async def respond(request):
        nonlocal active, peak, details
        if request.url.path == "/index.php":
            return httpx.Response(200, text="".join(card(f"/watchseries/show-{n}-online-free") for n in range(65)))
        details += 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        slug = request.url.path.removeprefix("/watchseries/").removesuffix("-online-free")
        active -= 1
        return httpx.Response(200, text="<h1>Title</h1>" + episode_link(1, 1, slug=slug))

    provider = provider_factory(respond)
    assert len(await provider.search("show")) == hydra._MAX_SEARCH_CARDS
    assert details == hydra._MAX_SEARCH_CARDS
    assert peak == hydra._SERIES_CONCURRENCY


async def test_blank_and_empty_search_results(provider_factory):
    count = 0

    def respond(_):
        nonlocal count
        count += 1
        return httpx.Response(200, text='<h1>Movie and TV Results For: nothing</h1><div class="browse-grid"></div>')

    provider = provider_factory(respond)
    assert await provider.search("   ") == []
    assert count == 0
    assert await provider.search("nothing") == []
    assert count == 1


async def test_unrecognized_search_page_is_not_reported_as_zero_matches(provider_factory):
    provider = provider_factory(lambda _: httpx.Response(200, text="<h1>Maintenance</h1>"))
    with pytest.raises(ProviderError, match="not recognized"):
        await provider.search("anything")


async def test_unknown_season_is_actionable(provider_factory):
    provider = provider_factory(lambda _: httpx.Response(200, text=series_page((1, 1, "Pilot"))))
    with pytest.raises(ProviderError, match="no longer lists season 2"):
        await provider.episodes(Show(f"{SERIES}#season=2", "Breaking Bad"))


@pytest.mark.parametrize("show_id", ["breaking-bad", SERIES, SERIES + "#season=-1", SERIES + "#season=NaN", "https://evil.test" + MOVIE, "/movie/../login", MOVIE + "?cookie=x"])
async def test_invalid_saved_identity_does_not_make_requests(provider_factory, show_id):
    provider = provider_factory(lambda _: pytest.fail("Invalid IDs must be rejected before requests"))
    with pytest.raises(ProviderError, match="saved title is invalid"):
        await provider.episodes(Show(show_id, "Broken"))


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_access_restrictions_are_not_bypassed(provider_factory, status):
    provider = provider_factory(lambda _: httpx.Response(status))
    with pytest.raises(ProviderError, match="cannot bypass"):
        await provider.search("show")


@pytest.mark.parametrize("html", ["<title>Just a moment...</title>", "<title>Attention Required! | Cloudflare</title>", '<form id="challenge-form"></form>', '<form action="/cdn-cgi/challenge"></form>'])
async def test_actual_challenge_pages_stop(provider_factory, html):
    provider = provider_factory(lambda _: httpx.Response(200, text=html))
    with pytest.raises(ProviderError, match="cannot bypass"):
        await provider.search("show")


async def test_passive_cloudflare_script_does_not_hide_valid_catalog(provider_factory):
    provider = provider_factory(lambda _: httpx.Response(200, text=card(MOVIE) + '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'))
    assert len(await provider.search("movie")) == 1


async def test_challenge_header_stops_even_with_normal_html(provider_factory):
    provider = provider_factory(lambda _: httpx.Response(200, text=card(MOVIE), headers={"cf-mitigated": "challenge"}))
    with pytest.raises(ProviderError, match="cannot bypass"):
        await provider.search("movie")


async def test_normal_com_to_gd_redirect_uses_final_page_address(provider_factory):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.host == "hydrahd.com":
            return httpx.Response(302, headers={"location": "https://hydrahd.gd" + request.url.path})
        return httpx.Response(200, text=card(MOVIE))

    provider = provider_factory(respond, base_url="https://hydrahd.com")
    assert len(await provider.search("movie")) == 1
    assert [r.url.host for r in requests] == ["hydrahd.com", "hydrahd.gd"]


@pytest.mark.parametrize("target", ["https://advertisement.test/", "https://hydrahd.com.evil.test/", "https://user:secret@hydrahd.gd/", "file:///private", "http://hydrahd.gd/", "https://hydrahd.gd:444/"])
async def test_external_or_insecure_redirects_are_not_followed(provider_factory, target):
    count = 0

    def respond(_):
        nonlocal count
        count += 1
        return httpx.Response(302, headers={"location": target})

    provider = provider_factory(respond)
    with pytest.raises(ProviderError, match="redirected"):
        await provider.search("show")
    assert count == 1


async def test_redirect_limit(provider_factory):
    count = 0

    def respond(_):
        nonlocal count
        count += 1
        return httpx.Response(302, headers={"location": "/another"})

    provider = provider_factory(respond)
    with pytest.raises(ProviderError, match="too many times"):
        await provider.search("show")
    assert count == 4


async def test_response_size_and_type_are_bounded(provider_factory):
    provider = provider_factory(lambda _: httpx.Response(200, text="x" * (hydra._MAX_PAGE_BYTES + 1)))
    with pytest.raises(ProviderError, match="too large"):
        await provider.search("show")
    other = provider_factory(lambda _: httpx.Response(200, json={"wrong": "API"}))
    with pytest.raises(ProviderError, match="unsupported catalog response"):
        await other.search("show")


async def test_network_failure_is_sanitized(provider_factory):
    def respond(_):
        raise httpx.ReadTimeout("sensitive URL secret")

    provider = provider_factory(respond)
    with pytest.raises(ProviderError, match="ReadTimeout") as caught:
        await provider.search("show")
    assert "secret" not in str(caught.value)


async def test_stream_uses_episode_route_final_url_and_separate_resolver_client(provider_factory, monkeypatch):
    from cue.providers import hydra_resolvers

    seen = []

    def respond(request):
        seen.append(request.url.path)
        if request.url.path == SERIES:
            return httpx.Response(200, text=series_page((2, 3, "Bit by a Dead Bee")), headers={"set-cookie": "catalog_cookie=secret; Path=/"})
        assert request.url.path == SERIES + "/season/2/episode/3"
        return httpx.Response(200, text="<h1>Player</h1>")

    async def resolve(client, page_url, html, *, user_agent):
        assert list(client.cookies.items()) == []
        assert page_url == "https://catalog.test" + SERIES + "/season/2/episode/3"
        assert html == "<h1>Player</h1>"
        assert user_agent == hydra.USER_AGENT
        return [Stream("https://media.test/video.m3u8")]

    monkeypatch.setattr(hydra_resolvers, "resolve_page", resolve)
    provider = provider_factory(respond)
    streams = await provider.stream(Episode(f"{SERIES}#season=2", 3, season=2))
    assert streams[0].url == "https://media.test/video.m3u8"
    assert seen == [SERIES, SERIES + "/season/2/episode/3"]


async def test_movie_stream_uses_movie_page(provider_factory, monkeypatch):
    from cue.providers import hydra_resolvers

    def respond(request):
        assert request.url.path == MOVIE
        return httpx.Response(200, text="movie player")

    async def resolve(client, page_url, html, *, user_agent):
        assert page_url == "https://catalog.test" + MOVIE
        assert html == "movie player"
        return [Stream("https://media.test/movie.mp4")]

    monkeypatch.setattr(hydra_resolvers, "resolve_page", resolve)
    provider = provider_factory(respond)
    assert len(await provider.stream(Episode(MOVIE, 1))) == 1


@pytest.mark.parametrize("episode, message", [(Episode(MOVIE, 2), "single playback item"), (Episode(MOVIE, float("nan")), "number is invalid"), (Episode(f"{SERIES}#season=2", 1, season=1), "season does not match")])
async def test_invalid_stream_selection_does_not_make_requests(provider_factory, episode, message):
    provider = provider_factory(lambda _: pytest.fail("Invalid selections must not fetch player pages"))
    with pytest.raises(ProviderError, match=message):
        await provider.stream(episode)


async def test_unknown_episode_does_not_fetch_invented_player_page(provider_factory):
    def respond(request):
        assert request.url.path == SERIES
        return httpx.Response(200, text=series_page((1, 1, "Pilot")))

    provider = provider_factory(respond)
    with pytest.raises(ProviderError, match="no longer lists this episode"):
        await provider.stream(Episode(f"{SERIES}#season=1", 99))


async def test_close_releases_both_clients(provider_factory):
    provider = provider_factory(lambda _: httpx.Response(200))
    await provider.aclose()
    assert provider._client.is_closed
    assert provider._resolver_client.is_closed
