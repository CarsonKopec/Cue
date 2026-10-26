from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from cue.player.base import PlayerError
from cue.player.vlc import VlcPlayer


PLAYING = {"state": "playing", "time": 12, "length": 120, "currentplid": 1}


class FakeProcess:
    """One owned process, with real wait/terminate synchronization."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = 0
        self.killed = 0
        self.done = asyncio.Event()

    def exit(self, code: int = 0) -> None:
        self.returncode = code
        self.done.set()

    def terminate(self) -> None:
        self.terminated += 1
        self.exit()

    def kill(self) -> None:
        self.killed += 1
        self.exit(-9)

    async def wait(self) -> int:
        await self.done.wait()
        assert self.returncode is not None
        return self.returncode


@pytest.fixture
def fake_vlc_process(monkeypatch):
    process = FakeProcess()
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr("cue.player.vlc.asyncio.create_subprocess_exec", spawn)
    return process, calls


def flag(args: list[str], name: str) -> str:
    prefix = name + "="
    for index, arg in enumerate(args):
        if arg.startswith(prefix):
            return arg[len(prefix):]
        if arg == name:
            return args[index + 1]
    raise AssertionError(f"missing {name} in {args}")


async def wait_until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_launch_arguments_isolate_vlc_and_preserve_media_headers() -> None:
    player = VlcPlayer(["C:/Program Files/VideoLAN/VLC/vlc.exe"], port=45781, password="test-password")
    try:
        args = player.build_args(
            "https://cdn.example/video.m3u8?token=abc",
            start=45.25,
            title="Episode 2 — A title with spaces",
            headers={"rEfErEr": "https://player.example/", "user-AGENT": "Cue Test"},
            extra_args=["--fs"],
        )
        assert args[0] == "C:/Program Files/VideoLAN/VLC/vlc.exe"
        assert "--no-one-instance" in args
        assert "--ignore-config" in args
        assert "--no-media-library" in args
        assert flag(args, "--http-host") == "127.0.0.1"
        assert flag(args, "--http-port") == "45781"
        assert flag(args, "--http-password") == "test-password"
        assert (Path(flag(args, "--http-src")) / "requests" / "status.json").is_file()
        assert flag(args, "--http-referrer") == "https://player.example/"
        assert flag(args, "--http-user-agent") == "Cue Test"
        assert float(flag(args, "--start-time")) == 45.25
        assert flag(args, "--meta-title") == "Episode 2 — A title with spaces"
        assert "--fullscreen" in args and "--fs" not in args
        assert args[-2:] == ["--", "https://cdn.example/video.m3u8?token=abc"]
    finally:
        await player.quit()


async def test_each_player_uses_fresh_control_credentials() -> None:
    first, second = VlcPlayer("vlc"), VlcPlayer("vlc")
    try:
        first_args = first.build_args("movie.mkv")
        second_args = second.build_args("movie.mkv")
        assert flag(first_args, "--http-password") != flag(second_args, "--http-password")
        assert len(flag(first_args, "--http-password")) >= 20
        assert 1024 <= int(flag(first_args, "--http-port")) <= 65535
    finally:
        await first.quit()
        await second.quit()


@pytest.mark.parametrize("headers", [
    {"Authorization": 'MediaBrowser Token="secret"'},
    {"Cookie": "session=secret"},
    {"Referer": "https://player.example/\r\nX-Injected: yes"},
    {"User-Agent": "Cue\nInjected"},
])
async def test_unrepresentable_headers_fail_before_launch(headers, fake_vlc_process) -> None:
    _process, calls = fake_vlc_process
    player = VlcPlayer("vlc")
    try:
        with pytest.raises(PlayerError):
            await player.launch("https://cdn.example/video.m3u8", headers=headers)
        assert not calls
    finally:
        await player.quit()


async def test_requests_use_loopback_password_auth_and_exact_control_route() -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=PLAYING)

    player = VlcPlayer("vlc", port=45782, password="test-password", transport=httpx.MockTransport(respond))
    try:
        assert await player._request() == PLAYING
        request = requests[0]
        assert request.url == "http://127.0.0.1:45782/requests/status.json"
        assert request.headers["Authorization"] == "Basic " + base64.b64encode(b":test-password").decode()
    finally:
        await player.quit()


@pytest.mark.parametrize("response", [
    httpx.Response(401, text="Not authorized"),
    httpx.Response(302, headers={"location": "https://remote.example/control"}),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json=[]),
])
async def test_invalid_control_responses_raise_player_error_without_redirecting(response) -> None:
    requests = []

    def respond(request):
        requests.append(request)
        return response

    player = VlcPlayer("vlc", transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(PlayerError):
            await player._request()
        assert len(requests) == 1
        assert requests[0].url.host == "127.0.0.1"
    finally:
        await player.quit()


async def test_launch_controls_and_quit_only_owned_process(fake_vlc_process, tmp_path: Path) -> None:
    process, calls = fake_vlc_process
    requests: list[httpx.Request] = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=PLAYING)

    player = VlcPlayer("vlc", poll_interval=60, transport=httpx.MockTransport(respond))
    closes = []
    player.on_close(lambda: closes.append(True))
    subtitle = tmp_path / "a subtitle with spaces.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    try:
        await player.launch("movie.mkv")
        assert player.running and len(calls) == 1
        await player.command("seek", 80, "absolute")
        await player.command("seek", 15, "relative")
        await player.command("seek", -5, "relative")
        await player.command("seek", 83.4, "absolute")
        await player.command("cycle", "sub")
        await player.command("sub-add", str(subtitle), "select")
        await player.command("sub-add", str(subtitle), "auto")
        count = len(requests)
        await player.command("show-text", "Skipped opening", 2000)
        assert len(requests) == count

        commands = [dict(request.url.params) for request in requests if "command" in request.url.params]
        seek_values = [entry["val"] for entry in commands if entry["command"] == "seek"]
        assert float(seek_values[0]) == 80
        assert seek_values[1].startswith("+") and float(seek_values[1]) == 15
        assert seek_values[2].startswith("-") and float(seek_values[2]) == -5
        # VLC 3 common.seek's parser accepts integer time syntax; rounding up
        # also prevents repeated intro-skip seeks to just before the boundary.
        assert seek_values[3] == "84"
        assert all("." not in value for value in seek_values)
        assert {"command": "key", "val": "subtitle-track"} in commands
        subtitle_commands = [entry for entry in commands if entry["command"] == "cue_addsubtitle"]
        assert subtitle_commands == [
            {"command": "cue_addsubtitle", "val": subtitle.as_uri(), "select": "true"},
            {"command": "cue_addsubtitle", "val": subtitle.as_uri(), "select": "false"},
        ]
    finally:
        await player.quit(timeout=0.1)
    assert process.returncode is not None
    assert process.terminated + process.killed >= 1
    assert not player.running and closes == [True]
    with pytest.raises(PlayerError):
        await player.command("seek", 3, "absolute")


async def test_status_emits_position_duration_and_eof_once_at_natural_end() -> None:
    player = VlcPlayer("vlc")
    positions, durations, eof = [], [], []
    player.observe("time-pos", positions.append)
    player.observe("duration", durations.append)
    player.observe("eof-reached", eof.append)
    try:
        player._update_status({"state": "opening", "time": 0, "length": 0})
        player._update_status({"state": "playing", "time": 118, "length": 120})
        player._update_status({"state": "paused", "time": 118, "length": 120})
        assert not any(eof)
        player._update_status({"state": "playing", "time": 119, "length": 120, "position": 119 / 120})
        player._update_status({"state": "stopped", "time": 0, "length": 0})
        player._update_status({"state": "stopped", "time": 0, "length": 0})
        assert 118 in positions and 120 in durations
        assert eof.count(True) == 1
    finally:
        await player.quit()


@pytest.mark.parametrize("statuses", [
    [{"state": "stopped", "time": 0, "length": 0}],
    [PLAYING, {"state": "stopped", "time": 0, "length": 0}],
    [{"state": "paused", "time": 119, "length": 120}, {"state": "stopped", "time": 0, "length": 0}],
    [{"state": "playing", "time": 120, "length": 0}, {"state": "stopped", "time": 0, "length": 0}],
])
async def test_startup_manual_stop_and_unknown_duration_do_not_mark_watched(statuses) -> None:
    player = VlcPlayer("vlc")
    eof = []
    player.observe("eof-reached", eof.append)
    try:
        for status in statuses:
            player._update_status(status)
        assert not any(eof)
    finally:
        await player.quit()


async def test_failed_start_reports_error_and_cleans_up(fake_vlc_process) -> None:
    process, _calls = fake_vlc_process

    def unavailable(request):
        raise httpx.ConnectError("not listening", request=request)

    player = VlcPlayer("vlc", connect_timeout=0.01, transport=httpx.MockTransport(unavailable))
    with pytest.raises(PlayerError):
        await player.launch("movie.mkv")
    assert process.returncode is not None
    assert not player.running
    await player.quit()


async def test_http_ready_without_media_times_out_and_cleans_up(fake_vlc_process) -> None:
    process, _calls = fake_vlc_process
    player = VlcPlayer(
        "vlc", connect_timeout=0.01,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"state": "stopped", "currentplid": -1})),
    )
    with pytest.raises(PlayerError):
        await player.launch("missing.mkv")
    assert process.returncode is not None
    assert not player.running
    await player.quit()


async def test_poll_failure_closes_without_signalling_eof(fake_vlc_process) -> None:
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json=PLAYING)
        raise httpx.ConnectError("control lost", request=request)

    player = VlcPlayer("vlc", poll_interval=0.005, transport=httpx.MockTransport(respond))
    closes, eof, errors = [], [], []
    player.on_close(lambda: closes.append(True))
    player.observe("eof-reached", eof.append)
    player.observe("error", errors.append)
    try:
        await player.launch("movie.mkv")
        await wait_until(lambda: bool(closes))
        assert not player.running and not any(eof)
        assert errors
    finally:
        await player.quit(timeout=0.1)
    assert closes == [True]


async def test_manual_stop_closes_without_eof(fake_vlc_process) -> None:
    count = 0

    def respond(_request):
        nonlocal count
        count += 1
        payload = PLAYING if count == 1 else {"state": "stopped", "time": 0, "length": 0}
        return httpx.Response(200, json=payload)

    player = VlcPlayer("vlc", poll_interval=0.005, transport=httpx.MockTransport(respond))
    eof, closes = [], []
    player.observe("eof-reached", eof.append)
    player.on_close(lambda: closes.append(True))
    try:
        await player.launch("movie.mkv")
        await wait_until(lambda: bool(closes))
        assert not any(eof)
    finally:
        await player.quit(timeout=0.1)
    assert closes == [True]


async def test_missing_executable_is_player_error(monkeypatch) -> None:
    async def missing(*_args, **_kwargs):
        raise FileNotFoundError("no VLC executable")

    monkeypatch.setattr("cue.player.vlc.asyncio.create_subprocess_exec", missing)
    player = VlcPlayer("missing-vlc")
    try:
        with pytest.raises(PlayerError, match="(?i)vlc"):
            await player.launch("movie.mkv")
        assert not player.running
    finally:
        await player.quit()


async def test_fractional_position_avoids_rounding_skip_boundaries() -> None:
    player = VlcPlayer("vlc")
    positions = []
    player.observe("time-pos", positions.append)
    try:
        player._update_status({"state": "playing", "time": 10, "length": 100, "position": 0.1075})
        assert positions[-1] == pytest.approx(10.75)
    finally:
        await player.quit()


async def test_negative_preroll_timing_is_ignored_then_valid_timing_resumes() -> None:
    player = VlcPlayer("vlc")
    positions, durations, eof = [], [], []
    player.observe("time-pos", positions.append)
    player.observe("duration", durations.append)
    player.observe("eof-reached", eof.append)
    try:
        player._update_status({"state": "playing", "time": -1, "position": -0.1, "length": -1})
        assert positions == durations == []
        player._update_status({"state": "playing", "time": 1, "position": 1 / 6, "length": 6})
        assert positions == [pytest.approx(1)]
        assert durations == [6]
        assert not any(eof)
    finally:
        await player.quit()


async def test_a_transient_poll_failure_recovers(fake_vlc_process) -> None:
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        if count == 2:
            raise httpx.ReadError("temporary interruption", request=request)
        return httpx.Response(200, json=PLAYING)

    player = VlcPlayer("vlc", poll_interval=0.005, transport=httpx.MockTransport(respond))
    errors = []
    player.observe("error", errors.append)
    try:
        await player.launch("movie.mkv")
        await wait_until(lambda: count >= 4)
        assert player.running and not errors
    finally:
        await player.quit(timeout=0.1)


async def test_process_crash_reports_error_and_never_eof(fake_vlc_process) -> None:
    process, _calls = fake_vlc_process
    player = VlcPlayer(
        "vlc", poll_interval=0.005,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=PLAYING)),
    )
    errors, eof = [], []
    player.observe("error", errors.append)
    player.observe("eof-reached", eof.append)
    try:
        await player.launch("movie.mkv")
        process.exit(7)
        await asyncio.wait_for(player.wait_closed(), 1)
        assert errors and "7" in str(errors[-1])
        assert not player.running and not any(eof)
    finally:
        await player.quit()


async def test_quit_requests_owned_player_exit_before_forcing_it(fake_vlc_process) -> None:
    process, _calls = fake_vlc_process

    def respond(request):
        if request.url.params.get("command") == "key" and request.url.params.get("val") == "quit":
            process.exit()
        return httpx.Response(200, json=PLAYING)

    player = VlcPlayer("vlc", poll_interval=60, transport=httpx.MockTransport(respond))
    await player.launch("movie.mkv")
    await player.quit(timeout=0.1)
    assert process.returncode == 0 and process.killed == process.terminated == 0
    assert not player.running


async def test_command_timeout_is_player_error_and_does_not_signal_eof(fake_vlc_process) -> None:
    async def respond(request):
        if request.url.params.get("command") == "seek":
            await asyncio.sleep(10)
        return httpx.Response(200, json=PLAYING)

    player = VlcPlayer("vlc", poll_interval=60, transport=httpx.MockTransport(respond))
    eof = []
    player.observe("eof-reached", eof.append)
    try:
        await player.launch("movie.mkv")
        with pytest.raises(PlayerError, match="(?i)answer.*seek|seek.*answer"):
            await player.command("seek", 20, "absolute", timeout=0.01)
        assert player.running and not any(eof)
    finally:
        await player.quit(timeout=0.1)


@pytest.mark.parametrize("seek", [(120, "absolute"), (200, "absolute"), (120, "relative")])
async def test_seek_to_end_does_not_lose_eof_between_polls(fake_vlc_process, seek) -> None:
    """An outro skip may stop the media before VLC reports its final position."""
    stopped = False

    def respond(request):
        nonlocal stopped
        if request.url.params.get("command") == "seek":
            stopped = True
        payload = {"state": "stopped", "time": 0, "length": 0} if stopped else PLAYING
        return httpx.Response(200, json=payload)

    player = VlcPlayer("vlc", poll_interval=0.005, transport=httpx.MockTransport(respond))
    events = []
    player.observe("eof-reached", lambda value: value and events.append("eof"))
    player.on_close(lambda: events.append("closed"))
    try:
        await player.launch("movie.mkv")
        await player.command("seek", *seek)
        await asyncio.wait_for(player.wait_closed(), 1)
        # PlaybackSession consumes the first action, so EOF must precede close.
        assert events == ["eof", "closed"]
    finally:
        await player.quit(timeout=0.1)


async def test_earlier_seek_clears_pending_end_seek_before_manual_stop(fake_vlc_process) -> None:
    stopped = False

    def respond(_request):
        payload = {"state": "stopped", "time": 0, "length": 0} if stopped else PLAYING
        return httpx.Response(200, json=payload)

    player = VlcPlayer("vlc", poll_interval=0.005, transport=httpx.MockTransport(respond))
    eof = []
    player.observe("eof-reached", eof.append)
    try:
        await player.launch("movie.mkv")
        await player.command("seek", 200, "absolute")
        await player.command("seek", 30, "absolute")
        stopped = True
        await asyncio.wait_for(player.wait_closed(), 1)
        assert not any(eof)
    finally:
        await player.quit(timeout=0.1)


async def test_failed_end_seek_does_not_turn_later_manual_stop_into_eof(fake_vlc_process) -> None:
    stopped = False

    def respond(request):
        if request.url.params.get("command") == "seek":
            return httpx.Response(500, text="cannot seek")
        payload = {"state": "stopped", "time": 0, "length": 0} if stopped else PLAYING
        return httpx.Response(200, json=payload)

    player = VlcPlayer("vlc", poll_interval=0.005, transport=httpx.MockTransport(respond))
    eof = []
    player.observe("eof-reached", eof.append)
    try:
        await player.launch("movie.mkv")
        with pytest.raises(PlayerError):
            await player.command("seek", 200, "absolute")
        stopped = True
        await asyncio.wait_for(player.wait_closed(), 1)
        assert not any(eof)
    finally:
        await player.quit(timeout=0.1)
