"""cue serve: the JSON API, the web app's files, casting jobs and the runner, on a real core and cast server."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from cue.cast import CastServer, devices
from cue.cast.server import PLAYLIST_TYPE
from cue.config import CastConfig, Config, ConfigError, WebConfig, parse_config
from cue.core import Core
from cue.providers.base import Episode, ProviderError, Show, Stream
from cue.skip import SkipInfo, SkipInterval
from cue.web.api import STATE_KEY, Events, create_app, is_local_address, is_local_host

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
video/720.m3u8
"""
MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:10
#EXTINF:10.0,
seg0.ts
#EXTINF:10.0,
seg1.ts
#EXT-X-ENDLIST
"""
SRT = "1\n00:00:01,000 --> 00:00:02,500\nHello\n"


def upstream() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("master.m3u8"):
            return httpx.Response(200, text=MASTER, headers={"content-type": PLAYLIST_TYPE})
        if url.endswith("720.m3u8"):
            return httpx.Response(200, text=MEDIA, headers={"content-type": PLAYLIST_TYPE})
        return httpx.Response(200, content=b"media", headers={"content-type": "video/mp2t"})

    return httpx.MockTransport(handle)


class FakeProvider:
    name = "fake"
    title = "Fake Anime"
    groups = ["anime"]

    def __init__(self) -> None:
        self.shows = [Show("s1", "Sousou no Frieren", year=2023, episode_count=3), Show("s2", "Frieren Specials")]

    async def search(self, query: str) -> list[Show]:
        return [show for show in self.shows if query.lower() in show.title.lower()]

    async def episodes(self, show: Show) -> list[Episode]:
        if show.id == "broken":
            raise ProviderError("The source is down")
        return [Episode(show.id, number, title=f"Part {number}") for number in (1, 2, 3)]

    async def stream(self, episode: Episode) -> list[Stream]:
        return [Stream(f"https://cdn.test/{episode.show_id}/{episode.label}/master.m3u8", headers={"Referer": "https://site.test/"})]


class SlowProvider(FakeProvider):
    name = "slow"

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__()
        self.gate = gate

    async def search(self, query: str) -> list[Show]:
        await self.gate.wait()
        return [Show("x9", "Frieren (slow)")]


class BrokenProvider(FakeProvider):
    name = "broken"

    async def search(self, query: str) -> list[Show]:
        raise RuntimeError("plugin bug")


class MovieProvider(FakeProvider):
    name = "movies"
    groups = ["movies"]
    skip_supported = False


@pytest.fixture
async def site(store, tmp_path: Path):
    offline = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    core = Core(Config(), store=store, http=offline, dropin_dir=tmp_path / "no-plugins", plugin_root=tmp_path / "plugins")
    core.providers = {"fake": FakeProvider(), "movies": MovieProvider()}
    core.build_groups()
    events = Events()
    core.on_notice, core.on_status = events.notice, events.status
    server = CastServer(loop=asyncio.get_running_loop(), store=store, host="127.0.0.1", port=0, bind="127.0.0.1",
                        announce=False, transport=upstream())
    server.start()
    core.cast_server = server
    client = TestClient(TestServer(create_app(core, events)))
    await client.start_server()
    try:
        async with httpx.AsyncClient(base_url=server.base_url, trust_env=False, timeout=10) as browser:
            yield SimpleNamespace(core=core, client=client, server=server, events=events, store=store, browser=browser,
                                  state=client.app[STATE_KEY])
    finally:
        await client.close()
        await core.aclose()


async def eventually(check: Callable[[], bool], seconds: float = 5) -> None:
    for _ in range(int(seconds * 50)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("never happened")


async def post(site, path: str, body: dict) -> tuple[int, dict]:
    response = await site.client.post(path, json=body)
    return response.status, await response.json()


# -- information and searching ------------------------------------------------------------------------


async def test_info_lists_groups_providers_and_the_relay(site):
    response = await site.client.get("/api/info")
    assert response.status == 200
    info = await response.json()
    assert [group["name"] for group in info["groups"]] == ["movies", "anime", "all"]
    assert info["default_group"] == "movies"
    assert {"name": "fake", "title": "Fake Anime"} in info["providers"]
    assert info["cast"] == {"available": True, "name": "Cue", "port": site.server.port,
                            "url": site.server.base_url, "problem": ""}


async def test_search_streams_each_provider_as_it_answers(site):
    gate = asyncio.Event()
    site.core.providers["slow"] = SlowProvider(gate)
    site.core.build_groups()
    response = await site.client.get("/api/search", params={"q": "frieren", "group": "anime"})
    assert response.status == 200 and response.content_type == "application/x-ndjson"
    start = json.loads(await response.content.readline())
    assert start == {"type": "start", "group": {"name": "anime", "title": "Anime", "providers": ["fake", "slow"]},
                     "providers": ["fake", "slow"]}
    first = json.loads(await response.content.readline())  # while the slow one is still searching
    assert first["provider"] == "fake" and first["error"] is None
    assert [show["id"] for show in first["shows"]] == ["s1", "s2"]
    assert first["shows"][0]["title"] == "Sousou no Frieren" and first["shows"][0]["score"] > 0
    gate.set()
    second = json.loads(await response.content.readline())
    assert second["provider"] == "slow" and second["shows"][0]["title"] == "Frieren (slow)"
    assert json.loads(await response.content.readline()) == {"type": "done"}


async def test_search_reports_each_providers_problem(site):
    site.core.providers["broken"] = BrokenProvider()
    site.core.build_groups()
    response = await site.client.get("/api/search", params={"q": "frieren", "group": "anime"})
    lines = [json.loads(line) for line in (await response.text()).splitlines()]
    results = {line["provider"]: line for line in lines if line["type"] == "results"}
    assert results["broken"]["error"] == "RuntimeError: plugin bug" and results["broken"]["shows"] == []
    assert len(results["fake"]["shows"]) == 2
    assert (await site.client.get("/api/search", params={"q": " "})).status == 400
    assert (await site.client.get("/api/search", params={"q": "x", "group": "nope"})).status == 404


# -- episodes and Continue Watching ---------------------------------------------------------------------


async def test_episodes_carry_watched_and_resume_marks(site):
    site.store.mark_watched("fake", "s1", 1)
    site.store.save_progress("fake", "s1", "Sousou no Frieren", 2, 125.0)
    response = await site.client.get("/api/episodes", params={"provider": "fake", "show": "s1"})
    data = await response.json()
    assert data["show"]["title"] == "Sousou no Frieren"  # from the history, never searched here
    assert [(e["label"], e["watched"], e["position"]) for e in data["episodes"]] == [
        ("1", True, 0.0), ("2", False, 125.0), ("3", False, 0.0)]
    assert data["resume"] == {"number": 2.0, "label": "2", "position": 125.0, "caught_up": False}

    site.store.save_progress("fake", "s1", "Sousou no Frieren", 3, 0.0)  # watched the latest
    data = await (await site.client.get("/api/episodes", params={"provider": "fake", "show": "s1"})).json()
    assert data["resume"]["caught_up"] is True


async def test_episodes_problems(site):
    response = await site.client.get("/api/episodes", params={"provider": "fake", "show": "broken", "title": "B"})
    assert response.status == 502 and (await response.json())["error"] == "The source is down"
    response = await site.client.get("/api/episodes", params={"provider": "gone", "show": "s1"})
    assert response.status == 404 and "not set up" in (await response.json())["error"]


async def test_continue_watching(site):
    site.store.save_progress("fake", "s1", "Sousou no Frieren", 2, 61.0)
    site.store.save_progress("removed", "z", "Old Show", 4, 0.0)
    entries = (await (await site.client.get("/api/continue")).json())["entries"]
    by_title = {entry["title"]: entry for entry in entries}
    assert by_title["Sousou no Frieren"] | {"updated_at": 0} == {
        "provider": "fake", "show": "s1", "title": "Sousou no Frieren", "episode": 2.0, "label": "2",
        "position": 61.0, "updated_at": 0, "available": True}
    assert by_title["Old Show"]["available"] is False


# -- watching in the browser -----------------------------------------------------------------------------


async def test_play_publishes_the_show_on_the_relay(site):
    site.store.save_progress("fake", "s1", "Sousou no Frieren", 2, 300.0)
    status, data = await post(site, "/api/play", {"provider": "fake", "show": "s1", "number": 2})
    assert status == 200
    generation = data["cast"]
    assert data["index"] == 1 and data["relay"] == {"port": site.server.port, "url": site.server.base_url}
    assert [(item["label"], item["path"], item["resume"]) for item in data["items"]] == [
        ("1", f"/play/{generation}/0", 0.0), ("2", f"/play/{generation}/1", 300.0), ("3", f"/play/{generation}/2", 0.0)]
    assert data["skip"] is True

    # The browser plays the relay's link, as a TV would: HLS with the source's headers added.
    response = await site.browser.get(data["items"][1]["path"])
    assert response.status_code == 200 and response.headers["content-type"] == PLAYLIST_TYPE
    assert response.headers["access-control-allow-origin"] == "*"
    assert "/r/" in response.text and "cdn.test" not in response.text


async def test_play_refuses_what_it_cant_find(site):
    assert (await post(site, "/api/play", {"provider": "fake", "show": "s1", "number": 9}))[0] == 404
    assert (await post(site, "/api/play", {"provider": "fake", "show": "s1"}))[0] == 400
    assert (await post(site, "/api/play", {"provider": "fake", "number": 1}))[0] == 400
    site.core.cast_server = None
    status, data = await post(site, "/api/play", {"provider": "fake", "show": "s1", "number": 1})
    assert status == 503 and data["error"]
    site.core.cast_server = site.server  # stopped by the fixture


async def test_skip_times_for_auto_skip(site, monkeypatch):
    async def lookup(provider, show, episode):
        assert (provider, show.id, episode.number, episode.title) == ("fake", "s1", 2.0, "Part 2")
        return SkipInfo(52991, [SkipInterval("op", 10.0, 95.0), SkipInterval("ed", 1300.0, 1390.0)])

    monkeypatch.setattr(site.core.skip, "lookup", lookup)
    await site.client.get("/api/episodes", params={"provider": "fake", "show": "s1"})  # the provider's own episode
    data = await (await site.client.get("/api/skip", params={"provider": "fake", "show": "s1", "number": "2"})).json()
    assert data["intervals"] == [
        {"kind": "op", "name": "opening", "start": 10.0, "end": 95.0},
        {"kind": "ed", "name": "ending", "start": 1300.0, "end": 1390.0},
    ]

    async def broken(provider, show, episode):
        raise RuntimeError("AniSkip is down")

    monkeypatch.setattr(site.core.skip, "lookup", broken)
    data = await (await site.client.get("/api/skip", params={"provider": "fake", "show": "s1", "number": "2"})).json()
    assert data == {"summary": "no skip data (lookup failed)", "intervals": []}
    data = await (await site.client.get("/api/skip", params={"provider": "movies", "show": "m", "number": "1"})).json()
    assert data == {"summary": "no skip data for this provider", "intervals": []}


async def test_relay_adds_subtitles_from_the_subtitle_sources(store, tmp_path: Path):
    """An episode whose provider has no subtitles gets the subtitle sources' (cue serve turns this on)."""
    srt = tmp_path / "frieren-02.en.srt"
    srt.write_text(SRT, encoding="cp1252")
    asked = []

    async def find_subtitles(provider_name, show, episode, stream):
        asked.append((provider_name, show.id, episode.number))
        return [("en", str(srt))]

    server = CastServer(loop=asyncio.get_running_loop(), store=store, host="127.0.0.1", port=0, bind="127.0.0.1",
                        announce=False, transport=upstream(), find_subtitles=find_subtitles)
    server.start()
    try:
        generation, _ = server.publish_queue("fake", FakeProvider(), Show("s1", "Show"), [Episode("s1", 2)])
        async with httpx.AsyncClient(base_url=server.base_url, trust_env=False, timeout=10) as browser:
            master = (await browser.get(f"/play/{generation}/0")).text
            assert 'TYPE=SUBTITLES' in master and 'LANGUAGE="en"' in master and "DEFAULT=YES" in master
            assert f"/s/{generation}/0/0.m3u8" in master
            vtt = (await browser.get(f"/s/{generation}/0/0.vtt")).text
        assert vtt.startswith("WEBVTT") and "00:00:01.000 --> 00:00:02.500" in vtt
        assert asked == [("fake", "s1", 2.0)]
    finally:
        server.stop()


async def test_cue_serve_asks_the_subtitle_sources(monkeypatch, store, tmp_path: Path):
    made = []

    class RecordingServer:
        def __init__(self, **options):
            made.append(options)

        def start(self):
            pass

    monkeypatch.setattr("cue.core.CastServer", RecordingServer)
    core = Core(Config(), store=store, http=httpx.AsyncClient(), dropin_dir=tmp_path / "none", plugin_root=tmp_path / "p")
    assert core.start_cast(source_subtitles=True) == ""
    assert core.start_cast() == ""  # the terminal app's casting is as it was
    assert made[0]["find_subtitles"] == core._source_subtitles and "find_subtitles" not in made[1]
    await core.http.aclose()


# -- casting to a TV ---------------------------------------------------------------------------------------


class Receiver(devices.Receiver):
    def __init__(self, played: list, hold: asyncio.Event | None = None) -> None:
        self.played, self.hold, self.closed = played, hold, False

    async def play(self, media: devices.Media) -> None:
        self.played.append(media)
        if self.hold is not None:
            await self.hold.wait()

    async def close(self) -> None:
        self.closed = True


def tvs(monkeypatch, *found: devices.Device, hold: asyncio.Event | None = None) -> SimpleNamespace:
    seen = SimpleNamespace(played=[], receivers=[], codes=[])

    async def discover(credentials, timeout=devices.SCAN_SECONDS):
        return list(found)

    async def connect(device, credentials):
        receiver = Receiver(seen.played, hold)
        seen.receivers.append(receiver)
        return receiver

    async def pair(device, credentials, ask_code):
        seen.codes.append(await ask_code())

    monkeypatch.setattr("cue.cast.devices.discover", discover)
    monkeypatch.setattr("cue.cast.devices.connect", connect)
    monkeypatch.setattr("cue.cast.devices.pair", pair)
    return seen


LIVING_ROOM = devices.Device(devices.RENDERER, "Living room", "uuid:tv-1", "10.0.0.9", "Samsung")


async def test_devices_lists_vlc_and_the_tvs_found(site, monkeypatch):
    tvs(monkeypatch, LIVING_ROOM)
    data = await (await site.client.get("/api/devices")).json()
    assert data["vlc"]["id"] == "vlc" and data["server"] == {"name": "Cue", "available": True, "problem": ""}
    assert data["devices"] == [{"id": "uuid:tv-1", "name": "Living room", "kind": "renderer", "kind_label": "DLNA",
                                "model": "Samsung", "needs_pairing": False}]


async def test_casting_starts_at_the_chosen_episode(site, monkeypatch):
    seen = tvs(monkeypatch, LIVING_ROOM)
    await site.client.get("/api/devices")
    status, job = await post(site, "/api/cast", {"device": "uuid:tv-1", "provider": "fake", "show": "s1",
                                                  "title": "Sousou no Frieren", "number": 2})
    assert status == 200 and job["state"] == "connecting" and job["total"] == 2 and job["episode"]["label"] == "2"
    await eventually(lambda: not site.state.jobs[job["id"]].active)
    # The TV stopped after the first one (as the fake did): that's the end of the queue.
    assert [media.title for media in seen.played] == ["Sousou no Frieren · episode 2"]
    assert seen.played[0].url.startswith(site.server.base_url + "/play/") and seen.played[0].url.endswith("/0")
    assert seen.played[0].content_type == PLAYLIST_TYPE and seen.receivers[0].closed
    assert site.state.jobs[job["id"]].state == "finished" and site.core.last_tv == "uuid:tv-1"


async def test_stop_and_next_on_the_tv(site, monkeypatch):
    hold = asyncio.Event()
    seen = tvs(monkeypatch, LIVING_ROOM, hold=hold)
    await site.client.get("/api/devices")
    _, job = await post(site, "/api/cast", {"device": "uuid:tv-1", "provider": "fake", "show": "s1",
                                            "title": "Sousou no Frieren", "number": 1})
    await eventually(lambda: site.state.jobs[job["id"]].state == "playing")

    status, moved = await post(site, f"/api/cast/{job['id']}/next", {})
    assert status == 200 and moved["id"] != job["id"] and moved["index"] == 1
    assert site.state.jobs[job["id"]].state == "stopped"  # one queue per TV
    await eventually(lambda: site.state.jobs[moved["id"]].state == "playing")
    assert [media.title for media in seen.played] == ["Sousou no Frieren · episode 1", "Sousou no Frieren · episode 2"]

    status, stopped = await post(site, f"/api/cast/{moved['id']}/stop", {})
    assert status == 200 and stopped["state"] == "stopped" and not stopped["active"]
    await eventually(lambda: all(receiver.closed for receiver in seen.receivers))
    listed = (await (await site.client.get("/api/cast")).json())["jobs"]
    assert {entry["id"] for entry in listed} == {job["id"], moved["id"]}
    assert (await post(site, "/api/cast/nope/stop", {}))[0] == 404


async def test_pairing_with_a_code_from_the_page(site, monkeypatch):
    tv = devices.Device(devices.AIRPLAY, "Bedroom", "tv-2", "10.0.0.8", needs_pairing=True)
    seen = tvs(monkeypatch, tv)
    await site.client.get("/api/devices")
    _, job = await post(site, "/api/cast", {"device": "tv-2", "provider": "fake", "show": "s1", "number": 3})
    await eventually(lambda: site.state.jobs[job["id"]].state == "pairing")
    status, _ = await post(site, f"/api/cast/{job['id']}/code", {"code": "12 34"})
    assert status == 200
    await eventually(lambda: not site.state.jobs[job["id"]].active)
    assert seen.codes == ["1234"] and len(seen.played) == 1
    assert site.state.devices["tv-2"].needs_pairing is False  # it won't ask again
    assert (await post(site, f"/api/cast/{job['id']}/code", {"code": "1"}))[0] == 409


async def test_casting_to_vlc_publishes_the_queue(site, monkeypatch):
    status, data = await post(site, "/api/cast", {"device": "vlc", "provider": "fake", "show": "s1", "number": 2})
    assert status == 200 and data["vlc"] is True
    assert data["url"].startswith(site.server.base_url + "/play/") and "VLC → Network → Cue" in data["message"]
    [cast] = site.server.casts()
    assert [episode.number for episode in cast.queue] == [2, 3]
    assert (await post(site, "/api/cast", {"device": "gone", "provider": "fake", "show": "s1", "number": 2}))[0] == 404


async def test_events_reach_open_pages(site):
    response = await site.client.get("/api/events")
    assert response.content_type == "text/event-stream"

    async def event() -> tuple[str, dict]:
        kind, data = "", {}
        while True:
            line = (await response.content.readline()).decode().rstrip("\n")
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
            elif line == "" and kind:
                return kind, data

    assert await event() == ("jobs", {"jobs": []})
    site.core.on_notice("Paired with Bedroom", title="Cast", timeout=6)
    assert await event() == ("notice", {"message": "Paired with Bedroom", "title": "Cast",
                                        "severity": "information", "timeout": 6})
    response.close()


# -- who may use it ------------------------------------------------------------------------------------------


def test_local_addresses_and_names():
    assert all(is_local_address(address) for address in
               ["127.0.0.1", "192.168.1.20", "10.0.0.3", "172.17.0.1", "::1", "::ffff:192.168.1.20", "fe80::1%eth0"])
    assert not any(is_local_address(address) for address in ["8.8.8.8", "2001:4860::8888", "", None, "nonsense"])
    assert all(is_local_host(host, []) for host in
               ["192.168.1.5:8771", "[::1]:8771", "nas", "nas.local:8771", "cue.lan", "box.home.arpa", "localhost:8771"])
    assert not is_local_host("evil.example.com:8771", [])
    assert is_local_host("cue.example.com:8771", ["cue.example.com"])


async def test_only_this_network_and_this_page(site, monkeypatch):
    assert (await site.client.get("/api/info", headers={"Host": "evil.example.com:8771"})).status == 403
    assert (await site.client.get("/api/info", headers={"Host": "nas.local:8771"})).status == 200
    site.state.hosts = ["cue.example.com"]
    assert (await site.client.get("/api/info", headers={"Host": "cue.example.com"})).status == 200

    body = {"provider": "fake", "show": "s1", "number": 1}
    response = await site.client.post("/api/play", data=json.dumps(body), headers={"Content-Type": "text/plain"})
    assert response.status == 415  # a form or text/plain post from another site
    response = await site.client.post("/api/play", json=body, headers={"Origin": "http://evil.example.com"})
    assert response.status == 403
    response = await site.client.post("/api/play", json=body, headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status == 403

    monkeypatch.setattr("cue.web.api.is_local_address", lambda address: False)
    assert (await site.client.get("/api/info")).status == 403


async def test_the_web_app_and_its_files(site):
    response = await site.client.get("/")
    assert response.status == 200 and response.content_type == "text/html"
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    page = await response.text()
    assert "/static/vendor/hls.min.js" in page and "/static/app.js" in page
    for path in ("/static/app.js", "/static/app.css", "/static/icon.svg", "/static/vendor/hls.min.js",
                 "/static/vendor/hls.js-LICENSE.txt"):
        assert (await site.client.get(path)).status == 200, path
    assert (await site.client.get("/static/../api.py")).status in {403, 404}
    assert (await site.client.get("/static/")).status in {403, 404}


# -- running cue serve ------------------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_cue_serve_runs_until_stopped(monkeypatch, capsys):
    from cue.web.serve import serve

    class Quiet:  # never announce on the real network
        def __init__(self, **options):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr("cue.cast.server.Advertiser", Quiet)
    port, cast_port = free_port(), free_port()
    config = Config(cast=CastConfig(port=cast_port, host="127.0.0.1"), web=WebConfig(host="127.0.0.1", port=port))
    stop = asyncio.Event()
    running = asyncio.create_task(serve(config, stop=stop))
    async with httpx.AsyncClient(trust_env=False) as client:
        for _ in range(200):
            try:
                info = (await client.get(f"http://127.0.0.1:{port}/api/info")).json()
                break
            except httpx.TransportError:
                await asyncio.sleep(0.05)
        else:
            raise AssertionError("cue serve didn't answer")
    assert info["cast"]["available"] and info["cast"]["port"] == cast_port  # even with [cast] enabled = false
    stop.set()
    assert await asyncio.wait_for(running, 10) == 0
    output = capsys.readouterr().out
    assert f"is serving http://127.0.0.1:{port}" in output and f"127.0.0.1:{cast_port}" in output


def test_cue_serve_command_line(capsys):
    from cue.__main__ import main

    assert main(["serve", "--port", "70000"]) == 2
    assert "--port" in capsys.readouterr().err


def test_web_config():
    assert parse_config({}).web == WebConfig()
    config = parse_config({"web": {"port": 9000, "host": " 0.0.0.0 ", "hosts": ["Cue.Example.com", " "]}})
    assert config.web == WebConfig(port=9000, host="0.0.0.0", hosts=["cue.example.com"])
    for bad in ({"port": 0}, {"port": "80"}, {"hosts": "cue.example.com"}, {"host": 5}):
        with pytest.raises(ConfigError):
            parse_config({"web": bad})
