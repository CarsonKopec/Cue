"""Offline resolver regressions using synthetic pages and public format fixtures.

No test accesses the network or executes a server's JavaScript. The encoded
fixtures below are fixed bytes, independent of the implementation under test.
"""

from __future__ import annotations

import gzip
from typing import Any

import httpx
import pytest

from cue.providers.anime_resolvers import resolve_server, streams_from_payload
from cue.providers.base import ProviderError


ZOKO = "https://zokoanime.video/stream/mal/100/1/sub"
MEGAPLAY = "https://megaplay.buzz/stream/s-2/100/sub?s=tcdn"
WATCH = "https://catalog.test/watch/example-100?ep=200"
AGENT = "Cue-offline-test"
MEDIA = "https://media.test/master.m3u8?sample=1"

# Exact UTF-8 JSON: {"src":"https://media.test/master.m3u8?sample=1","title":"雪"}
# Published Zoko v23 serialization is byte XOR with b"otaku-embed-v1", then base64.
ZOKO_VALID = "FFYSGRYPX08KERBdBQtAWwwOEUQEQxYAF1lZXA4HFQ4HAwheF11bXhdcHxgEVkQPSU8WDBBBExNVVojw3w8Y"
# Exact decoded bytes: b'not json', b'[]', and b'{"src":123}', respectively.
ZOKO_BAD_JSON = "ARsVSx9eCgM="
ZOKO_BAD_SHAPE = "NCk="
ZOKO_BAD_SRC = "FFYSGRYPX1xQVhk="
# {"src":"https://media.test/master.m3u8","drm":{"widevine":"https://license.test/"}}
ZOKO_DRM = "FFYSGRYPX08KERBdBQtAWwwOEUQEQxYAF1lZXA4HFQ4HAwheF11GAVRVHRlDUQ4PEgQGABJEGFRNTkMDAVkVHlhKS0EfUgoaEg5bWQAeFkpGUAs="


def zoko_page(encoded: str = ZOKO_VALID) -> str:
    return f'<html><script>window.__P="{encoded}";</script><script src="/player.js?v=23"></script></html>'


async def resolve_page(
    body: str,
    *,
    url: str = ZOKO,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> tuple[list, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, text=body, headers=headers or {"Content-Type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        streams = await resolve_server(client, url, referer=WATCH, user_agent=AGENT)
    return streams, requests


async def test_zoko_published_bytes_decode_to_source_and_preserve_player_headers() -> None:
    streams, requests = await resolve_page(zoko_page())
    assert [stream.url for stream in streams] == [MEDIA]
    assert streams[0].headers == {"Referer": "https://zokoanime.video/", "User-Agent": AGENT}
    assert streams[0].subtitles is None
    assert len(requests) == 1
    assert requests[0].headers["Referer"] == WATCH
    assert requests[0].headers["User-Agent"] == AGENT


@pytest.mark.parametrize("encoded", ["!not-base64!", "A", ZOKO_BAD_JSON, ZOKO_BAD_SHAPE, ZOKO_BAD_SRC])
async def test_zoko_rejects_invalid_base64_json_shape_or_source(encoded: str) -> None:
    with pytest.raises(ProviderError):
        await resolve_page(zoko_page(encoded))


async def test_zoko_rejects_protected_public_payload() -> None:
    with pytest.raises(ProviderError, match="(?i)encrypted|drm"):
        await resolve_page(zoko_page(ZOKO_DRM))


@pytest.mark.parametrize("server", [ZOKO, MEGAPLAY])
async def test_known_server_accepts_explicit_json_source(server: str) -> None:
    streams, requests = await resolve_page(
        '{"src":"/media/sample.m3u8","quality":"1080p"}',
        url=server,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert streams[0].url == str(requests[0].url.copy_with(path="/media/sample.m3u8", query=None))
    assert streams[0].headers == {"Referer": server, "User-Agent": AGENT}
    assert streams[0].quality == "1080p"


async def test_megaplay_accepts_explicit_html_video_source() -> None:
    streams, requests = await resolve_page(
        '<video controls><source src="/media/play?id=7&amp;kind=hls" '
        'type="application/vnd.apple.mpegurl"></video>',
        url=MEGAPLAY,
    )
    assert [stream.url for stream in streams] == ["https://megaplay.buzz/media/play?id=7&kind=hls"]
    assert streams[0].headers == {"Referer": MEGAPLAY, "User-Agent": AGENT}
    assert len(requests) == 1


async def test_megaplay_accepts_json_sources_assignment_without_executing_script() -> None:
    streams, requests = await resolve_page(
        '<script>throw new Error("never execute"); '
        'const sources = [{"file":"https://media.test/video.mp4","label":"720p"}];</script>',
        url=MEGAPLAY,
    )
    assert [(stream.url, stream.quality) for stream in streams] == [("https://media.test/video.mp4", "720p")]
    assert len(requests) == 1


async def test_gzip_embed_response_is_decompressed_only_once() -> None:
    compressed = gzip.compress(zoko_page().encode())

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=compressed,
            headers={"Content-Type": "text/html", "Content-Encoding": "gzip", "Content-Length": str(len(compressed))},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        streams = await resolve_server(client, ZOKO, referer=WATCH, user_agent=AGENT)
    assert [stream.url for stream in streams] == [MEDIA]


async def test_json_source_retains_explicit_referer_override() -> None:
    streams, requests = await resolve_page(
        '{"sources":[{"src":"https://media.test/video.mp4",'
        '"headers":{"Referer":"https://provided-player.test/","X-Playback":"sample"}}]}',
        url=MEGAPLAY,
        headers={"Content-Type": "application/json"},
    )
    assert streams[0].headers == {
        "User-Agent": AGENT,
        "Referer": "https://provided-player.test/",
        "X-Playback": "sample",
    }
    assert len(requests) == 1


async def test_script_sources_with_sibling_drm_config_are_not_returned() -> None:
    with pytest.raises(ProviderError, match="(?i)encrypted|drm"):
        await resolve_page(
            '<script>const player = {sources: [{"file":"https://media.test/master.m3u8"}],'
            'drm: {widevine: {url: "https://license.test/"}}};</script>',
            url=MEGAPLAY,
        )


@pytest.mark.parametrize(
    "body",
    [
        '<iframe src="https://media.test/video.mp4"></iframe>',
        '<script src="https://media.test/video.mp4"></script>',
        '<a href="https://media.test/video.mp4">download</a>',
        '<img src="https://media.test/video.mp4">',
        '<picture><source src="https://media.test/video.mp4"></picture>',
        '<script>const unrelated = "https://media.test/video.mp4";</script>',
        '<script>sources = [{file: computeMediaUrl()}];</script>',
    ],
)
async def test_server_does_not_treat_scripts_iframes_or_unrelated_urls_as_media(body: str) -> None:
    with pytest.raises(ProviderError, match="(?i)source|format"):
        await resolve_page(body, url=MEGAPLAY)


async def test_megaplay_http_200_removed_content_page_has_clear_error() -> None:
    with pytest.raises(ProviderError, match="(?i)removed.*410"):
        await resolve_page(
            '<h1>We\'re Sorry!</h1><p>We can\'t find the file you are looking for.</p>'
            '<div class="error-code">Error Code: <span>410</span></div>',
            url=MEGAPLAY,
        )


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_access_denied_stops_after_one_request_without_retry(status: int) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, text=zoko_page(), headers={"Retry-After": "1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ProviderError, match=str(status)):
            await resolve_server(client, ZOKO, referer=WATCH, user_agent=AGENT)
    assert len(requests) == 1


@pytest.mark.parametrize("status", [404, 410])
async def test_removed_http_status_is_reported(status: int) -> None:
    with pytest.raises(ProviderError, match=f"(?i)(removed|unavailable).*{status}"):
        await resolve_page("", url=MEGAPLAY, status=status)


@pytest.mark.parametrize(
    "body,headers",
    [
        ('<title>Just a moment...</title>' + zoko_page(), {}),
        ('<div id="challenge-form">Verify you are human</div>' + zoko_page(), {}),
        (zoko_page(), {"cf-mitigated": "challenge"}),
    ],
)
async def test_browser_challenge_is_reported_even_if_source_is_present(body: str, headers: dict[str, str]) -> None:
    with pytest.raises(ProviderError, match="(?i)challenge"):
        await resolve_page(body, headers=headers)


@pytest.mark.parametrize(
    "url",
    [
        "https://unsupported.test/embed/1",
        "https://zokoanime.video.attacker.test/embed/1",
        "https://user:password@zokoanime.video/embed/1",
        "https://zokoanime.video:8443/embed/1",
        "file:///media/video.mp4",
        "javascript:alert(1)",
        "https://[broken",
    ],
)
async def test_unsupported_or_malformed_server_url_is_rejected_without_request(url: str) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Unexpected network request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(ProviderError):
            await resolve_server(client, url, referer=WATCH, user_agent=AGENT)


async def test_same_origin_redirect_uses_final_page_for_relative_media_and_referer() -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if len(requested) == 1:
            return httpx.Response(302, headers={"Location": "/player/final"})
        return httpx.Response(200, text='<video src="./sample.mp4"></video>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        streams = await resolve_server(client, MEGAPLAY, referer=WATCH, user_agent=AGENT)
    assert requested == [MEGAPLAY, "https://megaplay.buzz/player/final"]
    assert streams[0].url == "https://megaplay.buzz/player/sample.mp4"
    assert streams[0].headers["Referer"] == "https://megaplay.buzz/player/final"


@pytest.mark.parametrize(
    "target",
    ["https://unsupported.test/embed/1", ZOKO, "http://megaplay.buzz/embed/1", "https://megaplay.buzz:8443/embed/1"],
)
async def test_redirect_cannot_leave_original_origin(target: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": target})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        with pytest.raises(ProviderError, match="(?i)redirect|origin"):
            await resolve_server(client, MEGAPLAY, referer=WATCH, user_agent=AGENT)
    assert len(requests) == 1


async def test_redirect_loop_is_bounded() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "/player/loop"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ProviderError, match="(?i)redirect.*limit"):
            await resolve_server(client, MEGAPLAY, referer=WATCH, user_agent=AGENT)
    assert len(requests) == 4


async def test_resolver_does_not_forward_caller_credentials_or_cookies() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=zoko_page())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        headers={"Authorization": "Bearer synthetic", "X-Secret": "synthetic"},
        cookies={"catalog_session": "synthetic"},
    ) as client:
        streams = await resolve_server(client, ZOKO, referer=WATCH, user_agent=AGENT)
    assert len(requests) == 1
    for header in ("Authorization", "Cookie", "X-Secret"):
        assert header not in requests[0].headers
        assert header not in streams[0].headers


@pytest.mark.parametrize("payload", [None, 123, True, {}, {"src": []}, {"html": '<video src="/file.mp4"></video>'}])
def test_nonmedia_json_shapes_return_no_streams(payload: Any) -> None:
    assert streams_from_payload(payload, MEGAPLAY, {}) == []


@pytest.mark.parametrize(
    "source",
    [
        {"src": "https://media.test/video.mp4"},
        {"url": "https://media.test/master.m3u8?x=1"},
        {"file": "https://media.test/manifest.mpd"},
        {"src": "https://media.test/play?id=1", "type": "application/vnd.apple.mpegurl"},
        {"src": "https://media.test/play?id=1", "mimeType": "video/mp4"},
    ],
)
def test_explicit_media_extensions_or_mime_types_are_accepted(source: dict[str, str]) -> None:
    streams = streams_from_payload({"sources": [source]}, MEGAPLAY, {"User-Agent": AGENT})
    assert len(streams) == 1
    assert streams[0].url == next(source[key] for key in ("src", "url", "file") if key in source)
    assert streams[0].headers == {"User-Agent": AGENT}


@pytest.mark.parametrize(
    "source",
    [
        {"src": "https://media.test/embed/1"},
        {"src": "https://media.test/video.mp4", "type": "text/html"},
        {"src": "https://media.test/video.mp4", "type": "iframe"},
        {"src": "javascript:alert(1)", "type": "hls"},
        {"src": "file:///video.mp4"},
        {"src": "https://user:password@media.test/video.mp4"},
        {"src": "https://media.test/video.mp4\r\nX-Test: value"},
        {"src": "https://[broken/video.mp4"},
    ],
)
def test_nonmedia_or_unsafe_source_urls_are_rejected(source: dict[str, str]) -> None:
    assert streams_from_payload({"sources": [source]}, MEGAPLAY, {}) == []


def test_payload_retains_headers_and_normalizes_quality_without_following_subtitles() -> None:
    payload = {
        "headers": {"X-Playback": "sample"},
        "sources": [
            {"src": "/media/sample.m3u8", "height": 1080, "headers": {"Referer": "https://player.test/"}},
            {"src": "https://media.test/video.mp4", "label": "1920x1080"},
        ],
        "subtitles": [{"src": "https://media.test/subtitle.mp4"}],
        "tracks": [{"file": "https://media.test/thumbnail.mp4"}],
    }
    initial = {"Referer": WATCH, "User-Agent": AGENT}
    streams = streams_from_payload(payload, MEGAPLAY, initial)
    assert [(stream.url, stream.quality) for stream in streams] == [
        ("https://megaplay.buzz/media/sample.m3u8", "1080p"),
        ("https://media.test/video.mp4", "1080p"),
    ]
    assert streams[0].headers == {"Referer": "https://player.test/", "User-Agent": AGENT, "X-Playback": "sample"}
    assert streams[1].headers["Referer"] == WATCH
    assert initial == {"Referer": WATCH, "User-Agent": AGENT}
    assert all(stream.subtitles is None for stream in streams)


def test_language_filter_inherits_container_language() -> None:
    payload = {
        "sub": {"sources": [{"src": "https://media.test/sub.mp4"}]},
        "dub": {"sources": [{"src": "https://media.test/dub.mp4"}]},
        "sources": [{"src": "https://media.test/alternate-dub.mp4", "language": "dub"}],
    }
    assert [stream.url for stream in streams_from_payload(payload, MEGAPLAY, {}, language="sub")] == [
        "https://media.test/sub.mp4"
    ]


def test_payload_deduplicates_same_source_without_losing_header_alternatives() -> None:
    payload = {"sources": [
        {"src": MEDIA},
        {"src": MEDIA},
        {"src": MEDIA, "headers": {"Referer": "https://alternate.test/"}},
    ]}
    streams = streams_from_payload(payload, MEGAPLAY, {"Referer": WATCH})
    assert len(streams) == 2
    assert [stream.headers["Referer"] for stream in streams] == [WATCH, "https://alternate.test/"]


@pytest.mark.parametrize("protection", [{"drm": {"widevine": "https://license.test/"}}, {"encrypted": True}, {"license_url": "https://license.test/"}])
def test_protected_sources_are_not_returned(protection: dict[str, Any]) -> None:
    with pytest.raises(ProviderError, match="(?i)encrypted|drm"):
        streams_from_payload({"sources": [{"src": MEDIA, **protection}]}, MEGAPLAY, {})


def test_header_newlines_are_rejected() -> None:
    with pytest.raises(ProviderError, match="(?i)header"):
        streams_from_payload({"sources": [{"src": MEDIA, "headers": {"X-Test": "value\r\nInjected: true"}}]}, MEGAPLAY, {})
