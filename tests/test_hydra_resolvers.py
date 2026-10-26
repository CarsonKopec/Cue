"""Hydra's public play-button data and server menu, without live requests."""

from __future__ import annotations

import json

import httpx
import pytest

from cue.providers import hydra_resolvers as resolver
from cue.providers.base import ProviderError, Stream


PAGE = "https://hydrahd.com/movie/1091/the-thing"
AGENT = "Cue resolver test"
MEDIA = "https://cdn.test/video.m3u8"

# Distilled from the supplied site's movie/episode scripts. These contain only
# the literal request data, not executable third-party advertising scripts.
MOVIE = """<script>
$(document).ready(function(){
    $('.ajaxlink_mov0').click(function(){
        $.get('/ajax/mov_0.php', {"i":"tt0084787","t":"1091"}, function(data){
            $('.loader').html(data);
        });
    });
});
</script>"""
TELEVISION = """<script>
$(document).ready(function(){
    $('.ajaxlink_tv0').click(function(){
        let isAutoNextEnabled = JSON.parse(localStorage.getItem("autoNext")) || false;
        let url = isAutoNextEnabled ? '/ajax/auto_tv.php' : '/ajax/tv_0.php';
        $.get(url, {"i":"tt0903747","t":"1396","s":"2","e":"3"}, function(data){
            $('.loader').html(data);
        });
    });
});
</script>"""


def menu(url="https://supported.test/embed/1", name="Public", attributes=""):
    return f'<div class="iframe-server-button" data-link="{url}" {attributes}>{name}</div>'


@pytest.fixture
def server_calls(monkeypatch):
    calls = []

    async def resolve(client, url, *, referer, user_agent):
        calls.append((url, referer, user_agent))
        return [Stream(MEDIA)]

    monkeypatch.setattr(resolver, "SUPPORTED_HOSTS", {"supported.test"})
    monkeypatch.setattr(resolver, "resolve_server", resolve)
    return calls


@pytest.mark.parametrize("html, path, params", [
    (MOVIE, "/ajax/mov_0.php", {"i": "tt0084787", "t": "1091"}),
    (TELEVISION, "/ajax/tv_0.php", {"i": "tt0903747", "t": "1396", "s": "2", "e": "3"}),
])
async def test_observed_bootstrap_selects_literal_public_request(html, path, params, server_calls):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=menu())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), auth=("private", "password"),
        headers={"Authorization": "Bearer private", "X-Private": "secret"},
        cookies={"session": "private"},
    ) as client:
        streams = await resolver.resolve_page(client, PAGE, html, user_agent=AGENT)
    assert [stream.url for stream in streams] == [MEDIA]
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == path
    assert dict(request.url.params) == params
    assert request.headers["referer"] == PAGE
    assert request.headers["user-agent"] == AGENT
    assert request.headers["x-requested-with"] == "XMLHttpRequest"
    assert not {"authorization", "cookie", "x-private"} & set(request.headers)
    assert server_calls == [("https://supported.test/embed/1", "https://hydrahd.com/", AGENT)]


@pytest.mark.parametrize("params", [
    '[]', '{"i":"tt1"}', '{"i":"tt1","t":"2","token":"private"}',
    '{"i":"tt1","t":2}', '{"i":null,"t":"2"}', '{"i":"bad","t":"2"}',
    '{"i":"tt1","t":"../2"}', '{"i":"tt1","t":"2"',
])
async def test_invalid_movie_request_data_is_not_sent(params):
    html = f"<script>$('.ajaxlink_mov0').click(function(){{ $.get('/ajax/mov_0.php', {params}); }});</script>"
    requests = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: requests.append(request))) as client:
        with pytest.raises(ProviderError):
            await resolver.resolve_page(client, PAGE, html, user_agent=AGENT)
    assert requests == []


@pytest.mark.parametrize("html", [
    TELEVISION.replace('"s":"2"', '"s":"-1"'),
    TELEVISION.replace('"e":"3"', '"e":"next"'),
    TELEVISION.replace("'/ajax/tv_0.php'", "'/ajax/unknown.php'"),
    MOVIE.replace('{"i":"tt0084787","t":"1091"}', 'getPrivateData()'),
])
async def test_dynamic_or_unrecognized_bootstraps_are_not_executed(html):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))) as client:
        with pytest.raises(ProviderError):
            await resolver.resolve_page(client, PAGE, html, user_agent=AGENT)


def test_menu_preserves_nested_names_deduplicates_and_skips_premium():
    html = """
    <div class="outer"><div class="iframe-server-button" data-link="https://supported.test/a?x=1&amp;y=2">
      <div><span>Hydra 1</span></div><span>HD</span>
    </div></div>
    <div class="iframe-server-button" data-link="https://supported.test/a?x=1&amp;y=2">Duplicate</div>
    <div class="iframe-server-button premium" data-link="https://supported.test/premium">Premium</div>
    <div class="iframe-server-button" data-premium="true" data-link="https://supported.test/private">Private</div>
    <div class="iframe-server-button" data-link="/embed/local">Local</div>
    """
    assert resolver.server_choices(html, PAGE) == [
        resolver.ServerChoice("https://supported.test/a?x=1&y=2", "Hydra 1"),
        resolver.ServerChoice("https://hydrahd.com/embed/local", "Local"),
    ]


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,x", "https://user:pass@supported.test/x",
    "https://supported.test:invalid/x", "https://supported.test:8123/x", "https://[invalid/x", "",
])
def test_menu_rejects_invalid_server_urls(url):
    assert resolver.server_choices(menu(url), PAGE) == []


async def test_unsupported_servers_are_not_requested(server_calls):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))) as client:
        with pytest.raises(ProviderError, match="no supported"):
            await resolver.resolve_page(client, PAGE, menu("https://unrelated.test/embed/1"), user_agent=AGENT)
    assert server_calls == []


async def test_server_failures_fall_back_then_stop_on_success(monkeypatch):
    calls = []

    async def resolve(client, url, **kwargs):
        calls.append(url)
        if url.endswith("/1"):
            raise ProviderError("temporarily unavailable")
        if url.endswith("/2"):
            return []
        return [Stream(MEDIA)]

    monkeypatch.setattr(resolver, "SUPPORTED_HOSTS", {"supported.test"})
    monkeypatch.setattr(resolver, "resolve_server", resolve)
    html = "".join(menu(f"https://supported.test/{i}") for i in range(1, 5))
    async with httpx.AsyncClient() as client:
        streams = await resolver.resolve_page(client, PAGE, html, user_agent=AGENT)
    assert streams[0].url == MEDIA
    assert calls == [f"https://supported.test/{i}" for i in range(1, 4)]


async def test_failed_server_menu_is_bounded_and_reports_failures(monkeypatch):
    calls = []

    async def resolve(client, url, **kwargs):
        calls.append(url)
        raise ProviderError("server refused playback")

    monkeypatch.setattr(resolver, "SUPPORTED_HOSTS", {"supported.test"})
    monkeypatch.setattr(resolver, "resolve_server", resolve)
    html = "".join(menu(f"https://supported.test/{i}", name=f"Server {i}") for i in range(10))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderError, match="Server 0: server refused playback"):
            await resolver.resolve_page(client, PAGE, html, user_agent=AGENT)
    assert len(calls) == 6


@pytest.mark.parametrize("location", ["https://elsewhere.test/a", "http://hydrahd.com/a", "https://hydrahd.com:8443/a"])
async def test_ajax_refuses_redirect_outside_origin(location):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ProviderError, match="outside its origin"):
            await resolver.resolve_page(client, PAGE, MOVIE, user_agent=AGENT)
    assert len(requests) == 1


@pytest.mark.parametrize("location", ["/ajax/ready", "https://hydrahd.com:443/ajax/ready"])
async def test_ajax_follows_same_origin_redirect_without_cookie_leak(server_calls, location):
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": location, "set-cookie": "secret=private; Path=/"})
        return httpx.Response(200, text=menu())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await resolver.resolve_page(client, PAGE, MOVIE, user_agent=AGENT)
    assert requests[-1].url.path == "/ajax/ready"
    assert all("cookie" not in request.headers for request in requests)
    assert all(request.headers["referer"] == PAGE for request in requests)


async def test_ajax_redirect_loop_is_bounded():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "/ajax/loop"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ProviderError, match="redirect limit"):
            await resolver.resolve_page(client, PAGE, MOVIE, user_agent=AGENT)
    assert len(requests) == 4


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_ajax_http_errors_are_reported(status):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
        with pytest.raises(ProviderError, match=str(status)):
            await resolver.resolve_page(client, PAGE, MOVIE, user_agent=AGENT)


@pytest.mark.parametrize("body, headers", [
    ('<title>Just a moment...</title>', {}),
    ('<form id="challenge-form">Verify</form>', {}),
    ('<div class="g-recaptcha">Verify</div>', {}),
    ('challenge', {"cf-mitigated": "challenge"}),
])
async def test_ajax_challenges_are_reported_without_bypass(body, headers):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body, headers=headers))) as client:
        with pytest.raises(ProviderError, match="browser challenge"):
            await resolver.resolve_page(client, PAGE, MOVIE, user_agent=AGENT)


async def test_initial_page_challenge_is_not_requested(server_calls):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))) as client:
        with pytest.raises(ProviderError, match="browser challenge"):
            await resolver.resolve_page(client, PAGE, '<title>Access denied</title>' + menu(), user_agent=AGENT)
    assert server_calls == []


async def test_passive_js_detection_footer_does_not_block_real_menu(server_calls):
    footer = '<script>var a=document.createElement("script");a.src="/cdn-cgi/challenge-platform/scripts/jsd/main.js";</script>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=menu() + footer))) as client:
        streams = await resolver.resolve_page(client, PAGE, MOVIE + footer, user_agent=AGENT)
    assert streams[0].url == MEDIA
    assert len(server_calls) == 1


async def test_ajax_response_size_limit(monkeypatch):
    monkeypatch.setattr(resolver, "_MAX_BYTES", 32)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 33))) as client:
        with pytest.raises(ProviderError, match="too large"):
            await resolver.resolve_page(client, PAGE, MOVIE, user_agent=AGENT)


async def test_network_failure_becomes_provider_error():
    def respond(request):
        raise httpx.ConnectTimeout("host unreachable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ProviderError, match="ConnectTimeout"):
            await resolver.resolve_page(client, PAGE, MOVIE, user_agent=AGENT)


@pytest.mark.parametrize("via_ajax", [False, True])
@pytest.mark.parametrize("format", ["html", "json"])
async def test_direct_sources_keep_subtitle_metadata(format, via_ajax, server_calls):
    if format == "html":
        data = '<video><source src="https://cdn.test/video.m3u8" type="application/x-mpegURL"><track src="https://subs.test/en.vtt" srclang="en" label="English"></video>'
    else:
        data = json.dumps({"sources": [{"file": MEDIA}], "tracks": [{
            "file": "https://subs.test/en.vtt", "kind": "captions", "label": "English", "language": "en",
        }]})
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        streams = await resolver.resolve_page(client, PAGE, MOVIE if via_ajax else data, user_agent=AGENT)
    assert len(requests) == int(via_ajax)
    assert server_calls == []
    assert len(streams) == 1 and streams[0].url == MEDIA
    assert streams[0].headers == {"Referer": "https://hydrahd.com/", "User-Agent": AGENT}
    assert len(streams[0].subtitle_tracks) == 1
    track = streams[0].subtitle_tracks[0]
    assert (track.url, track.language, track.label) == ("https://subs.test/en.vtt", "en", "English")
    assert track.headers == {"Referer": "https://hydrahd.com/", "User-Agent": AGENT}


async def test_arbitrary_iframe_is_not_playable_media():
    html = '<iframe src="https://unrelated.test/embed/1"></iframe>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request"))) as client:
        with pytest.raises(ProviderError, match="no supported player configuration"):
            await resolver.resolve_page(client, PAGE, html, user_agent=AGENT)
