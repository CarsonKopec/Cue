"""Offline fixtures for VixSrc's publicly published, data-only player flow."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from cue.providers.base import ProviderError
from cue.providers.hydra_servers import SUPPORTED_HOSTS, resolve_server

OUTER = "https://vixsrc.to/movie/123?autoplay=true"
API = "https://vixsrc.to/api/movie/123?autoplay=true"
EMBED = "https://vixsrc.to/embed/456?token=public-gate&lang=en&cdn=primary"
REF = "https://hydrahd.gd/"
AGENT = "Cue-test"
INNER = """<script>
window.masterPlaylist = {
  params: {'token': 'public-media', 'expires': '1234567', 'asn': ''},
  url: 'https://vixsrc.to/playlist/456?existing=yes',
}
window.canPlayFHD = true
</script>"""
# Shape of the live master playlist: every rendition is DEFAULT=NO.
MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="English",DEFAULT=YES,AUTOSELECT=YES,FORCED=NO,LANGUAGE="eng",URI="https://vixsrc.to/playlist/456?type=audio&rendition=eng"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Italian [Forced]",DEFAULT=NO,AUTOSELECT=NO,FORCED=NO,LANGUAGE="forced-ita",URI="https://vixsrc.to/playlist/456?type=subtitle&rendition=1-forced-ita"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English [CC]",DEFAULT=NO,AUTOSELECT=NO,FORCED=NO,LANGUAGE="eng",URI="https://vixsrc.to/playlist/456?type=subtitle&rendition=2-eng"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",DEFAULT=NO,AUTOSELECT=NO,FORCED=NO,LANGUAGE="eng",URI="/playlist/456?type=subtitle&rendition=3-eng"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Greek",DEFAULT=NO,AUTOSELECT=NO,FORCED=NO,LANGUAGE="gre",URI="https://vixsrc.to/playlist/456?type=subtitle&rendition=4-gre"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Broken",LANGUAGE="eng",URI="javascript:alert(1)"
#EXT-X-STREAM-INF:BANDWIDTH=4500000,RESOLUTION=1920x1080,AUDIO="audio",SUBTITLES="subs"
https://vixsrc.to/playlist/456?type=video&rendition=1080p
"""


async def resolve_fixture(*, outer: str = OUTER, body: str = INNER, api_data=None, statuses=None, response_headers=None, playlist: str = MASTER):
    requests = []
    statuses = statuses or {}
    response_headers = response_headers or {}

    def handle(request):
        requests.append(request)
        path = request.url.path
        kwargs = {}
        if path.startswith("/api/"):
            kwargs["json"] = api_data if api_data is not None else {"src": EMBED}
        elif path.startswith("/embed/"):
            kwargs["text"] = body
        elif path.startswith("/playlist/"):
            kwargs["text"] = playlist
        else:
            kwargs["text"] = "<html>Public movie player</html>"
        return httpx.Response(statuses.get(path, 200), headers=response_headers.get(path, {}), **kwargs)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), headers={"Authorization": "private"}, cookies={"session":"private"}) as client:
        streams = await resolve_server(client, outer, referer=REF, user_agent=AGENT)
    return streams, requests


async def test_movie_uses_published_flow_and_preserves_headers():
    streams, requests = await resolve_fixture()
    assert SUPPORTED_HOSTS == {"vixsrc.to"}
    assert [str(r.url) for r in requests] == [OUTER, API, EMBED, streams[0].url]
    assert [r.headers["referer"] for r in requests] == [REF, OUTER, "https://vixsrc.to/", EMBED]
    assert all(r.headers["user-agent"] == AGENT for r in requests)
    assert all("cookie" not in r.headers and "authorization" not in r.headers for r in requests)
    assert len(streams) == 1
    assert urlsplit(streams[0].url).path == "/playlist/456"
    assert parse_qs(urlsplit(streams[0].url).query) == {
        "existing":["yes"], "token":["public-media"], "expires":["1234567"],
        "cdn":["primary"], "h":["1"], "lang":["en"],
    }
    assert streams[0].headers == {"Referer":EMBED, "User-Agent":AGENT}


async def test_master_playlist_subtitles_become_tracks_with_full_subtitles_first():
    streams, _ = await resolve_fixture()
    tracks = streams[0].subtitle_tracks
    assert [(t.label, t.language) for t in tracks] == [
        ("English", "eng"), ("Greek", "gre"), ("English [CC]", "eng"), ("Italian [Forced]", "forced-ita"),
    ]
    assert tracks[0].url == "https://vixsrc.to/playlist/456?type=subtitle&rendition=3-eng"
    # Tracks inherit the stream's Referer/User-Agent when Cue downloads them.
    assert all(t.headers is None for t in tracks)


@pytest.mark.parametrize(("playlist", "status"), [
    (MASTER, 403), (MASTER, 404), ("<html>Not a playlist</html>", 200),
    ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvideo.m3u8\n", 200),
])
async def test_unreadable_or_subtitle_free_master_still_plays(playlist, status):
    streams, _ = await resolve_fixture(playlist=playlist, statuses={"/playlist/456": status})
    assert len(streams) == 1
    assert streams[0].subtitle_tracks is None


async def test_tv_preserves_season_and_episode_in_published_route():
    _, requests = await resolve_fixture(outer="https://vixsrc.to/tv/1396/2/11?primaryColor=B20710")
    assert str(requests[1].url) == "https://vixsrc.to/api/tv/1396/2/11?primaryColor=B20710"


@pytest.mark.parametrize("url", [
    "https://ythd.org/embed/123/", "https://www.1src.org/embed/movie/123",
    "https://vixsrc.to.evil.test/movie/123", "https://user:pass@vixsrc.to/movie/123",
    "http://vixsrc.to/movie/123", "https://vixsrc.to:999/movie/123",
    "https://vixsrc.to/movie/0", "https://vixsrc.to/tv/123/1", "https://vixsrc.to/embed/123",
    "https://vixsrc.to/other", "javascript:alert(1)", "https://vixsrc.to:bad/movie/123",
])
async def test_unsupported_urls_fail_before_network(url):
    def no_request(request):
        pytest.fail("Unsupported URL must not make a request")
    async with httpx.AsyncClient(transport=httpx.MockTransport(no_request)) as client:
        with pytest.raises(ProviderError):
            await resolve_server(client,url,referer=REF,user_agent=AGENT)


@pytest.mark.parametrize("stage", ["/movie/123", "/api/movie/123", "/embed/456"])
@pytest.mark.parametrize("status", [401,403,429,404,410])
async def test_restrictions_and_missing_content_stop_each_stage(stage,status):
    with pytest.raises(ProviderError,match=str(status)):
        await resolve_fixture(statuses={stage:status})


@pytest.mark.parametrize("body", [
    '<title>Just a moment</title>', '<div>Verify you are human</div>',
    '<script src="/cdn-cgi/challenge-platform/example.js"></script>',
])
async def test_browser_challenges_are_not_bypassed(body):
    with pytest.raises(ProviderError,match="challenge"):
        await resolve_fixture(body=body)


async def test_cloudflare_mitigated_header_is_respected():
    with pytest.raises(ProviderError,match="challenge"):
        await resolve_fixture(response_headers={"/embed/456":{"cf-mitigated":"challenge"}})


@pytest.mark.parametrize("src", ["https://evil.test/embed/456", "//evil.test/embed/456", "javascript:alert(1)", "/arbitrary/456", "/embed/0", "https://vixsrc.to:8443/embed/456", None])
async def test_embed_must_stay_on_supported_origin_and_path(src):
    with pytest.raises(ProviderError,match="embed"):
        await resolve_fixture(api_data={"src":src})


@pytest.mark.parametrize("config", [
    "{params:{token:compute()},url:'https://vixsrc.to/playlist/1'}",
    "{params:{token:'one'+'two'},url:'https://vixsrc.to/playlist/1'}",
    "{params:{token:'one'},url:'https://vixsrc.to/playlist/1'} || attack()",
    "{params:{token:'one'},url:'https://evil.test/playlist/1'}",
    "{params:{token:'one'},url:'https://vixsrc.to/not-media/1'}",
    "{params:{token:'one',token:'two'},url:'https://vixsrc.to/playlist/1'}",
    "{params:'one',url:'https://vixsrc.to/playlist/1'}",
    "{params:{token:'one'},url:'https://vixsrc.to/playlist/1',drm:'widevine'}",
    "{params:{token:'one'},url:'https://vixsrc.to/playlist/1',encrypted:'true'}",
])
async def test_rejects_computed_malformed_or_protected_player_data(config):
    with pytest.raises(ProviderError):
        await resolve_fixture(body=f"<script>window.masterPlaylist={config};</script>")


async def test_no_literal_configuration_is_clear_error():
    with pytest.raises(ProviderError,match="configuration"):
        await resolve_fixture(body="<div>Player unavailable</div>")


async def test_plain_sources_preserve_subtitle_metadata_and_referrer_policy():
    body = '<video><source src="https://cdn.test/file.m3u8"><track src="https://subs.test/en.vtt" srclang="en" label="English"></video>'
    streams, _ = await resolve_fixture(body=body)
    assert streams[0].url == "https://cdn.test/file.m3u8"
    assert streams[0].headers == {"Referer":"https://vixsrc.to/", "User-Agent":AGENT}
    assert streams[0].subtitle_tracks[0].language == "en"
    assert streams[0].subtitle_tracks[0].label == "English"


async def test_relative_playlist_and_default_language():
    streams, _ = await resolve_fixture(body="<script>window.masterPlaylist={params:{token:'a&b'},url:'/playlist/1'};</script>",api_data={"src":"/embed/456"})
    query = parse_qs(urlsplit(streams[0].url).query)
    assert query == {"token":["a&b"],"lang":["en"],"h":["1"]}


async def test_cross_origin_redirect_rejected_without_following():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(302,headers={"location":"https://evil.test/movie/123"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError,match="origin"):
            await resolve_server(client,OUTER,referer=REF,user_agent=AGENT)
    assert len(requests)==1


async def test_oversized_page_is_rejected():
    with pytest.raises(ProviderError,match="size"):
        await resolve_fixture(body="x"*(2*1024*1024+1))
