"""Casting: HLS relay, DLNA directory and SSDP, with the TV played by an HTTP client."""

from __future__ import annotations

import asyncio
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from textual.widgets import Input

from cue.app import CueApp
from cue.cast import CastServer, hls, network
from cue.cast.server import ITEM_PROTOCOL, PLAYLIST_TYPE
from cue.cast.ssdp import Advertiser
from cue.config import CastConfig, Config, ConfigError, parse_config
from cue.player.session import PlaybackState
from cue.providers.base import Episode, ProviderError, Show, Stream, StreamSubtitle
from cue.screens.episodes import EpisodesScreen
from cue.screens.player import NowPlayingScreen
from cue.subtitles.provider import ProviderSubtitles

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",DEFAULT=YES,URI="audio/eng.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",URI="subs/eng.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=4500000,RESOLUTION=1920x1080,AUDIO="aud",SUBTITLES="subs"
video/1080.m3u8
"""
SHORT_MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-MAP:URI="init.mp4"
#EXTINF:6.0,
https://media.test/seg0.ts
#EXTINF:6.0,
seg1.ts
#EXTINF:4.5,
seg2.ts
#EXT-X-ENDLIST
"""
# 100 ten-second segments: a 1000-second episode.
LONG_MEDIA = "#EXTM3U\n#EXT-X-TARGETDURATION:10\n" + "".join(
    f"#EXTINF:10.0,\n{'https://media.test/' if n == 0 else ''}seg{n}.ts\n" for n in range(100)
) + "#EXT-X-ENDLIST\n"
BASE = "https://cdn.test/v/master.m3u8"
VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n"
SRT = "1\n00:00:01,000 --> 00:00:02,500\nBonjour\n"


def recorder():
    calls = []

    def register(url, kind, start):
        calls.append((url, kind, start))
        return f"relay:{len(calls) - 1}"

    return calls, register


# -- playlists ----------------------------------------------------------------------


def test_master_relays_every_uri_and_replaces_source_subtitles():
    calls, register = recorder()
    text = hls.rewrite_master(MASTER, BASE, register, [
        hls.Rendition("English", "en", "http://pc/s/0.m3u8", default=True),
        hls.Rendition('French "FR"', "fr", "http://pc/s/1.m3u8"),
    ])
    assert calls == [
        ("https://cdn.test/v/audio/eng.m3u8", "playlist", None),
        ("https://cdn.test/v/video/1080.m3u8", "playlist", None),
    ]
    lines = text.splitlines()
    assert lines[0] == "#EXTM3U"
    assert 'NAME="English",DEFAULT=YES,AUTOSELECT=YES' in lines[1] and 'URI="http://pc/s/0.m3u8"' in lines[1]
    assert "NAME=\"French 'FR'\",DEFAULT=NO" in lines[2]
    variant = next(line for line in lines if line.startswith("#EXT-X-STREAM-INF"))
    assert variant.count("SUBTITLES=") == 1 and variant.endswith(f'SUBTITLES="{hls.SUBTITLE_GROUP}"')
    assert "cdn.test" not in text and "subs/eng" not in text


def test_master_without_cue_subtitles_relays_the_sources():
    calls, register = recorder()
    text = hls.rewrite_master(MASTER, BASE, register)
    assert ("https://cdn.test/v/subs/eng.m3u8", "subtitles", None) in calls
    assert 'SUBTITLES="subs"' in text


def test_media_playlist_records_segment_starts_and_total():
    calls, register = recorder()
    text, total = hls.rewrite_media(SHORT_MEDIA, "https://cdn.test/v/video/1080.m3u8", register)
    assert total == 16.5
    assert calls == [
        ("https://cdn.test/v/video/init.mp4", "segment", None),
        ("https://media.test/seg0.ts", "segment", 0.0),
        ("https://cdn.test/v/video/seg1.ts", "segment", 6.0),
        ("https://cdn.test/v/video/seg2.ts", "segment", 12.0),
    ]
    assert text.splitlines()[-1] == "#EXT-X-ENDLIST" and "media.test" not in text
    calls.clear()
    hls.rewrite_media(SHORT_MEDIA, "https://cdn.test/v/subs/eng.m3u8", register, track_progress=False)
    assert all(start is None for _, _, start in calls)


def test_wrapped_media_and_subtitle_playlists():
    wrapped = hls.wrap_media("http://pc/r/a", [hls.Rendition("English", "en", "http://pc/s/0.m3u8", True)])
    assert wrapped.splitlines()[-2:] == [f'#EXT-X-STREAM-INF:BANDWIDTH=2000000,SUBTITLES="{hls.SUBTITLE_GROUP}"', "http://pc/r/a"]
    playlist = hls.subtitle_playlist("http://pc/s/0.vtt", 12.25)
    assert "#EXTINF:12.250,\nhttp://pc/s/0.vtt\n#EXT-X-ENDLIST" in playlist
    assert hls.is_playlist(playlist) and hls.is_playlist(b"\xef\xbb\xbf#EXTM3U\n") and not hls.is_playlist(b"\x47\x40")


def test_subtitle_conversion_and_duration():
    assert hls.to_webvtt(VTT, ".vtt") == VTT
    converted = hls.to_webvtt(SRT, ".srt")
    assert converted.startswith("WEBVTT\n\n") and "00:00:01.000 --> 00:00:02.500" in converted
    assert hls.to_webvtt("[Script Info]", ".ass") is None
    assert hls.to_webvtt("00:01.000 --> 00:02.000\nx", ".vtt") is None
    assert hls.webvtt_duration("WEBVTT\n\n01:02:03.500 --> 01:02:05.250\nx\n\n00:10.000 --> 00:12.000\ny\n") == 3725.25


# -- SSDP ---------------------------------------------------------------------------


def advertiser() -> Advertiser:
    return Advertiser(uuid="abc", location="http://192.168.1.5:8770/dlna/description.xml", address="192.168.1.5", server="Cue")


def search(target: str, man: str = '"ssdp:discover"') -> bytes:
    return f"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: {man}\r\nMX: 1\r\nST: {target}\r\n\r\n".encode()


def test_ssdp_answers_searches_for_this_media_server():
    everything = advertiser().responses(search("ssdp:all"))
    assert len(everything) == 5
    assert all(b"LOCATION: http://192.168.1.5:8770/dlna/description.xml" in reply for reply in everything)
    [server] = advertiser().responses(search("urn:schemas-upnp-org:device:MediaServer:1"))
    assert server.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"USN: uuid:abc::urn:schemas-upnp-org:device:MediaServer:1\r\n" in server


@pytest.mark.parametrize("request_bytes", [
    search("urn:schemas-upnp-org:device:MediaRenderer:1"), search("ssdp:all", man="none"),
    b"NOTIFY * HTTP/1.1\r\nNT: upnp:rootdevice\r\n\r\n", b"\xff\xfe garbage",
])
def test_ssdp_ignores_other_requests(request_bytes):
    assert advertiser().responses(request_bytes) == []


def test_ssdp_announcements():
    alive = advertiser().notify("alive")
    assert len(alive) == 5 and all(b"NTS: ssdp:alive" in m and b"LOCATION: " in m for m in alive)
    assert all(b"NTS: ssdp:byebye" in m and b"LOCATION" not in m for m in advertiser().notify("byebye"))


# -- the server, with an HTTP client as the TV ------------------------------------------


class FakeProvider:
    name = "fake"

    def __init__(self, streams) -> None:
        self.streams = streams

    async def stream(self, episode: Episode) -> list[Stream]:
        result = self.streams[episode.number]
        if isinstance(result, Exception):
            raise result
        return result


def source_stream(**changes) -> Stream:
    values = dict(
        url=BASE,
        headers={"Referer": "https://player.test/", "Authorization": "Bearer secret"},
        subtitle_tracks=[
            StreamSubtitle("https://subs.test/en.vtt", "en", "English"),
            StreamSubtitle("https://subs.test/fr.srt", "fr", "French"),
        ],
    )
    values.update(changes)
    return Stream(**values)


def upstream(requests: list[httpx.Request]) -> httpx.MockTransport:
    playlists = {
        BASE: MASTER, "https://cdn.test/v/video/1080.m3u8": LONG_MEDIA,
        "https://cdn.test/v/audio/eng.m3u8": SHORT_MEDIA, "https://cdn.test/v/plain.m3u8": SHORT_MEDIA,
    }

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        url = str(request.url)
        if url in playlists:
            return httpx.Response(200, text=playlists[url], headers={"content-type": PLAYLIST_TYPE})
        if "missing" in url:
            return httpx.Response(404)
        return httpx.Response(200, content=b"media:" + url.encode(), headers={"content-type": "video/mp2t"})

    return httpx.MockTransport(handle)


def subtitle_files(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=VTT if request.url.path.endswith(".vtt") else SRT)


@pytest.fixture
async def cast(tmp_path: Path, store):
    requests: list[httpx.Request] = []
    statuses: list[str] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(subtitle_files)) as subtitle_client:
        server = CastServer(
            loop=asyncio.get_running_loop(), store=store,
            provider_subtitles=ProviderSubtitles(subtitle_client, tmp_path / "subs"), subtitle_languages=["en"],
            name="Test Cue", host="127.0.0.1", port=0, bind="127.0.0.1", announce=False,
            transport=upstream(requests), on_status=statuses.append,
        )
        server.start()
        try:
            async with httpx.AsyncClient(base_url=server.base_url, trust_env=False, timeout=10) as tv:
                yield SimpleNamespace(server=server, tv=tv, upstream=requests, statuses=statuses, store=store)
        finally:
            server.stop()


SHOW = Show("s1", "Show")
EPISODES = [Episode("s1", 1, title="Pilot"), Episode("s1", 2)]


async def eventually(condition, timeout: float = 2.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    assert condition()


async def test_tv_plays_through_the_relay_with_source_headers(cast):
    url = cast.server.publish("fake", FakeProvider({1: [source_stream()], 2: [source_stream()]}), SHOW, EPISODES)
    assert url == f"{cast.server.base_url}/play/1/0"
    master = await cast.tv.get("/play/1/0")
    assert master.status_code == 200 and master.headers["content-type"] == PLAYLIST_TYPE
    assert "cdn.test" not in master.text and "secret" not in master.text
    english, french = [line for line in master.text.splitlines() if "TYPE=SUBTITLES" in line]
    assert 'NAME="English"' in english and "DEFAULT=YES" in english
    assert 'NAME="French"' in french and "DEFAULT=NO" in french

    variant = master.text.splitlines()[-1]
    assert variant.startswith(f"{cast.server.base_url}/r/")
    media = await cast.tv.get(variant)
    segments = [line for line in media.text.splitlines() if line.startswith("http")]
    assert len(segments) == 100 and all(s.startswith(f"{cast.server.base_url}/r/") for s in segments)

    data = await cast.tv.get(segments[1], headers={"Range": "bytes=0-99"})
    assert data.content == b"media:https://cdn.test/v/video/seg1.ts"
    assert data.headers["content-type"] == "video/mp2t"
    sent = {str(r.url): r for r in cast.upstream}
    same_origin = sent["https://cdn.test/v/video/seg1.ts"]
    assert same_origin.headers["Authorization"] == "Bearer secret" and same_origin.headers["Range"] == "bytes=0-99"
    # Another host (a CDN) gets the stream's Referer but never its credentials.
    await cast.tv.get(segments[0])
    other_host = sent | {str(r.url): r for r in cast.upstream}
    assert other_host["https://media.test/seg0.ts"].headers["Referer"] == "https://player.test/"
    assert "authorization" not in other_host["https://media.test/seg0.ts"].headers


async def test_subtitles_are_served_as_hls_renditions(cast):
    cast.server.publish("fake", FakeProvider({1: [source_stream()]}), SHOW, EPISODES[:1])
    await cast.tv.get("/play/1/0")
    playlist = await cast.tv.get("/s/1/0/1.m3u8")
    assert playlist.headers["content-type"] == PLAYLIST_TYPE
    vtt_url = next(line for line in playlist.text.splitlines() if line.startswith("http"))
    french = await cast.tv.get(vtt_url)
    assert french.headers["content-type"].startswith("text/vtt")
    assert french.text.startswith("WEBVTT") and "00:00:01.000 --> 00:00:02.500" in french.text
    assert (await cast.tv.get("/s/1/0/9.vtt")).status_code == 404


async def test_media_only_stream_is_wrapped_to_carry_subtitles(cast):
    plain = "https://cdn.test/v/plain.m3u8"
    cast.server.publish("fake", FakeProvider({1: [source_stream(url=plain)]}), SHOW, EPISODES[:1])
    master = await cast.tv.get("/play/1/0")
    assert "#EXT-X-STREAM-INF" in master.text and "TYPE=SUBTITLES" in master.text
    media = await cast.tv.get(master.text.splitlines()[-1])
    assert "#EXTINF" in media.text


async def test_segment_requests_save_resume_points_and_watched_markers(cast):
    cast.server.publish("fake", FakeProvider({1: [source_stream()]}), SHOW, EPISODES[:1])
    master = await cast.tv.get("/play/1/0")
    media = await cast.tv.get(master.text.splitlines()[-1])
    segments = [line for line in media.text.splitlines() if line.startswith("http")]

    await cast.tv.get(segments[1])  # opening the episode keeps any earlier resume point
    await asyncio.sleep(0.1)
    assert cast.store.get_history("fake", "s1") is None
    await cast.tv.get(segments[60])  # seeking on the TV to 10:00
    await eventually(lambda: cast.store.get_history("fake", "s1") is not None)
    entry = cast.store.get_history("fake", "s1")
    assert (entry.last_episode, entry.position) == (1, 580.0)
    await cast.tv.get(segments[95])  # past 90% of 1000 seconds
    await eventually(lambda: cast.store.watched("fake", "s1") == {1})
    assert cast.store.get_history("fake", "s1").position == 0.0
    assert "Cast: finished Show · episode 1" in cast.statuses


async def test_local_files_are_served_with_ranges(cast, tmp_path: Path):
    video = tmp_path / "episode.mp4"
    video.write_bytes(bytes(range(256)))
    cast.server.publish("fake", FakeProvider({1: [Stream(str(video))]}), SHOW, EPISODES[:1])
    whole = await cast.tv.get("/play/1/0")
    assert whole.content == bytes(range(256)) and whole.headers["accept-ranges"] == "bytes"
    part = await cast.tv.get("/play/1/0", headers={"Range": "bytes=10-13"})
    assert part.status_code == 206 and part.content == bytes([10, 11, 12, 13])
    assert part.headers["content-range"] == "bytes 10-13/256"
    assert (await cast.tv.get("/play/1/0", headers={"Range": "bytes=300-"})).status_code == 416


async def test_failures_reach_the_tv_and_cue(cast):
    cast.server.publish("fake", FakeProvider({1: ProviderError("Server is down"), 2: []}), SHOW, EPISODES)
    failed = await cast.tv.get("/play/1/0")
    assert failed.status_code == 502 and "Server is down" in failed.text
    await eventually(lambda: any("failed: Server is down" in status for status in cast.statuses))
    assert "No playable stream" in (await cast.tv.get("/play/1/1")).text
    assert (await cast.tv.get("/play/1/5")).status_code == 404
    assert (await cast.tv.get("/r/" + "0" * 32)).status_code == 404
    cast.server.publish("fake", FakeProvider({1: [source_stream(url="https://cdn.test/missing.m3u8")]}), SHOW, EPISODES[:1])
    assert (await cast.tv.get("/play/2/0")).status_code == 404


async def test_only_recent_casts_stay_available(cast):
    provider = FakeProvider({1: [source_stream()]})
    for _ in range(6):
        cast.server.publish("fake", provider, SHOW, EPISODES[:1])
    assert (await cast.tv.get("/play/1/0")).status_code == 404
    now = await cast.tv.get("/now", follow_redirects=False)
    assert now.status_code == 302 and now.headers["location"].endswith("/play/6/0")


# -- DLNA directory -----------------------------------------------------------------------

NS = {
    "s": "http://schemas.xmlsoap.org/soap/envelope/",
    "d": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


async def browse(tv: httpx.AsyncClient, object_id: str, flag: str = "BrowseDirectChildren", start: int = 0, count: int = 0):
    body = (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        '<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
        f"<ObjectID>{object_id}</ObjectID><BrowseFlag>{flag}</BrowseFlag><Filter>*</Filter>"
        f"<StartingIndex>{start}</StartingIndex><RequestedCount>{count}</RequestedCount><SortCriteria></SortCriteria>"
        "</u:Browse></s:Body></s:Envelope>"
    )
    response = await tv.post("/dlna/cds/control", content=body, headers={
        "SOAPACTION": '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"', "Content-Type": 'text/xml; charset="utf-8"',
    })
    if response.status_code != 200:
        return response, None
    envelope = ET.fromstring(response.text)
    result = envelope.find(".//Result")
    return envelope, ET.fromstring(result.text)


async def test_dlna_device_description(cast):
    description = await cast.tv.get("/dlna/description.xml")
    assert "urn:schemas-upnp-org:device:MediaServer:1" in description.text
    assert "<friendlyName>Test Cue</friendlyName>" in description.text
    assert f"<UDN>uuid:{cast.server.uuid}</UDN>" in description.text
    for path in ("/dlna/cds.xml", "/dlna/cms.xml"):
        ET.fromstring((await cast.tv.get(path)).text)  # well-formed service descriptions


async def test_dlna_lists_casts_and_their_episodes(cast):
    _, empty = await browse(cast.tv, "0")
    assert "Nothing cast yet" in empty.find("d:container/dc:title", NS).text

    cast.store.mark_watched("fake", "s1", 1)
    cast.store.save_progress("fake", "s1", "Show", 2, 125.0)
    url = cast.server.publish("fake", FakeProvider({}), SHOW, EPISODES)
    assert url.endswith("/play/1/1")  # starts at the unfinished episode

    envelope, root = await browse(cast.tv, "0")
    [container] = root.findall("d:container", NS)
    assert container.get("id") == "c1" and container.find("dc:title", NS).text == "Show"
    _, episodes = await browse(cast.tv, "c1")
    items = episodes.findall("d:item", NS)
    assert [item.find("dc:title", NS).text for item in items] == ["✓ 1. Pilot", "Episode 2 (resume 2:05)"]
    resource = items[0].find("d:res", NS)
    assert resource.get("protocolInfo") == ITEM_PROTOCOL and resource.get("protocolInfo").startswith("http-get:*:video/")
    assert resource.text == f"{cast.server.base_url}/play/1/0"
    assert items[0].find("{urn:schemas-upnp-org:metadata-1-0/upnp/}class").text == "object.item.videoItem"

    envelope, page = await browse(cast.tv, "c1", start=1, count=1)
    assert len(page.findall("d:item", NS)) == 1 and envelope.find(".//TotalMatches").text == "2"
    _, item = await browse(cast.tv, "c1.1", flag="BrowseMetadata")
    assert item.find("d:item", NS).get("id") == "c1.1"
    fault, _ = await browse(cast.tv, "c9")
    assert fault.status_code == 500 and "<errorCode>701</errorCode>" in fault.text


async def test_port_in_use_is_reported(cast, store):
    other = CastServer(loop=asyncio.get_running_loop(), store=store, host="127.0.0.1",
                       port=cast.server.port, bind="127.0.0.1", announce=False)
    with pytest.raises(OSError):
        other.start()
    other.stop()


# -- configuration and the TUI ----------------------------------------------------------------


def test_cast_config():
    assert parse_config({}).cast == CastConfig()
    config = parse_config({"cast": {"enabled": True, "port": 9000, "name": "Living room", "host": " 192.168.1.5 "}})
    assert config.cast == CastConfig(enabled=True, port=9000, name="Living room", host="192.168.1.5")
    for port in (0, 70000, True, "8770"):
        with pytest.raises(ConfigError):
            parse_config({"cast": {"port": port}})
    assert parse_config({"cast": {"interface": " Ethernet "}}).cast.interface == "Ethernet"
    with pytest.raises(ConfigError, match="not both"):
        parse_config({"cast": {"host": "192.168.1.5", "interface": "Ethernet"}})


def test_adapter_is_found_by_name(monkeypatch):
    monkeypatch.setattr(network, "adapters", lambda: {"Ethernet": "192.168.1.5", "Wi-Fi": "10.0.0.2"})
    assert network.adapter_address(" ethernet ") == "192.168.1.5"
    with pytest.raises(network.AdapterError, match=r"Connected adapters: Ethernet \(192\.168\.1\.5\), Wi-Fi \(10\.0\.0\.2\)"):
        network.adapter_address("Bluetooth")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows adapter list")
def test_windows_adapters_include_the_default_route():
    found = network.adapters()
    assert all(address.count(".") == 3 and not address.startswith("127.") for address in found.values())
    if network.lan_address() != "127.0.0.1":
        assert network.lan_address() in found.values()


class RecordingServer:
    started: list[dict] = []

    def __init__(self, **options) -> None:
        self.options, self.name, self.base_url = options, options["name"], "http://pc:8770"

    def start(self) -> None:
        RecordingServer.started.append(self.options)

    def stop(self) -> None:
        pass


@pytest.mark.parametrize(("cast_config", "listens_on"), [
    (CastConfig(enabled=True), ""),
    (CastConfig(enabled=True, host="192.168.1.9"), "192.168.1.9"),
    (CastConfig(enabled=True, interface="Ethernet"), "10.0.0.5"),
])
async def test_cast_server_listens_on_the_chosen_address(monkeypatch, store, tmp_path, cast_config, listens_on):
    monkeypatch.setattr("cue.app.CastServer", RecordingServer)
    monkeypatch.setattr("cue.app.adapter_address", lambda name: {"Ethernet": "10.0.0.5"}[name])
    RecordingServer.started = []
    app = CueApp(Config(cast=cast_config), store=store, dropin_dir=tmp_path / "no-plugins",
                 http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))))
    async with app.run_test():
        pass
    [options] = RecordingServer.started
    assert options["host"] == options["bind"] == listens_on


async def test_missing_adapter_turns_casting_off(monkeypatch, store, tmp_path):
    def missing(name):
        raise network.AdapterError(f"No connected network adapter is named {name!r}")

    monkeypatch.setattr("cue.app.adapter_address", missing)
    app = CueApp(Config(cast=CastConfig(enabled=True, interface="Wi-Fi")), store=store, dropin_dir=tmp_path / "no-plugins",
                 http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.cast_server is None
        assert any("adapter is named 'Wi-Fi'" in str(n.message) for n in app._notifications)


async def test_t_casts_the_episodes(media_dir: Path, store, tmp_path: Path, monkeypatch):
    monkeypatch.setattr("cue.app.devices.supported", lambda: [])  # no AirPlay or Chromecast: straight to VLC
    offline = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    app = CueApp(Config(providers={"local": {"path": str(media_dir)}}), store=store, http=offline,
                 dropin_dir=tmp_path / "no-plugins")
    published = []
    async with app.run_test(size=(100, 30)) as pilot:
        [show] = await app.providers["local"].search("frieren")
        app.open_show("local", show)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, EpisodesScreen)

        app.screen.query_one("#range", Input).focus()
        await pilot.press("t")  # casting is off in this config
        await pilot.pause()
        assert any("Casting is off" in str(n.message) for n in app._notifications)
        assert app.screen.query_one("#range", Input).value == ""  # not typed into the box

        app.cast_server = SimpleNamespace(
            name="Cue", base_url="http://pc:8770",
            publish=lambda *args: published.append(args) or "http://pc:8770/play/1/0", stop=lambda: None,
        )
        await pilot.press("t")
        await pilot.press("2", "-", "3", "t")  # typed range, cast without leaving the box
        await pilot.press("escape", "t")  # from the grid as well
        await pilot.pause()
    assert [[e.number for e in queue] for _, _, _, queue in published] == [[1, 2, 3], [2, 3], [2, 3]]


class WatchingSession:
    """A session playing episode 2 of 3 on the PC until it is stopped."""

    def __init__(self) -> None:
        self.provider_name, self.provider = "local", SimpleNamespace(name="local", language="dub")
        self.show = Show("s", "Show")
        self.queue = [Episode("s", 1), Episode("s", 2), Episode("s", 3)]
        self.index, self.auto_next, self.listener = 1, True, None
        self.state = PlaybackState(self.show, episode=self.queue[1])
        self.stopped = asyncio.Event()

    async def run(self) -> None:
        await self.stopped.wait()
        self.state.finished = True

    def stop(self) -> None:
        self.stopped.set()

    def kill(self) -> None:
        pass


async def test_send_to_tv_moves_playback_from_the_pc(store, tmp_path: Path, monkeypatch):
    monkeypatch.setattr("cue.app.devices.supported", lambda: [])
    app = CueApp(Config(), store=store, dropin_dir=tmp_path / "no-plugins",
                 http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))))
    published = []
    async with app.run_test(size=(100, 30)) as pilot:
        session = WatchingSession()
        app.push_screen(NowPlayingScreen(session))
        await pilot.pause()
        await pilot.press("t")  # casting is off: keep watching on the PC
        await pilot.pause()
        assert not session.stopped.is_set() and isinstance(app.screen, NowPlayingScreen)

        app.cast_server = SimpleNamespace(
            name="Cue", base_url="http://pc:8770",
            publish=lambda *args: published.append(args) or "http://pc:8770/play/1/0", stop=lambda: None,
        )
        await pilot.press("t")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert session.stopped.is_set() and not isinstance(app.screen, NowPlayingScreen)
    [(provider_name, provider, show, queue)] = published
    # The current and later episodes, with the session's own SUB/DUB binding.
    assert (provider_name, provider, show) == ("local", session.provider, session.show)
    assert [episode.number for episode in queue] == [2, 3]


# -- AirPlay and Chromecast ---------------------------------------------------------------


async def test_tvs_told_what_to_play_get_resolved_media(cast):
    generation, first = cast.server.publish_queue(
        "fake", FakeProvider({1: [source_stream()], 2: ProviderError("Server is down")}), SHOW, EPISODES)
    assert (generation, first) == (1, 0) and cast.server.queue_length(generation) == 2
    media = await cast.server.media(generation, 0)
    assert media.url == f"{cast.server.base_url}/play/1/0" and media.content_type == PLAYLIST_TYPE
    assert media.title.startswith("Show") and media.start == 0.0
    with pytest.raises(ProviderError, match="Server is down"):
        await cast.server.media(generation, 1)
    with pytest.raises(ProviderError, match="replaced"):
        await cast.server.media(99, 0)

    # Played to the end (the TV asked for the last pieces): the next one may start.
    assert not cast.server.finished(generation, 0)
    master = await cast.tv.get("/play/1/0")
    media_playlist = await cast.tv.get(master.text.splitlines()[-1])
    segments = [line for line in media_playlist.text.splitlines() if line.startswith("http")]
    await cast.tv.get(segments[-1])
    await eventually(lambda: cast.server.finished(generation, 0))


async def test_chromecast_may_read_media_but_not_the_server_pages(cast):
    cast.server.publish("fake", FakeProvider({1: [source_stream()]}), SHOW, EPISODES[:1])
    master = await cast.tv.get("/play/1/0")
    assert master.headers["access-control-allow-origin"] == "*"
    piece = master.text.splitlines()[-1]
    preflight = await cast.tv.request("OPTIONS", piece, headers={"Access-Control-Request-Headers": "range"})
    assert preflight.status_code == 204 and preflight.headers["access-control-allow-headers"] == "range"
    assert "GET" in preflight.headers["access-control-allow-methods"]
    index = await cast.tv.get("/")
    assert "access-control-allow-origin" not in index.headers


def test_airplay_speakers_are_left_out() -> None:
    from cue.cast.devices import _plays_video

    assert _plays_video({"features": "0x5A7FFFF7,0x1E"})  # an Apple TV
    assert not _plays_video({"features": "0x4A7FCA00,0xBC354BD0"})  # a HomePod
    assert _plays_video({}) and _plays_video({"features": "nonsense"})  # unknown: offer it


async def test_discovery_keeps_what_answered(monkeypatch) -> None:
    from cue.cast import devices

    monkeypatch.setattr(devices, "supported", lambda: [devices.AIRPLAY, devices.CHROMECAST])

    async def airplay(credentials, timeout):
        return [devices.Device(devices.AIRPLAY, "Living room", "a", "10.0.0.2")]

    async def chromecast(timeout):
        raise OSError("no multicast here")

    monkeypatch.setattr(devices, "_airplay_devices", airplay)
    monkeypatch.setattr(devices, "_chromecast_devices", chromecast)
    assert [d.name for d in await devices.discover(Path("unused.json"), 0.1)] == ["Living room"]


async def test_chromecast_playback_ends_when_its_video_does() -> None:
    import threading

    from cue.cast import devices

    class Controller:
        def __init__(self):
            self.listeners = []

        def register_status_listener(self, listener):
            self.listeners.append(listener)

        def play_media(self, url, content_type, **options):
            self.loaded = (url, content_type, options)
            report = self.listeners[0]

            def statuses():  # what came before is ignored; then this video plays and ends
                report.new_media_status(SimpleNamespace(content_id="http://old", player_state="IDLE", idle_reason="FINISHED"))
                report.new_media_status(SimpleNamespace(content_id=url, player_state="BUFFERING", idle_reason=None))
                report.new_media_status(SimpleNamespace(content_id=url, player_state="PLAYING", idle_reason=None))
                report.new_media_status(SimpleNamespace(content_id=url, player_state="IDLE", idle_reason=self.reason))

            threading.Thread(target=statuses).start()

    class Cast:
        def __init__(self):
            self.media_controller = Controller()

        def register_status_listener(self, listener):
            pass

        def disconnect(self, timeout):
            pass

    cast = Cast()
    receiver = devices._Chromecast(devices.Device(devices.CHROMECAST, "Kitchen", "c", "10.0.0.3"), cast)
    media = devices.Media("http://pc/play/1/0", "Show · episode 1", PLAYLIST_TYPE, 95.0)
    cast.media_controller.reason = "FINISHED"
    await asyncio.wait_for(receiver.play(media), 5)
    assert cast.media_controller.loaded == ("http://pc/play/1/0", PLAYLIST_TYPE, {
        "title": "Show · episode 1", "current_time": 95.0, "stream_type": "BUFFERED"})
    cast.media_controller.reason = "ERROR"
    with pytest.raises(devices.DeviceError, match="stopped with an error"):
        await asyncio.wait_for(receiver.play(media), 5)
    await receiver.close()


async def test_casting_to_a_tv_pairs_once_and_moves_on(store, tmp_path: Path, monkeypatch):
    from cue.cast import devices
    from cue.screens.cast import CastPicker, PairingDialog

    tv = devices.Device(devices.AIRPLAY, "Living room", "tv-1", "10.0.0.9", "Samsung", needs_pairing=True)
    monkeypatch.setattr("cue.app.devices.supported", lambda: [devices.AIRPLAY])

    async def discover(credentials, timeout=devices.SCAN_SECONDS):
        return [tv]

    played, codes, closed = [], [], []

    class Receiver(devices.Receiver):
        async def play(self, media):
            played.append(media.url)

        async def close(self):
            closed.append(True)

    async def connect(device, credentials):
        assert codes, "paired before connecting"
        return Receiver()

    async def pair(device, credentials, ask_code):
        codes.append(await ask_code())

    async def media(generation, index):
        return devices.Media(f"http://pc/play/{generation}/{index}", f"Episode {index + 1}", PLAYLIST_TYPE)

    monkeypatch.setattr("cue.screens.cast.devices.discover", discover)
    monkeypatch.setattr("cue.app.devices.connect", connect)
    monkeypatch.setattr("cue.app.devices.pair", pair)
    app = CueApp(Config(), store=store, dropin_dir=tmp_path / "no-plugins",
                 http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))))
    async with app.run_test(size=(100, 30)) as pilot:
        app.cast_server = SimpleNamespace(
            name="Cue", base_url="http://pc:8770", stop=lambda: None, publish_queue=lambda *args: (7, 1),
            queue_length=lambda generation: 3, media=media,
            finished=lambda generation, index: index == 1,  # episode 2 played to the end, episode 3 was stopped
        )
        app.cast("fake", SHOW, [*EPISODES, Episode("s1", 3)], provider=FakeProvider({}))
        await pilot.pause()
        assert isinstance(app.screen, CastPicker)
        await eventually(lambda: "Living room" in str(app.screen.query_one("#targets").get_option_at_index(1).prompt))
        await pilot.press("down", "enter")
        await eventually(lambda: isinstance(app.screen, PairingDialog))
        await pilot.press("1", "2", "3", "4", "enter")
        await eventually(lambda: bool(closed))
    assert codes == ["1234"]
    assert played == ["http://pc/play/7/1", "http://pc/play/7/2"]
    assert app._last_tv == "tv-1"
