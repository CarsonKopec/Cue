"""Offline regression coverage for HiAnime discovery and resolver orchestration."""

from __future__ import annotations

import base64
from collections.abc import Callable
from html import escape
from typing import Any

import httpx
import pytest

from cue.providers import anime
from cue.providers.base import Episode, ProviderConfigError, ProviderError, Show, Stream


SHOW_ID = "example-show-10492"
EPISODE = Episode(show_id=SHOW_ID, number=1)
EPISODE_ID = "76543"
ZOKO = "https://zokoanime.video/stream/mal/100/1/sub"
MEGAPLAY = "https://megaplay.buzz/stream/s-2/100/sub?s=tcdn"


def server(url: str, name: str = "ZokoAnime", language: str = "sub") -> str:
    encoded = base64.b64encode(url.encode()).decode()
    return (
        f'<div class="item server-item" data-type="{escape(language, quote=True)}" '
        f'data-server-name="{escape(name, quote=True)}" data-hash="{encoded}"></div>'
    )


@pytest.fixture
async def provider_factory():
    providers: list[anime.AnimeProvider] = []

    def make(
        payload: Any = None,
        *,
        options: dict[str, Any] | None = None,
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
        cached: bool = True,
    ) -> anime.AnimeProvider:
        def respond(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/theme/episode/servers"
            assert request.url.params["episodeId"] == EPISODE_ID
            return httpx.Response(200, json=payload)

        provider = anime.AnimeProvider(
            {"base_api": "https://catalog.test", **(options or {})},
            transport=httpx.MockTransport(handler or respond),
        )
        providers.append(provider)
        if cached:
            provider._episode_ids[(SHOW_ID, 1)] = EPISODE_ID
        return provider

    yield make
    for provider in providers:
        await provider.aclose()


def test_server_discovery_preserves_order_and_language() -> None:
    html = (
        server(ZOKO, " First & original ", " SUB ")
        + server(ZOKO, "Duplicate", "sub")
        + server(ZOKO, "Dub label", "dub")
        + server(MEGAPLAY, "", "")
    )
    assert anime._extract_server_choices(html) == [
        (ZOKO, "First & original", "sub"),
        (ZOKO, "Dub label", "dub"),
        (MEGAPLAY, "Unknown server", ""),
    ]


def test_server_discovery_matches_complete_class_token() -> None:
    html = server(ZOKO).replace('class="item server-item"', 'class="server-item\titem"')
    html += server(MEGAPLAY).replace("server-item", "not-server-item")
    assert anime._extract_server_choices(html) == [(ZOKO, "ZokoAnime", "sub")]


@pytest.mark.parametrize(
    "raw",
    [
        b"javascript:alert(1)",
        b"file:///C:/example/video.mp4",
        b"//zokoanime.video/stream/mal/100/1/sub",
        b"https://[broken",
        b"https:///missing-host",
        b"\xff\xfe",
    ],
)
def test_server_discovery_skips_invalid_decoded_urls(raw: bytes) -> None:
    encoded = base64.b64encode(raw).decode()
    html = f'<div class="server-item" data-hash="{encoded}"></div>'
    assert anime._extract_server_choices(html) == []


@pytest.mark.parametrize("encoded", ["", "not-base64!!!", "a"])
def test_server_discovery_skips_invalid_hashes(encoded: str) -> None:
    html = f'<div class="server-item" data-hash="{encoded}"></div>'
    assert anime._extract_server_choices(html) == []


@pytest.mark.parametrize("language", ["both", "fr", ""])
def test_invalid_language_option_is_rejected(language: str) -> None:
    with pytest.raises(ProviderConfigError, match="language"):
        anime.AnimeProvider({"language": language})


async def test_explicit_direct_sources_preserve_metadata(provider_factory) -> None:
    provider = provider_factory(
        {
            "sources": [
                {
                    "file": "https://media.test/master.m3u8?token=synthetic",
                    "quality": "1080p",
                    "headers": {"Referer": "https://player.test/", "X-Playback": "sample"},
                },
                {"url": "https://media.test/video.mp4", "label": "720p"},
            ]
        }
    )
    streams = await provider.stream(EPISODE)
    assert [(stream.url, stream.quality) for stream in streams] == [
        ("https://media.test/master.m3u8?token=synthetic", "1080p"),
        ("https://media.test/video.mp4", "720p"),
    ]
    assert streams[0].headers["Referer"] == "https://player.test/"
    assert streams[0].headers["X-Playback"] == "sample"


async def test_explicit_media_type_allows_extensionless_source(provider_factory) -> None:
    provider = provider_factory(
        {"sources": [{"file": "https://media.test/play?id=123", "type": "application/vnd.apple.mpegurl"}]}
    )
    streams = await provider.stream(EPISODE)
    assert [stream.url for stream in streams] == ["https://media.test/play?id=123"]


@pytest.mark.parametrize("language", ["sub", "dub"])
async def test_direct_sources_filter_explicit_language(provider_factory, language: str) -> None:
    provider = provider_factory(
        {
            "sources": [
                {"file": "https://media.test/sub.mp4", "language": "sub"},
                {"file": "https://media.test/dub.mp4", "language": "dub"},
            ]
        },
        options={"language": language},
    )
    streams = await provider.stream(EPISODE)
    assert [stream.url for stream in streams] == [f"https://media.test/{language}.mp4"]


async def test_generic_embed_url_is_not_a_direct_stream(provider_factory) -> None:
    provider = provider_factory({"sources": [{"url": "https://unsupported.test/embed/123"}]})
    with pytest.raises(ProviderError):
        await provider.stream(EPISODE)


async def test_uncached_episode_is_discovered_before_server_request(provider_factory) -> None:
    requested: list[httpx.URL] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        if request.url.path == "/api/theme/episode/list/10492":
            html = (
                f'<div class="ep-item" data-number="1" data-id="{EPISODE_ID}">'
                f'<a href="/watch/{SHOW_ID}?ep={EPISODE_ID}" title="Pilot">1</a></div>'
            )
            return httpx.Response(200, json={"status": True, "html": html})
        assert request.url.path == "/api/theme/episode/servers"
        assert request.url.params["episodeId"] == EPISODE_ID
        return httpx.Response(200, json={"sources": [{"file": "https://media.test/video.mp4"}]})

    provider = provider_factory(handler=respond, cached=False)
    await provider.stream(EPISODE)
    await provider.stream(EPISODE)
    assert [url.path for url in requested] == [
        "/api/theme/episode/list/10492",
        "/api/theme/episode/servers",
        "/api/theme/episode/servers",
    ]


async def test_missing_episode_never_substitutes_display_number(provider_factory) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/theme/episode/list/10492"
        html = (
            '<div class="ep-item" data-number="2" data-id="999">'
            f'<a href="/watch/{SHOW_ID}?ep=999">2</a></div>'
        )
        return httpx.Response(200, json={"status": True, "html": html})

    provider = provider_factory(handler=respond, cached=False)
    with pytest.raises(ProviderError):
        await provider.stream(EPISODE)


async def test_failed_episode_response_cannot_populate_cache(provider_factory) -> None:
    html = (
        f'<div class="ep-item" data-number="1" data-id="{EPISODE_ID}">'
        f'<a href="/watch/{SHOW_ID}?ep={EPISODE_ID}">1</a></div>'
    )
    provider = provider_factory(
        handler=lambda request: httpx.Response(200, json={"status": False, "html": html}),
        cached=False,
    )
    with pytest.raises(ProviderError):
        await provider.episodes(Show(id=SHOW_ID, title="Example show"))
    assert (SHOW_ID, 1) not in provider._episode_ids


@pytest.mark.parametrize("language", ["sub", "dub"])
async def test_resolves_only_requested_language_and_deduplicates(
    provider_factory, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    dub = ZOKO.rsplit("/", 1)[0] + "/dub"
    calls: list[tuple[str, str, str]] = []
    result = Stream("https://media.test/video.mp4", headers={"Referer": ZOKO}, quality="720p")

    async def resolve(client, url: str, *, referer: str, user_agent: str) -> list[Stream]:
        calls.append((url, referer, user_agent))
        return [result]

    monkeypatch.setattr(anime, "resolve_server", resolve)
    provider = provider_factory(
        {"html": server(ZOKO) + server(ZOKO, "Duplicate") + server(dub, language="dub")},
        options={"language": language, "agent": "Cue-test-agent"},
    )
    streams = await provider.stream(EPISODE)
    assert streams == [result]
    assert calls == [
        (
            ZOKO if language == "sub" else dub,
            f"https://catalog.test/watch/{SHOW_ID}?ep={EPISODE_ID}",
            "Cue-test-agent",
        )
    ]


async def test_default_language_does_not_silently_fall_back_to_dub(
    provider_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected(*args, **kwargs):
        pytest.fail("A SUB request must not resolve a DUB server")

    monkeypatch.setattr(anime, "resolve_server", unexpected)
    provider = provider_factory({"html": server(ZOKO.rsplit("/", 1)[0] + "/dub", language="dub")})
    with pytest.raises(ProviderError, match="(?i)sub"):
        await provider.stream(EPISODE)


async def test_catalog_credentials_do_not_leak_to_resolver(
    provider_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def resolve(client, url: str, *, referer: str, user_agent: str) -> list[Stream]:
        request = client.build_request("GET", url)
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        assert "X-Catalog-Secret" not in request.headers
        return [Stream("https://media.test/video.mp4")]

    monkeypatch.setattr(anime, "resolve_server", resolve)
    provider = provider_factory(
        {"html": server(ZOKO)},
        options={
            "headers": {
                "Authorization": "Bearer synthetic-catalog-token",
                "Cookie": "session=synthetic-catalog-cookie",
                "X-Catalog-Secret": "synthetic-custom-secret",
            }
        },
    )
    provider._client.cookies.set("session", "synthetic-stored-cookie", domain="catalog.test")
    assert await provider.stream(EPISODE)


async def test_failed_server_does_not_hide_a_playable_alternative(
    provider_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def resolve(client, url: str, *, referer: str, user_agent: str) -> list[Stream]:
        calls.append(url)
        if url == ZOKO:
            raise ProviderError("Synthetic server is unavailable")
        return [Stream("https://media.test/master.m3u8", quality="1080p")]

    monkeypatch.setattr(anime, "resolve_server", resolve)
    provider = provider_factory({"html": server(ZOKO) + server(MEGAPLAY, "HD-1")})
    streams = await provider.stream(EPISODE)
    assert calls == [ZOKO, MEGAPLAY]
    assert [stream.url for stream in streams] == ["https://media.test/master.m3u8"]


async def test_all_failed_servers_report_actionable_context(
    provider_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def resolve(client, url: str, *, referer: str, user_agent: str) -> list[Stream]:
        raise ProviderError("Synthetic source has no public media URL")

    monkeypatch.setattr(anime, "resolve_server", resolve)
    provider = provider_factory({"html": server(MEGAPLAY, "HD-1")})
    with pytest.raises(ProviderError) as error:
        await provider.stream(EPISODE)
    assert "HD-1" in str(error.value)
    assert "no public media URL" in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": True, "html": ""},
        {"status": False, "sources": [{"file": "https://media.test/video.mp4"}]},
    ],
)
async def test_empty_or_failed_server_response_is_an_error(provider_factory, payload: Any) -> None:
    provider = provider_factory(payload)
    with pytest.raises(ProviderError):
        await provider.stream(EPISODE)


async def test_forbidden_catalog_response_stops_without_resolver(
    provider_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected(*args, **kwargs):
        pytest.fail("Do not resolve after catalog access was denied")

    monkeypatch.setattr(anime, "resolve_server", unexpected)
    provider = provider_factory(handler=lambda request: httpx.Response(403))
    with pytest.raises(ProviderError, match="403"):
        await provider.stream(EPISODE)
