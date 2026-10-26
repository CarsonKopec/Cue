from __future__ import annotations

import asyncio
import gzip
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cue.providers.base import Stream
from cue.subtitles.base import SubtitleError, SubtitleTrack
from cue.subtitles.provider import ProviderSubtitles, language_code

SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello <b>world</b>!\n"
VTT = b"WEBVTT\n\n00:01.000 --> 00:02.000\nHello world!\n"
ASS = b"[Script Info]\nScriptType: v4.00+\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello world!\n"


def stream(*, url="https://subs.test/english.vtt", language="en", label="English", headers=None):
    # This also exercises callers implementing the stream contract without a
    # particular dataclass constructor (legacy providers remain compatible).
    return SimpleNamespace(
        url="https://video.test/ep.m3u8", headers={"Referer": "https://player.test/ep"}, subtitles=None,
        subtitle_tracks=[SimpleNamespace(url=url, language=language, label=label, headers=headers)],
    )


@pytest.mark.parametrize(("value", "expected"), [
    ("English (SDH)", "en"), ("eng", "en"), ("en-US", "en-us"),
    ("Portuguese Brazilian", "pt-br"), ("Spanish LAT", "es-419"), ("Arabic", "ar"),
    ("French", "fr"), ("ja_JP", "ja-jp"), ("German", "de"), ("", ""),
    (None, ""), ("unknown language", ""), ("und", ""), ("CC", ""),
    ("Spanish ESP", "es-es"), ("pt_BR", "pt-br"), ("es-419", "es-419"),
    ("en-GB", "en-gb"), ("Brazilian Portuguese", "pt-br"),
])
def test_language_codes(value, expected):
    assert language_code(value) == expected


async def test_choices_normalize_real_labels_and_register_headers(tmp_path):
    async with httpx.AsyncClient() as client:
        source = ProviderSubtitles(client, tmp_path)
        tracks = source.choices(stream(language="en", label="Arabic"))
        assert tracks[0].language == "ar"
        assert tracks[0].source == "provider"
        assert tracks[0].label == "Arabic"
        assert tracks[0].url == "https://subs.test/english.vtt"
        assert len(tracks[0].id) == 64


@pytest.mark.parametrize(("label", "language", "expected"), [
    ("English", "en-GB", "en-gb"), ("CC", "en", "en"),
    ("Spanish LAT", "en", "es-419"), ("Portuguese Brazilian", "en", "pt-br"),
])
async def test_choices_preserve_regions_and_ignore_nonspecific_labels(tmp_path, label, language, expected):
    async with httpx.AsyncClient() as client:
        source = ProviderSubtitles(client, tmp_path)
        assert source.choices(stream(label=label, language=language))[0].language == expected


@pytest.mark.parametrize("headers", [{"Referer": "https://player.test/\r\nsecret: injected"}, {"invalid name": "value"}, {"Referer": None}])
async def test_choices_skip_malformed_required_headers(tmp_path, headers):
    async with httpx.AsyncClient() as client:
        assert ProviderSubtitles(client, tmp_path).choices(stream(headers=headers)) == []


async def test_legacy_stream_choices_deduplicate_and_use_filename_hint(tmp_path):
    async with httpx.AsyncClient() as client:
        source = ProviderSubtitles(client, tmp_path)
        legacy = Stream("https://video.test/main.m3u8", subtitles=["https://subs.test/show.eng.srt", "https://subs.test/show.eng.srt", "file:///private/file.srt"])
        tracks = source.choices(legacy)
        assert len(tracks) == 1
        assert tracks[0].language == "en"
        assert source.choices(Stream("file.mkv")) == []


@pytest.mark.parametrize("url", ["file:///a.srt", "ftp://host/a.srt", "javascript:alert(1)", "https://u:p@host/a.srt", "https://host:invalid/a.srt", "//subs.test/a.srt", "https://"])
async def test_choices_skip_invalid_urls(tmp_path, url):
    async with httpx.AsyncClient() as client:
        assert ProviderSubtitles(client, tmp_path).choices(stream(url=url)) == []


@pytest.mark.parametrize(("data", "extension"), [(SRT, ".srt"), (VTT, ".vtt"), (ASS, ".ass"), (SRT.decode().encode("utf-16"), ".srt"), (b"\xef\xbb\xbf" + VTT, ".vtt")])
async def test_download_detects_format_and_uses_safe_local_filename(tmp_path, data, extension):
    requests = []
    def handle(request):
        requests.append(request)
        assert request.extensions["timeout"]["read"] == 15
        return httpx.Response(200, content=data, headers={"content-type": "application/octet-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = ProviderSubtitles(client, tmp_path)
        track = source.choices(stream(url="https://subs.test/../../bad.exe?token=signed"))[0]
        path = Path(await source.fetch(track))
        assert path.parent == tmp_path
        assert path.name == track.id + extension
        assert path.read_text(encoding="utf-8").endswith("\n")
        assert await source.fetch(track) == str(path)
        assert len(requests) == 1
        assert list(path.parent.glob("*.tmp")) == []


async def test_download_uses_only_explicit_track_headers_not_shared_credentials(tmp_path):
    seen = []
    def handle(request):
        seen.append(request)
        assert request.headers["Referer"] == "https://player.test/ep"
        assert request.headers["User-Agent"] == "Stream UA"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert "x-client-secret" not in request.headers
        return httpx.Response(200, content=VTT)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), auth=("private", "password"), headers={"X-Client-Secret": "secret"}, cookies={"session": "secret"}) as client:
        source = ProviderSubtitles(client, tmp_path)
        entry = stream(headers={"Referer": "https://player.test/ep", "User-Agent": "Stream UA"})
        await source.fetch(source.choices(entry)[0])
    assert len(seen) == 1


async def test_legacy_tracks_do_not_inherit_video_authorization_or_cookies(tmp_path):
    def handle(request):
        assert request.headers["Referer"] == "https://player.test/ep"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, content=SRT)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = ProviderSubtitles(client, tmp_path)
        legacy = Stream("https://video.test/main", headers={"Referer": "https://player.test/ep", "Authorization": "secret", "Cookie": "secret"}, subtitles=["https://subs.test/en.srt"])
        await source.fetch(source.choices(legacy)[0])


async def test_cache_identity_includes_required_headers(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=SRT))) as client:
        source = ProviderSubtitles(client, tmp_path)
        one = source.choices(stream(headers={"Referer": "https://player.test/one"}))[0]
        two = source.choices(stream(headers={"Referer": "https://player.test/two"}))[0]
        assert one.id != two.id
        assert await source.fetch(one) != await source.fetch(two)


async def test_parallel_downloads_share_one_cached_file(tmp_path):
    count = 0
    async def handle(request):
        nonlocal count
        count += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, content=VTT)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = ProviderSubtitles(client, tmp_path)
        track = source.choices(stream())[0]
        paths = await asyncio.gather(source.fetch(track), source.fetch(track))
        assert paths[0] == paths[1] and count == 1


async def test_invalid_cache_is_downloaded_again(tmp_path):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=VTT))) as client:
        source = ProviderSubtitles(client, tmp_path)
        track = source.choices(stream())[0]
        bad = tmp_path / f"{track.id}.vtt"
        bad.parent.mkdir(exist_ok=True)
        bad.write_text("<html>expired</html>")
        assert await source.fetch(track) == str(bad)
        assert bad.read_bytes() == VTT


@pytest.mark.parametrize("data", [b"", b"<html>Challenge</html>", b'{"error":"not found"}', b"not subtitles", b"WEBVTT\n", b"\x00\x00" + SRT, b"\xff\xff\xfe\x00", b"<!DOCTYPE html>\n" + SRT])
async def test_reject_invalid_subtitle_content(tmp_path, data):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=data))) as client:
        source = ProviderSubtitles(client, tmp_path)
        with pytest.raises(SubtitleError):
            await source.fetch(source.choices(stream())[0])
        assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", [401, 403, 404, 410, 429, 500, 204, 206])
async def test_access_or_server_errors_are_not_retried(tmp_path, status):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(status, content=SRT)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = ProviderSubtitles(client, tmp_path)
        with pytest.raises(SubtitleError, match=str(status)):
            await source.fetch(source.choices(stream())[0])
    assert len(requests) == 1


@pytest.mark.parametrize("content_type", ["text/html", "application/json", "application/xhtml+xml; charset=UTF-8"])
async def test_error_content_types_are_rejected_even_with_timing_text(tmp_path, content_type):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=SRT, headers={"content-type": content_type}))) as client:
        source = ProviderSubtitles(client, tmp_path)
        with pytest.raises(SubtitleError):
            await source.fetch(source.choices(stream())[0])


async def test_same_origin_redirects_keep_explicit_headers_but_not_response_cookies(tmp_path):
    calls = []
    def handle(request):
        calls.append(str(request.url))
        assert request.headers["Referer"] == "https://player.test/ep"
        assert "cookie" not in request.headers
        if request.url.path == "/english.vtt":
            return httpx.Response(302, headers={"Location": "/actual.srt", "Set-Cookie": "private=secret"})
        return httpx.Response(200, content=SRT)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        source = ProviderSubtitles(client, tmp_path)
        assert (await source.fetch(source.choices(stream())[0])).endswith(".srt")
        assert len(calls) == 2


@pytest.mark.parametrize("destination", ["https://other.test/file.srt", "http://subs.test/file.srt", "https://subs.test:444/file.srt", "file:///a.srt", "https://subs.test:invalid/file.srt"])
async def test_cross_origin_and_invalid_redirects_are_not_followed(tmp_path, destination):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": destination})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = ProviderSubtitles(client, tmp_path)
        with pytest.raises(SubtitleError):
            await source.fetch(source.choices(stream())[0])
    assert len(calls) == 1


async def test_redirect_loops_are_bounded(tmp_path):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "/english.vtt"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = ProviderSubtitles(client, tmp_path)
        with pytest.raises(SubtitleError, match="too many"):
            await source.fetch(source.choices(stream())[0])
    assert len(calls) == 4


@pytest.mark.parametrize("kind", ["declared", "actual", "compressed"])
async def test_size_limit_checks_declared_and_expanded_download(tmp_path, monkeypatch, kind):
    monkeypatch.setattr("cue.subtitles.provider.MAX_SUBTITLE_BYTES", 100)
    def handle(request):
        if kind == "declared":
            return httpx.Response(200, content=SRT, headers={"content-length": "101"})
        if kind == "compressed":
            return httpx.Response(200, content=gzip.compress(SRT * 10), headers={"content-encoding": "gzip"})
        return httpx.Response(200, content=SRT * 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = ProviderSubtitles(client, tmp_path)
        with pytest.raises(SubtitleError, match="too large"):
            await source.fetch(source.choices(stream())[0])


async def test_network_errors_are_reported_without_signed_url(tmp_path):
    def handle(request):
        raise httpx.ReadTimeout(f"secret url {request.url}")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = ProviderSubtitles(client, tmp_path)
        with pytest.raises(SubtitleError) as caught:
            await source.fetch(source.choices(stream(url="https://subs.test/a.vtt?token=secret"))[0])
        assert "secret" not in str(caught.value)


async def test_hls_subtitle_playlist_loads_its_single_webvtt_file(tmp_path):
    # Observed VixSrc shape: one full-length WebVTT file on the stream's CDN host.
    playlist = (
        b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:3472\n"
        b"#EXTINF:3471.917,\nhttps://cdn.test/hls/subtitle/3-eng/subs-0000.vtt?token=signed\n#EXT-X-ENDLIST\n"
    )
    calls = []
    def handle(request):
        calls.append(request)
        assert request.headers["Referer"] == "https://player.test/ep"
        if request.url.host == "subs.test":
            return httpx.Response(200, content=playlist, headers={"content-type": "application/vnd.apple.mpegurl"})
        return httpx.Response(200, content=VTT, headers={"content-type": "application/octet-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = ProviderSubtitles(client, tmp_path)
        track = source.choices(stream(url="https://subs.test/playlist/1?type=subtitle&rendition=eng"))[0]
        path = Path(await source.fetch(track))
    assert path.suffix == ".vtt" and path.read_bytes() == VTT
    assert [str(r.url) for r in calls] == [
        "https://subs.test/playlist/1?type=subtitle&rendition=eng",
        "https://cdn.test/hls/subtitle/3-eng/subs-0000.vtt?token=signed",
    ]


@pytest.mark.parametrize("playlist", [
    b"#EXTM3U\n#EXTINF:10,\na.vtt\n#EXTINF:10,\nb.vtt\n#EXT-X-ENDLIST\n",  # segmented
    b"#EXTM3U\n#EXTINF:10,\na.vtt\n",  # live, still growing
    b"#EXTM3U\n#EXT-X-ENDLIST\n",  # empty
    b"#EXTM3U\n#EXTINF:10,\njavascript:alert(1)\n#EXT-X-ENDLIST\n",
])
async def test_unloadable_hls_subtitle_playlists_are_reported(tmp_path, playlist):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=playlist)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = ProviderSubtitles(client, tmp_path)
        with pytest.raises(SubtitleError):
            await source.fetch(source.choices(stream(url="https://subs.test/playlist/1"))[0])
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == []


async def test_unregistered_or_modified_tracks_cannot_choose_cache_path(tmp_path):
    async with httpx.AsyncClient() as client:
        source = ProviderSubtitles(client, tmp_path)
        with pytest.raises(SubtitleError):
            await source.fetch(SubtitleTrack("en", "bad", url="https://subs.test/en.srt", source="provider", id="../../bad"))
        track = source.choices(stream())[0]
        track.url = "https://other.test/en.srt"
        with pytest.raises(SubtitleError):
            await source.fetch(track)

