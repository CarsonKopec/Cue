"""Any player, any stream: the local relay, one continuous stream, downloads, and who gets what."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from cue.cast import hls
from cue.player.base import PlayerError
from cue.player.command import CommandPlayer, split_arguments
from cue.player.locate import find_ffmpeg
from cue.player.registry import players
from cue.player.relay import LocalRelay, RelayingPlayer, Storage, place_subtitles, route_player, vtt_to_srt
from cue.providers.base import Stream

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Italiano",DEFAULT=NO,LANGUAGE="ita",URI="audio/ita.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",DEFAULT=YES,LANGUAGE="eng",URI="audio/eng.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="eng",URI="subs/eng.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=854x480,AUDIO="aud",SUBTITLES="subs"
video/480.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080,AUDIO="aud",SUBTITLES="subs"
video/1080.m3u8
"""
MEDIA = "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg0.ts\n#EXTINF:3.5,\nhttps://cdn.test/seg1.ts\n#EXT-X-ENDLIST\n"


def test_single_variant_keeps_the_best_video_and_default_audio() -> None:
    cut = hls.single_variant(MASTER)
    assert "video/1080.m3u8" in cut and "video/480.m3u8" not in cut
    assert 'URI="audio/eng.m3u8"' in cut and "ita" not in cut
    assert "SUBTITLES" not in cut and cut.count("#EXT-X-STREAM-INF") == 1
    no_default = MASTER.replace("DEFAULT=YES", "DEFAULT=NO")
    assert 'URI="audio/ita.m3u8"' in hls.single_variant(no_default)  # then the group's first
    assert hls.single_variant(MEDIA) == MEDIA  # not a master playlist: unchanged


# -- the relay over a real local socket ------------------------------------------------


@pytest.fixture
def upstream():
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("referer") != "https://site.test/":
            return httpx.Response(403)
        path = request.url.path
        if path == "/master.m3u8":
            return httpx.Response(200, text=MASTER)
        if path.endswith(".m3u8"):
            return httpx.Response(200, text=MEDIA)
        if path.endswith(".ts"):
            return httpx.Response(206 if "range" in request.headers else 200, content=b"\x47" * 188,
                                  headers={"content-type": "video/mp2t"})
        return httpx.Response(404)

    relay = LocalRelay(transport=httpx.MockTransport(respond))
    relay.start()
    yield relay, seen
    relay.stop()


STREAM = Stream("https://site.test/master.m3u8", {"Referer": "https://site.test/", "Authorization": "secret"})


def test_relay_links_need_no_headers(upstream) -> None:
    relay, seen = upstream
    shared = relay.share(STREAM)
    master = httpx.get(shared.hls).text
    links = [line for line in master.splitlines() if line and not line.startswith("#")]
    assert len(links) == 2 and all(link.startswith(relay.base_url + "/h/") and link.endswith(".m3u8") for link in links)
    assert 'URI="' + relay.base_url in master and "site.test" not in master

    media = httpx.get(links[1]).text
    segments = [line for line in media.splitlines() if line and not line.startswith("#")]
    assert all(segment.endswith(".ts") for segment in segments)
    assert relay.duration(shared.stream_id) == 7.5

    piece = httpx.get(segments[1], headers={"Range": "bytes=0-187"})
    assert piece.status_code == 206 and piece.content == b"\x47" * 188
    cdn = seen[-1]
    assert cdn.url.host == "cdn.test" and cdn.headers["range"] == "bytes=0-187"
    # The source's login goes only to its own site; the CDN gets the Referer it asked for.
    assert cdn.headers["referer"] == "https://site.test/" and "authorization" not in cdn.headers
    assert seen[0].headers["authorization"] == "secret"

    assert httpx.get(f"{relay.base_url}/h/{'0' * 32}.ts").status_code == 404


def test_single_link_and_continuous_without_ffmpeg(upstream) -> None:
    relay, _ = upstream
    shared = relay.share(STREAM)
    cut = httpx.get(shared.single).text
    assert cut.count("#EXT-X-STREAM-INF") == 1 and cut.count("TYPE=AUDIO") == 1
    response = httpx.get(shared.continuous, follow_redirects=False)
    # Without FFmpeg a plain video still plays; an HLS source is sent on as its cut-down playlist.
    assert response.status_code == 302 and response.headers["location"] == shared.single


def test_save_needs_ffmpeg_or_ytdlp(upstream, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    relay, _ = upstream
    monkeypatch.setattr("cue.download.find_ytdlp", lambda: None)
    with pytest.raises(PlayerError, match="needs FFmpeg"):
        relay.save(STREAM, "Show", folder=tmp_path)


@pytest.mark.skipif(find_ffmpeg() is None, reason="needs FFmpeg")
def test_ffmpeg_joins_streams_and_saves_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run([
        find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=320x240:rate=10",
        "-f", "lavfi", "-i", "sine=duration=4", "-shortest", "-c:v", "libx264", "-g", "10", "-c:a", "aac",
        "-f", "hls", "-hls_time", "1", "-hls_playlist_type", "vod", str(source / "index.m3u8"),
    ], check=True, timeout=60)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.headers.get("referer") != "https://site.test/":
            return httpx.Response(403)
        file = source / request.url.path.lstrip("/")
        return httpx.Response(200, content=file.read_bytes()) if file.is_file() else httpx.Response(404)

    relay = LocalRelay(ffmpeg=find_ffmpeg(), transport=httpx.MockTransport(respond))
    relay.start()
    try:
        stream = Stream("https://site.test/index.m3u8", {"Referer": "https://site.test/"})
        continuous = httpx.get(relay.share(stream).continuous, timeout=60).content
        assert len(continuous) > 10_000 and continuous[0] == 0x47  # MPEG-TS sync byte
        messages: list[str] = []
        saved = relay.save(stream, "Show: Episode 1", progress=messages.append, folder=tmp_path / "out")
        assert saved.name == "Show Episode 1.mp4" and saved.read_bytes()[4:8] == b"ftyp"
        # Pieces are fetched in parallel, then joined from local files.
        assert messages and all(message.endswith("%") for message in messages)
        assert list((tmp_path / "out").iterdir()) == [saved]  # no pieces or partial files left

        # With subtitles they go inside (an MKV; an MP4 on macOS), the first as the default.
        english, french = tmp_path / "en.srt", tmp_path / "fr.srt"
        english.write_text("1\n00:00:00,500 --> 00:00:03,500\nHello\n", encoding="utf-8-sig")
        french.write_text("1\n00:00:00,500 --> 00:00:03,500\nBonjour\n", encoding="utf-8-sig")
        subtitled = relay.save(stream, "Show: Episode 1", folder=tmp_path / "subtitled",
                               subtitles=[("en", english), ("fr", french)])
        assert subtitled.name == "Show Episode 1" + (".mp4" if sys.platform == "darwin" else ".mkv")
        probe = _probe(subtitled)
        tracks = [line for line in probe.splitlines() if "Subtitle:" in line]
        assert len(tracks) == 2 and "(en" in tracks[0] and "(default)" in tracks[0]
        assert "(fr" in tracks[1] and "(default)" not in tracks[1]
        assert "Video:" in probe and "Audio:" in probe

        # Burned in: an MP4 whose picture has the first subtitle drawn on, and no subtitle tracks.
        burner = relay.burn_encoder()
        if burner is None:
            pytest.skip("this FFmpeg can't draw subtitles (no libass)")
        burned = relay.save(stream, "Show: Episode 1", folder=tmp_path / "burned",
                            subtitles=[("en", english), ("fr", french)], burn=burner)
        assert burned.name == "Show Episode 1.mp4" and "Subtitle:" not in _probe(burned)
        assert list((tmp_path / "burned").iterdir()) == [burned]
        plain, drawn = _frame(saved), _frame(burned)
        rows = len(plain) // 320

        def changed(first: int, last: int) -> int:
            """Pixels the subtitle could have changed: re-encoding alone moves them only a little."""
            part = slice(first * 320, last * 320)
            return sum(1 for a, b in zip(plain[part], drawn[part], strict=True) if abs(a - b) > 64)

        bottom, top = changed(rows * 3 // 4, rows), changed(0, rows // 4)
        assert bottom > 100 and top * 20 < bottom, (bottom, top)
    finally:
        relay.stop()


def _probe(path: Path) -> str:
    return subprocess.run([find_ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                          timeout=30).stderr


def _frame(path: Path) -> bytes:
    """The grey picture 2 seconds in."""
    return subprocess.run([find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", "2", "-i", str(path),
                           "-frames:v", "1", "-vf", "format=gray", "-f", "rawvideo", "-"],
                          capture_output=True, check=True, timeout=30).stdout


def test_burning_uses_the_first_encoder_that_works(monkeypatch: pytest.MonkeyPatch) -> None:
    import cue.player.relay as relay_module

    ran: list[list[str]] = []

    def run(command, **options):
        ran.append(command)
        if "-filters" in command:
            return subprocess.CompletedProcess(command, 0, " .. subtitles         V->V       Render text subtitles\n", "")
        return subprocess.CompletedProcess(command, 0 if "h264_amf" in command else 1, "", "")

    monkeypatch.setattr(relay_module.subprocess, "run", run)
    relay = LocalRelay(ffmpeg="ffmpeg")
    burner = relay.burn_encoder()
    assert burner is not None and burner[:2] == ["-c:v", "h264_amf"]  # NVIDIA's and Intel's had no GPU here
    assert relay.burn_encoder() is burner and len(ran) == 4  # checked once
    tried = [command[command.index("-c:v") + 1] for command in ran if "-c:v" in command]
    assert tried == ["h264_nvenc", "h264_qsv", "h264_amf"]

    monkeypatch.setattr(relay_module.subprocess, "run",
                        lambda command, **options: subprocess.CompletedProcess(command, 0, " .. scale  V->V\n", ""))
    assert LocalRelay(ffmpeg="ffmpeg").burn_encoder() is None  # no libass, no burning
    assert LocalRelay().burn_encoder() is None

    shared = LocalRelay(ffmpeg="ffmpeg").share(Stream("https://site.test/a.m3u8"))
    command = LocalRelay(ffmpeg="ffmpeg").ffmpeg_command(shared, "out.mp4", "mp4", [("en", Path("w/subtitles.srt"))],
                                                         burner)
    assert command[command.index("-vf") + 1] == "subtitles=subtitles.srt" and command.count("-i") == 1
    assert command[command.index("-c:a") + 1] == "copy" and "h264_amf" in command


def test_prefetch_holds_a_small_window_retries_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    import cue.player.relay as relay_module

    monkeypatch.setattr(relay_module, "WINDOW", 4)
    real_sleep = time.sleep
    monkeypatch.setattr(relay_module.time, "sleep", lambda seconds: None)  # no waiting between retries
    failures = {"seg1.ts": 2}  # the second piece fails twice, then arrives
    fetched: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        fetched.append(name)
        if failures.get(name):
            failures[name] -= 1
            return httpx.Response(503)
        return httpx.Response(200, content=name.encode())

    relay = LocalRelay(transport=httpx.MockTransport(respond))
    shared = relay.share(Stream("https://site.test/index.m3u8"))
    tokens = [relay_module._token(relay.link(f"https://site.test/seg{n}.ts", "segment", shared.stream_id))
              for n in range(12)]
    prefetch = relay_module._Prefetch(relay, tokens, {}, threading.Event(), 0)
    prefetch.start()
    try:
        real_sleep(0.3)
        assert len(fetched) <= 4 + 2  # the window, plus seg1's failed tries
        held = []
        for n, token in enumerate(tokens):
            assert prefetch.take(token) == f"seg{n}.ts".encode()
            held.append(len(prefetch._ready) + len(prefetch._busy))
        assert max(held) <= 4 and prefetch.taken == 12
        assert fetched.count("seg1.ts") == 3
        assert prefetch.take(tokens[0]) is None  # asked for again: the relay fetches it as usual
    finally:
        prefetch.stop()

    stopped = relay_module._Prefetch(relay, tokens, {}, threading.Event(), 0)
    stopped.stop()
    with pytest.raises(PlayerError, match="Download stopped"):
        stopped.take(tokens[0])


def test_downloads_decrypt_aes_pieces_on_their_own_threads() -> None:
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    import cue.player.relay as relay_module

    key = bytes(range(16, 32))
    fixed_iv = bytes(range(16))

    def encrypt(data: bytes, iv: bytes) -> bytes:
        padding = 16 - len(data) % 16
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return encryptor.update(data + bytes([padding]) * padding) + encryptor.finalize()

    playlist = (
        '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:5\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
        "#EXTINF:4,\nseg0.ts\n#EXTINF:4,\nseg1.ts\n"
        f'#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x{fixed_iv.hex()}\n#EXTINF:4,\nseg2.ts\n'
        "#EXT-X-KEY:METHOD=NONE\n#EXTINF:4,\nseg3.ts\n#EXT-X-ENDLIST\n"
    )
    key_requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "index.m3u8":
            return httpx.Response(200, text=playlist)
        if name == "key.bin":
            key_requests.append(name)
            return httpx.Response(200, content=key)
        number = int(name[3])
        text = f"piece {number}".encode()
        if number == 3:
            return httpx.Response(200, content=text)  # after METHOD=NONE: not encrypted
        # Without an IV, a piece's IV is its sequence number (5, 6, ...).
        return httpx.Response(200, content=encrypt(text, fixed_iv if number == 2 else (5 + number).to_bytes(16, "big")))

    relay = LocalRelay(transport=httpx.MockTransport(respond))
    relay.start()
    try:
        shared = relay.share(Stream("https://site.test/index.m3u8"))
        prefetch = relay._plan(shared, None)
        assert prefetch is not None
        given_to_ffmpeg = prefetch.playlist(relay_module._token(shared.single))
        assert "EXT-X-KEY" not in given_to_ffmpeg and given_to_ffmpeg.count("#EXTINF") == 4
        assert len(key_requests) == 1  # one key, fetched once
        tokens = list(prefetch._order)
        prefetch.start()
        try:
            assert [prefetch.take(token) for token in tokens] == [
                b"piece 0", b"piece 1", b"piece 2", b"piece 3"]
        finally:
            prefetch.stop()
    finally:
        relay.stop()

    # What Cue can't decrypt itself stays with FFmpeg.
    link = relay.link("https://site.test/k", "segment", shared.stream_id)
    for tag in ('#EXT-X-KEY:METHOD=SAMPLE-AES,URI="{link}"', '#EXT-X-KEY:METHOD=AES-128,URI="{link}"\n#EXT-X-BYTERANGE:10@0',
                '#EXT-X-KEY:METHOD=AES-128,URI="{link}",IV=0x01\n#EXT-X-MAP:URI="{link}"'):
        assert relay_module._encryption(f"#EXTM3U\n{tag.format(link=link)}\n#EXTINF:4,\n{link}\n") is None
    assert relay_module._encryption(MEDIA) == (MEDIA.replace("\n\n", "\n"), {})  # nothing encrypted


def test_download_quality_and_free_space(upstream, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    relay, _ = upstream
    assert "RESOLUTION=854x480" in httpx.get(relay.share(STREAM, "720").single).text
    assert "RESOLUTION=1920x1080" in httpx.get(relay.share(STREAM, "best").single).text
    assert "RESOLUTION=854x480" in httpx.get(relay.share(STREAM, "worst").single).text

    relay.ffmpeg = "ffmpeg-not-needed"
    monkeypatch.setattr("cue.player.relay.shutil.disk_usage", lambda path: type("Usage", (), {"free": 1_000})())
    with pytest.raises(PlayerError, match="Not enough free space .* needs about .* and 1 MB is free|Not enough free space"):
        relay.save(STREAM, "Show", folder=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_tidy_keeps_what_it_should(tmp_path: Path) -> None:
    import os
    import time

    from cue.player.relay import Storage, tidy

    now = time.time()

    def episode(name: str, megabytes: int, age_hours: float) -> Path:
        path = tmp_path / name
        path.write_bytes(b"x" * megabytes * 1_000_000)
        os.utime(path, (now - age_hours * 3600, now - age_hours * 3600))
        return path

    old, recent, newest, broken = (episode("old.mp4", 3, 30), episode("recent.mp4", 2, 2),
                                   episode("newest.mp4", 2, 1), episode("x.part.mp4", 1, 0))
    tidy(tmp_path, Storage(keep_hours=24, max_mb=0))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["newest.mp4", "recent.mp4"]  # too old, and a leftover part
    tidy(tmp_path, Storage(keep_hours=24, max_mb=3))
    assert [p.name for p in tmp_path.iterdir()] == ["newest.mp4"]  # the oldest went to fit 3 MB

    playing = episode("playing.mp4", 1, 5)
    with playing.open("rb"):
        tidy(tmp_path, Storage(keep_hours=0, max_mb=0), spare={newest})
        # Windows can't delete a file a player has open, so it stays until next time;
        # Linux and macOS delete it, and the player keeps reading what it has open.
        kept = ["newest.mp4", "playing.mp4"] if sys.platform == "win32" else ["newest.mp4"]
        assert sorted(p.name for p in tmp_path.iterdir()) == kept
    tidy(tmp_path, Storage(keep_hours=0, max_mb=0))
    assert list(tmp_path.iterdir()) == []
    assert not old.exists() and not recent.exists() and not broken.exists()


# -- who gets what --------------------------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.launches: list[tuple[str, dict]] = []
        self.quits = 0
        self.running = False

    def observe(self, name, callback) -> None:
        pass

    def on_close(self, callback) -> None:
        pass

    async def launch(self, url, **options) -> None:
        self.launches.append((url, options))

    async def command(self, *args, timeout=5.0):
        raise PlayerError("no")

    async def quit(self, timeout=3.0) -> None:
        self.quits += 1

    def kill(self) -> None:
        pass


def _routed(**capabilities) -> RelayingPlayer:
    return RelayingPlayer(Recorder(), title="Test", relay=lambda: None, **capabilities)


def test_routes(tmp_path: Path) -> None:
    media = tmp_path / "Episode 1.mkv"
    media.write_bytes(b"")
    vlc = _routed(headers=frozenset({"referer", "user-agent"}), hls=True, links=True)
    assert vlc.route("https://site.test/a.m3u8", {"Referer": "x"}) == "direct"
    assert vlc.route("https://jellyfin.test/v.mkv", {"Authorization": "x"}) == "hls"  # relay adds it
    assert vlc.route(str(media), {"Authorization": "x"}) == "direct"
    plain = _routed(headers=frozenset(), hls=False, links=True)
    assert plain.route("https://site.test/a.m3u8", None) == "continuous"
    files_only = _routed(headers=frozenset(), hls=True, links=False)
    assert files_only.route("https://site.test/a.m3u8", None) == "file"
    assert files_only.route(str(media), None) == "direct"
    capable = Recorder()
    assert route_player(capable, title="mpv", headers=None, hls=True, links=True, relay=lambda: None) is capable


async def test_relayed_launch_gives_a_header_free_link(upstream) -> None:
    relay, _ = upstream
    recorder = Recorder()
    player = RelayingPlayer(recorder, title="Test", relay=lambda: relay, headers=frozenset(), hls=True, links=True)
    await player.launch(STREAM.url, start=42.0, headers=STREAM.headers, title="Show")
    [(url, options)] = recorder.launches
    assert url.startswith(relay.base_url + "/h/") and options["headers"] is None and options["start"] == 42.0


async def test_files_only_players_get_a_download(tmp_path: Path) -> None:
    recorder = Recorder()
    saved = tmp_path / "Show.mp4"
    calls = []

    class FakeRelay:
        def save(self, stream, name, *, progress, cancelled, folder, quality, storage, subtitles, burn):
            calls.append((stream.url, name, cancelled, quality))
            progress("50%")
            saved.write_bytes(b"video")
            return saved

    player = RelayingPlayer(recorder, title="Default app", relay=FakeRelay, headers=frozenset(), hls=True, links=False,
                            quality="720")
    messages: list[str] = []
    player.observe("message", messages.append)
    await player.launch("https://site.test/a.m3u8", start=30.0, headers={"Referer": "x"}, title="Show")
    await asyncio.sleep(0)  # progress arrives through the event loop
    assert recorder.launches == [(str(saved), {"start": None, "headers": None, "title": "Show", "extra_args": ()})]
    assert calls[0][:2] == ("https://site.test/a.m3u8", "Show") and calls[0][3] == "720"
    assert "Downloading for Default app… 50%" in messages
    await player.quit()
    assert calls[0][2].is_set() and recorder.quits == 1  # stopping also stops a download in progress
    assert not saved.exists()  # kept for no time at all: deleted once you're done

    kept = RelayingPlayer(Recorder(), title="Default app", relay=FakeRelay, headers=frozenset(), hls=True,
                          links=False, storage=Storage(keep_hours=48))
    await kept.launch("https://site.test/a.m3u8", title="Show")
    await kept.quit()
    assert saved.exists()  # kept for replays


async def test_files_only_players_get_subtitles_inside_the_download(tmp_path: Path) -> None:
    subtitle = tmp_path / "found.vtt"
    subtitle.write_text("WEBVTT\n\n00:01.000 --> 00:02.000\nHi\n", encoding="utf-8")
    given: list[list[tuple[str, str]]] = []
    copies: list[Path] = []

    class FakeRelay:
        ffmpeg = "ffmpeg"

        def save(self, stream, name, *, subtitles, **options):
            # What FFmpeg gets: UTF-8 SRT, converted before the download starts.
            given.append([(language, Path(path).read_text(encoding="utf-8-sig")) for language, path in subtitles])
            copies.extend(Path(path) for _, path in subtitles)
            saved = tmp_path / "Show.mkv"
            saved.write_bytes(b"video")
            return saved

    recorder = Recorder()
    player = RelayingPlayer(recorder, title="Default app", relay=FakeRelay, headers=frozenset(), hls=True, links=False)
    found = asyncio.get_running_loop().create_future()
    player.give_subtitles(found)
    asyncio.get_running_loop().call_later(0.05, found.set_result, [("en", str(subtitle))])
    await player.launch("https://site.test/a.m3u8", title="Show")
    assert given == [[("en", "1\n00:00:01,000 --> 00:00:02,000\nHi\n")]]
    assert player.embedded == ["en"] and player.sidecars == []  # inside the file, so nothing beside it
    assert recorder.launches[0][0] == str(tmp_path / "Show.mkv")
    assert copies and not any(path.exists() for path in copies)  # the converted copies are gone


async def test_files_only_players_burn_subtitles_in_when_asked(tmp_path: Path) -> None:
    subtitle = tmp_path / "found.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
    saves: list[tuple[list[str], list[str] | None]] = []

    def fake_relay(burner):
        class FakeRelay:
            ffmpeg = "ffmpeg"

            def burn_encoder(self):
                return burner

            def save(self, stream, name, *, subtitles, burn, **options):
                saves.append(([language for language, _ in subtitles], burn))
                saved = tmp_path / ("Show.mp4" if burn else "Show.mkv")
                saved.write_bytes(b"video")
                return saved

        return FakeRelay

    async def play(burner, burn_subtitles=True) -> RelayingPlayer:
        player = RelayingPlayer(Recorder(), title="Default app", relay=fake_relay(burner), headers=frozenset(),
                                hls=True, links=False, burn_subtitles=burn_subtitles)
        done = asyncio.get_running_loop().create_future()
        done.set_result([("en", str(subtitle)), ("fr", str(subtitle))])
        player.give_subtitles(done)
        await player.launch("https://site.test/a.m3u8", title="Show")
        return player

    burned = await play(["-c:v", "libx264"])
    assert burned.burned == "en" and burned.embedded == [] and saves[-1] == (["en", "fr"], ["-c:v", "libx264"])
    unable = await play(None)  # FFmpeg can't draw subtitles: they go inside instead
    assert unable.burned == "" and unable.embedded == ["en", "fr"] and saves[-1][1] is None
    off = await play(["-c:v", "libx264"], burn_subtitles=False)
    assert off.burned == "" and off.embedded == ["en", "fr"] and saves[-1][1] is None


async def test_without_ffmpeg_subtitles_go_beside_the_download(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    import cue.player.relay as relay_module

    saved = tmp_path / "Show.mp4"
    subtitle = tmp_path / "found.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")

    class FakeRelay:
        ffmpeg = None  # yt-dlp downloads it, as an MP4

        def save(self, stream, name, **options):
            saved.write_bytes(b"video")
            return saved

    beside: list[bool] = []

    class Opener(Recorder):
        async def launch(self, url, **options) -> None:
            beside.append(Path(url).with_suffix(".srt").is_file())  # there before the player opens the file
            await super().launch(url, **options)

    player = RelayingPlayer(Opener(), title="Default app", relay=FakeRelay, headers=frozenset(), hls=True, links=False)
    messages: list[str] = []
    player.observe("message", messages.append)
    found = asyncio.get_running_loop().create_future()
    player.give_subtitles(found)
    asyncio.get_running_loop().call_later(0.05, found.set_result, [("en", str(subtitle))])
    await player.launch("https://site.test/a.m3u8", title="Show")
    assert beside == [True] and player.sidecars == [tmp_path / "Show.srt"]
    assert "Finding subtitles…" in messages
    await player.quit()
    assert not saved.exists() and not (tmp_path / "Show.srt").exists()  # both go when you're done

    # Subtitles that don't come in time don't hold the episode back.
    monkeypatch.setattr(relay_module, "SUBTITLE_WAIT", 0.05)
    late = RelayingPlayer(Recorder(), title="Default app", relay=FakeRelay, headers=frozenset(), hls=True, links=False)
    late.give_subtitles(asyncio.get_running_loop().create_future())
    await late.launch("https://site.test/a.m3u8", title="Show")
    assert late.sidecars == [] and late._player.launches[0][0] == str(saved)


def test_subtitles_are_placed_as_srt_under_the_episodes_name(tmp_path: Path) -> None:
    video = tmp_path / "Show - Episode 1.mp4"
    video.write_bytes(b"video")
    srt = tmp_path / "en.srt"
    srt.write_bytes("1\n00:00:01,000 --> 00:00:02,000\nCafé\n".encode())
    vtt = tmp_path / "fr.vtt"
    vtt.write_text("WEBVTT\n\n00:01.000 --> 00:02.000\nBonjour\n", encoding="utf-8")
    ass = tmp_path / "ja.ass"
    ass.write_text("[Script Info]\n", encoding="utf-8")

    old = tmp_path / "de.srt"
    old.write_bytes("1\n00:00:01,000 --> 00:00:02,000\nGrüße\n".encode("cp1252"))

    placed = place_subtitles(video, [("en", str(srt)), ("fr", str(vtt)), ("ja", str(ass)), ("de", old)], ffmpeg=None)
    assert [(language, path.name) for language, path in placed] == [  # ASS needs FFmpeg
        ("en", "Show - Episode 1.srt"), ("fr", "Show - Episode 1.fr.srt"), ("de", "Show - Episode 1.de.srt")]
    texts = {language: path.read_text(encoding="utf-8-sig") for language, path in placed}
    assert all(path.read_bytes().startswith(b"\xef\xbb\xbf") for _, path in placed)  # UTF-8 Windows players recognise
    assert texts == {"en": "1\n00:00:01,000 --> 00:00:02,000\nCafé\n",
                     "fr": "1\n00:00:01,000 --> 00:00:02,000\nBonjour\n",
                     "de": "1\n00:00:01,000 --> 00:00:02,000\nGrüße\n"}  # an old Windows-1252 file, made UTF-8


@pytest.mark.skipif(find_ffmpeg() is None, reason="needs FFmpeg")
def test_ffmpeg_turns_other_subtitles_into_srt(tmp_path: Path) -> None:
    video = tmp_path / "Show.mp4"
    ass = tmp_path / "en.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, Bold, Italic, Alignment\n"
        "Style: Default,Arial,20,&H00FFFFFF,0,0,2\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,Hello there\n",
        encoding="utf-8",
    )
    [(_, placed)] = place_subtitles(video, [("en", str(ass))], ffmpeg=find_ffmpeg())
    text = placed.read_text(encoding="utf-8-sig")
    assert placed == tmp_path / "Show.srt" and "00:00:01,000 --> 00:00:02,500" in text and "Hello there" in text


def test_webvtt_becomes_srt() -> None:
    vtt = (
        "WEBVTT\nKind: captions\n\nNOTE made by hand\n\nSTYLE\n::cue { color: red }\n\n"
        "intro\n00:01.5 --> 00:03.250 align:start position:10%\n"
        "<v Narrator>Tom &amp; <i>Jerry</i></v>\n<c.yellow>run</c>\n\n"
        "01:02:03.004 --> 01:02:04.000\n<00:00:01.000>karaoke <B>loud</B>\n\n"
        "00:05.000 --> 00:06.000\n"  # nothing to show
    )
    assert vtt_to_srt(vtt) == (
        "1\n00:00:01,500 --> 00:00:03,250\nTom & <i>Jerry</i>\nrun\n\n"
        "2\n01:02:03,004 --> 01:02:04,000\nkaraoke <b>loud</b>\n"
    )
    assert vtt_to_srt("WEBVTT\n") == ""


# -- the "Another program" player -------------------------------------------------------------


def test_another_program_command_line() -> None:
    assert split_arguments('"{url}" /start {start_ms} --title "{title}"') == ["{url}", "/start", "{start_ms}", "--title", "{title}"]
    player = CommandPlayer("mpc-hc64.exe", {"arguments": '"{url}" /start {start_ms} /title "{title}" {other}'})
    args = player.arguments("http://127.0.0.1:1/h/x.m3u8", start=61.25, headers={}, title="My Show", extra_args=["--fs"])
    assert args == ["http://127.0.0.1:1/h/x.m3u8", "/start", "61250", "/title", "My Show", "{other}"]
    assert CommandPlayer("p.exe", {"arguments": "--fullscreen"}).arguments(
        "u", start=None, headers={}, title=None, extra_args=()) == ["--fullscreen", "u"]
    assert CommandPlayer("p.exe", {}).arguments("u", start=None, headers={}, title=None, extra_args=()) == ["u"]

    other = players()["other"]
    assert other.headers == frozenset() and not other.controls and other.program({}) is None
    assert players()["vlc"].headers == frozenset({"referer", "user-agent"}) and players()["mpv"].headers is None


async def test_play_wraps_players_that_need_help(media_dir: Path, store, tmp_path: Path, monkeypatch) -> None:
    from cue.app import CueApp
    from cue.config import Config
    from cue.player.mpv import MpvPlayer

    program = tmp_path / "player.exe"
    program.write_bytes(b"")
    monkeypatch.setattr("cue.player.registry.find_vlc", lambda: str(program))
    monkeypatch.setattr("cue.player.registry.find_mpv", lambda: str(program))
    made = {}
    for name, options in (("vlc", {}), ("mpv", {}), ("other", {"path": str(program), "hls": False})):
        app = CueApp(
            Config(providers={"local": {"path": str(media_dir)}}, player=name, players={name: options}),
            store=store, http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))),
            dropin_dir=tmp_path / "x", plugin_root=tmp_path / "y",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            monkeypatch.setattr(app, "push_screen", lambda screen, **kwargs: None)
            provider = app.providers["local"]
            show = (await provider.search("frieren"))[0]
            app.play("local", show, await provider.episodes(show), 0)
            made[name] = next(iter(app._sessions))._player_factory()
            await pilot.pause()
    assert isinstance(made["mpv"], MpvPlayer)  # sends any header and plays HLS: nothing to add
    assert isinstance(made["vlc"], RelayingPlayer) and made["vlc"].route("https://j.test/v", {"Authorization": "x"}) == "hls"
    assert isinstance(made["other"], RelayingPlayer) and isinstance(made["other"]._player, CommandPlayer)
    assert made["other"].route("https://site.test/a.m3u8", None) == "continuous"  # hls = false in its settings


def test_links_carry_extensions_ffmpeg_accepts(upstream) -> None:
    # FFmpeg refuses HLS pieces without a media extension; some hosts disguise theirs as images.
    relay, _ = upstream
    stream = relay.share(STREAM).stream_id
    names = {
        "https://cdn.test/1080/seg_00000.ts.jpg": ".ts",
        "https://cdn.test/v/chunk-7.m4s": ".m4s",
        "https://cdn.test/v/init.MP4": ".mp4",
        "https://cdn.test/keys/key.bin": ".ts",
        "https://cdn.test/v/segment": ".ts",
        "https://cdn.test/subs/part1.vtt": ".vtt",
    }
    for url, suffix in names.items():
        assert relay.link(url, "segment", stream).endswith(suffix), url
    assert relay.link("https://cdn.test/v/index", "playlist", stream).endswith(".m3u8")
    assert relay.link("https://cdn.test/video.mp4?sig=1", "root", stream).endswith(".mp4")


def test_playback_settings() -> None:
    from cue.config import ConfigError, parse_config

    playback = parse_config({"playback": {"folder": "D:/Cue temp", "quality": 720, "keep_hours": 1.5, "max_mb": 0}}).playback
    assert (playback.folder, playback.quality, playback.keep_hours, playback.max_mb) == ("D:/Cue temp", "720", 1.5, 0)
    defaults = parse_config({}).playback
    assert (defaults.folder, defaults.quality, defaults.keep_hours, defaults.max_mb) == ("", "", 0.0, 4096)
    with pytest.raises(ConfigError, match="can't be negative"):
        parse_config({"playback": {"keep_hours": -1}})
    no_sizes = MASTER.replace("RESOLUTION=854x480,", "").replace("RESOLUTION=1920x1080,", "")
    assert "video/480.m3u8" in hls.single_variant(no_sizes, 1)  # "worst" still picks the smallest
