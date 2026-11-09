"""DLNA TVs: finding and describing media renderers, and playing on one, with the TV faked over HTTP."""

from __future__ import annotations

import asyncio
import re
from xml.sax.saxutils import unescape

import httpx
import pytest

from cue.cast import devices, renderers
from cue.cast.devices import Device, DeviceError, Media
from cue.cast.renderers import Renderer, RendererError

FIND_RENDERERS = devices._renderer_devices  # tests switch it off (conftest); one test puts it back
LOCATION = "http://192.168.1.5:9197/dmr"
DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>[TV] Samsung 7 Series (50)</friendlyName>
    <modelName>UN50TU700DFXZA</modelName>
    <UDN>uuid:addfbb6b-59d0</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
        <controlURL>/upnp/control/ConnectionManager1</controlURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <controlURL>{transport}</controlURL>
      </service>
    </serviceList>
  </device>
</root>"""
TV = Renderer("Samsung 7 Series (50)", "uuid:addfbb6b-59d0", "192.168.1.5", "UN50TU700DFXZA",
              "http://192.168.1.5:9197/upnp/control/AVTransport1", "http://192.168.1.5:9197/upnp/control/ConnectionManager1")


def soap_answer(action: str, fields: dict[str, str] | None = None) -> httpx.Response:
    body = "".join(f"<{name}>{value}</{name}>" for name, value in (fields or {}).items())
    return httpx.Response(200, text=(
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        f'<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">{body}</u:{action}Response>'
        "</s:Body></s:Envelope>"))


def soap_fault(code: int, description: str) -> httpx.Response:
    return httpx.Response(500, text=(
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
        f"<detail><UPnPError><errorCode>{code}</errorCode><errorDescription>{description}</errorDescription>"
        "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"))


# -- finding and describing --------------------------------------------------------------


def test_only_plain_addresses_on_the_device_that_answered_are_followed() -> None:
    answer = b"HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\nLocation: %s\r\n\r\n"
    assert renderers._location(answer % LOCATION.encode(), "192.168.1.5") == LOCATION
    assert renderers._location(answer % LOCATION.encode(), "192.168.1.9") == ""  # pointing somewhere else
    assert renderers._location(answer % b"https://192.168.1.5/dmr", "192.168.1.5") == ""
    assert renderers._location(b"HTTP/1.1 200 OK\r\n\r\n", "192.168.1.5") == ""


async def test_a_samsung_tv_is_described() -> None:
    pages = {LOCATION: DESCRIPTION.format(transport="/upnp/control/AVTransport1")}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=pages[str(request.url)]))) as http:
        assert await renderers.describe(http, LOCATION) == TV  # "[TV] " is Samsung's, not the TV's name


@pytest.mark.parametrize("page", [
    DESCRIPTION.format(transport="http://192.168.1.77/control"),  # control on another host
    DESCRIPTION.replace("MediaRenderer", "MediaServer").format(transport="/c"),  # not a renderer
    '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><root>&a;</root>',  # a DTD
    "not xml",
])
async def test_descriptions_cue_wont_use(page: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=page))) as http:
        assert await renderers.describe(http, LOCATION) is None


@pytest.mark.parametrize(("sink", "video"), [
    ("http-get:*:video/mp4:*,http-get:*:audio/mpeg:*", True),
    ("http-get:*:application/vnd.apple.mpegurl:*", True),
    ("http-get:*:audio/mpeg:*,http-get:*:audio/flac:*", False),  # a speaker
    ("", True),  # says nothing: offer it
])
async def test_speakers_are_left_out(sink: str, video: bool) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: soap_answer("GetProtocolInfo", {"Source": "", "Sink": sink}))) as http:
        assert await renderers.plays_video(http, TV) is video


async def test_a_refused_command_says_why() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: soap_fault(701, "Transition not available"))) as http:
        with pytest.raises(RendererError, match=r"Seek: Transition not available \(701\)"):
            await renderers.call(http, TV.av_transport, renderers.AV_TRANSPORT, "Seek", (("InstanceID", "0"),))


def test_metadata_and_times() -> None:
    metadata = renderers.didl("http://pc/play/1/0?start=605&x=<1>", 'Show "1"', "application/vnd.apple.mpegurl")
    assert "http://pc/play/1/0?start=605&amp;x=&lt;1&gt;" in metadata and "object.item.videoItem" in metadata
    assert renderers.clock(3725.9) == "1:02:05" and renderers.clock(-3) == "0:00:00"


# -- playing ------------------------------------------------------------------------------


class FakeTV:
    """A DLNA TV's AVTransport: it plays for a few polls after Play, then stops."""

    def __init__(self, *, plays_for: int = 3, error: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.states = ["TRANSITIONING"]
        self.plays_for = plays_for
        self.error = error

    def handle(self, request: httpx.Request) -> httpx.Response:
        action = request.headers["SOAPACTION"].strip('"').rsplit("#", 1)[1]
        fields = {name: unescape(value) for name, value in re.findall(r"<(\w+)>(.*?)</\1>", request.content.decode())}
        self.calls.append((action, fields))
        if action == "Play":
            self.states = ["TRANSITIONING", *["PLAYING"] * self.plays_for, "STOPPED"]
        if action == "GetTransportInfo":
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            status = "ERROR_OCCURRED" if self.error and state == "PLAYING" else "OK"
            return soap_answer(action, {"CurrentTransportState": state, "CurrentTransportStatus": status})
        return soap_answer(action)

    def actions(self) -> list[str]:
        return [action for action, _ in self.calls if action != "GetTransportInfo"]


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(devices, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(devices, "STEP_SECONDS", 0.01)


def receiver(tv: FakeTV) -> devices._Renderer:
    device = Device(devices.RENDERER, TV.name, TV.udn, TV.host, TV.model, handle=TV)
    return devices._Renderer(device, httpx.AsyncClient(transport=httpx.MockTransport(tv.handle)))


async def test_an_episode_plays_until_the_tv_stops(quick: None) -> None:
    tv = FakeTV()
    await receiver(tv).play(Media("http://pc:8770/play/1/0", "Episode 1", "application/vnd.apple.mpegurl"))
    assert tv.actions() == ["Stop", "SetAVTransportURI", "Play"]
    sent = dict(tv.calls)["SetAVTransportURI"]
    assert sent["CurrentURI"] == "http://pc:8770/play/1/0" and "Episode 1" in sent["CurrentURIMetaData"]


async def test_resuming_hls_starts_the_stream_there_instead_of_seeking(quick: None) -> None:
    tv = FakeTV()
    await receiver(tv).play(Media("http://pc:8770/play/1/0", "Episode 1", "application/vnd.apple.mpegurl", 605.4))
    assert dict(tv.calls)["SetAVTransportURI"]["CurrentURI"] == "http://pc:8770/play/1/0?start=605"
    assert "Seek" not in tv.actions()


async def test_subtitles_are_offered_the_ways_tvs_look_for_them(quick: None) -> None:
    tv = FakeTV()
    await receiver(tv).play(Media("http://pc/play/1/0", "Episode 1", "application/vnd.apple.mpegurl", 605,
                                  "http://pc/s/1/0/0.srt"))
    metadata = dict(tv.calls)["SetAVTransportURI"]["CurrentURIMetaData"]
    srt = "http://pc/s/1/0/0.srt?start=605"  # shifted like the stream, which starts at 605 s
    assert f'<sec:CaptionInfoEx sec:type="srt">{srt}</sec:CaptionInfoEx>' in metadata  # Samsung
    assert f'pv:subtitleFileUri="{srt}"' in metadata  # LG and others
    assert "CaptionInfo" not in renderers.didl("http://pc/play/1/0", "E", "video/mp4")  # none without one


async def test_resuming_a_video_file_seeks(quick: None) -> None:
    tv = FakeTV()
    await receiver(tv).play(Media("http://pc:8770/play/1/0", "Movie", "video/mp4", 605))
    assert dict(tv.calls)["SetAVTransportURI"]["CurrentURI"] == "http://pc:8770/play/1/0"
    assert dict(tv.calls)["Seek"] == {"InstanceID": "0", "Unit": "REL_TIME", "Target": "0:10:05"}


async def test_stopping_casting_stops_the_tv(quick: None) -> None:
    tv = FakeTV(plays_for=10_000)
    play = asyncio.ensure_future(receiver(tv).play(Media("http://pc/play/1/0", "E", "video/mp4")))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if "PLAYING" not in tv.states[:1] and len(tv.calls) > 4:
            break
    play.cancel()
    with pytest.raises(asyncio.CancelledError):
        await play
    assert tv.actions()[-1] == "Stop"


async def test_a_tv_that_cant_play_it_says_so(quick: None) -> None:
    with pytest.raises(DeviceError, match="couldn't play this"):
        await receiver(FakeTV(error=True)).play(Media("http://pc/play/1/0", "E", "video/mp4"))


async def test_discovery_finds_dlna_tvs_and_leaves_out_speakers(monkeypatch: pytest.MonkeyPatch) -> None:
    speaker = Renderer("Kitchen", "uuid:speaker", "192.168.1.9", "", "http://192.168.1.9/avt", "http://192.168.1.9/cm")
    monkeypatch.setattr(devices, "_renderer_devices", FIND_RENDERERS)  # the network itself stays faked below
    monkeypatch.setattr(devices, "_search_addresses", lambda: ["192.168.1.2"])
    monkeypatch.setattr(renderers, "search", lambda addresses, timeout: [LOCATION, "http://192.168.1.9/d"])

    async def describe(http, location):
        return TV if location == LOCATION else speaker

    async def plays_video(http, renderer):
        return renderer is TV

    monkeypatch.setattr(renderers, "describe", describe)
    monkeypatch.setattr(renderers, "plays_video", plays_video)
    [found] = await devices._renderer_devices(timeout=1)
    assert (found.kind_label, found.name, found.address, found.model) == ("DLNA", TV.name, TV.host, TV.model)
    assert devices.RENDERER in devices.supported()
