"""Provider subtitle metadata is explicit, scoped, and separate from video URLs."""

from __future__ import annotations

import httpx
import pytest

from cue.providers.anime_resolvers import resolve_server, streams_from_payload
from cue.providers.base import ProviderError, Stream, StreamSubtitle


EMBED = "https://zokoanime.video/stream/mal/100/1/sub"
HEADERS = {"Referer": EMBED, "User-Agent": "Cue-test"}


def test_stream_positional_compatibility_and_track_model() -> None:
    legacy = Stream("https://media.test/video.mp4", {}, ["https://sub.test/en.srt"], "720p")
    assert legacy.quality == "720p"
    assert legacy.subtitle_tracks is None
    assert StreamSubtitle("https://sub.test/en.srt").language == ""


async def test_zoko_published_envelope_keeps_subtitles_but_not_sprite_track() -> None:
    # Public v23 representation of src, subtitles:[{src,lang,label,default}], sprite_vtt.
    envelope = (
        "FFYSGRYPX09NCAFJH1BAGQAYAUgXQw9WERVUHU0HFAkBRBEBBxZGFy1KTQcTCFcXRwUWERRe"
        "TB5AFwAbAUQKAxFLEEgFRUARD0UDWRFPTkcITBhWTU5DDhsPSU8OBAZIGhNVViQFEkEMHgpH"
        "SA8SVAkVFAcBD18ZEBABUCsdTQcRGRxZADIUERAPTBMHABUbBhdKQgEEFFkfXgEHTx8QXhFC"
        "Fg0RQBRCQQIVH1dQ"
    )
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=f'<script>window.__P="{envelope}";</script>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        streams = await resolve_server(client, EMBED, referer="https://catalog.test/", user_agent="Cue-test")
    assert len(requests) == 1  # Metadata parsing does not fetch subtitle bodies.
    assert len(streams) == 1
    assert streams[0].subtitle_tracks == [StreamSubtitle(
        "https://captions.test/en.vtt", "en", "English",
        {"Referer": "https://zokoanime.video/", "User-Agent": "Cue-test"},
    )]


def test_container_tracks_inherit_and_source_tracks_stay_with_their_stream() -> None:
    streams = streams_from_payload({
        "subtitles": [{"src": "/subs/en.vtt", "lang": "en", "label": "English"}],
        "sources": [
            {"src": "/first.mp4", "tracks": [{"file": "/subs/fr.srt", "kind": "captions", "srclang": "fr", "label": "French"}]},
            {"src": "/second.mp4"},
        ],
    }, EMBED, HEADERS)
    assert [track.language for track in streams[0].subtitle_tracks] == ["en", "fr"]
    assert [track.language for track in streams[1].subtitle_tracks] == ["en"]
    assert streams[0].subtitle_tracks[0].url == "https://zokoanime.video/subs/en.vtt"
    assert streams[0].subtitle_tracks[0].headers == HEADERS


def test_sub_dub_tracks_do_not_bleed_between_language_containers() -> None:
    payload = {
        "sub": {"src": "/sub.mp4", "subtitles": [{"src": "/sub.vtt", "label": "English"}]},
        "dub": {"src": "/dub.mp4", "subtitles": [{"src": "/signs.vtt", "label": "Signs"}]},
    }
    streams = streams_from_payload(payload, EMBED, HEADERS, language="dub")
    assert len(streams) == 1
    assert [track.label for track in streams[0].subtitle_tracks] == ["Signs"]


@pytest.mark.parametrize("key", ["subtitles", "captions", "tracks"])
def test_common_json_subtitle_containers(key: str) -> None:
    stream = streams_from_payload({"src": "/video.mp4", key: [{"url": "./en.srt", "language": "en", "name": "English"}]}, EMBED, {})[0]
    assert stream.subtitle_tracks == [StreamSubtitle("https://zokoanime.video/stream/mal/100/1/en.srt", "en", "English")]


@pytest.mark.parametrize("row", [
    {"src": "/subtitle?id=1", "kind": "subtitles"},
    {"file": "/subtitle?id=1", "kind": "captions"},
    {"url": "/subtitle?id=1", "type": "text/vtt"},
])
def test_explicit_extensionless_text_tracks_are_supported(row: dict) -> None:
    stream = streams_from_payload({"src": "/video.mp4", "tracks": [row]}, EMBED, {})[0]
    assert stream.subtitle_tracks[0].url == "https://zokoanime.video/subtitle?id=1"


def test_plain_legacy_urls_in_subtitles_metadata_are_retained() -> None:
    stream = streams_from_payload({"src": "/video.mp4", "subtitles": ["/en.srt"]}, EMBED, {})[0]
    assert stream.subtitle_tracks == [StreamSubtitle("https://zokoanime.video/en.srt")]


@pytest.mark.parametrize("row", [
    {"src": "/thumbs.vtt", "kind": "thumbnails"},
    {"src": "/chapters.vtt", "kind": "chapters"},
    {"src": "/metadata.vtt", "kind": "metadata"},
    {"src": "/other.mp4", "kind": "subtitles"},
    {"src": "/poster.jpg", "kind": "captions"},
    {"src": "/not-subs", "type": "text/html"},
    {"src": "javascript:alert(1)"},
    {"src": "file:///local.srt"},
    {"src": "https://user:password@captions.test/en.vtt"},
    {"src": "https://[broken/en.vtt"},
    {"src": "https://captions.test:invalid/en.vtt"},
    {"src": "/en.vtt\r\nX: invalid"},
])
def test_non_subtitle_or_unsafe_rows_are_ignored(row: dict) -> None:
    streams = streams_from_payload({"src": "/video.mp4", "subtitles": [row]}, EMBED, {})
    assert len(streams) == 1
    assert streams[0].subtitle_tracks is None


def test_ambiguous_tracks_do_not_become_subtitles_or_video() -> None:
    streams = streams_from_payload({"src": "/video.mp4", "tracks": [{"src": "/unknown"}, {"src": "/wrong.mp4"}]}, EMBED, {})
    assert len(streams) == 1
    assert streams[0].subtitle_tracks is None
    assert streams_from_payload({"subtitles": [{"src": "/wrong.mp4"}]}, EMBED, {}) == []


def test_subtitle_headers_do_not_inherit_media_credentials() -> None:
    headers = {**HEADERS, "Cookie": "media_session=secret", "Authorization": "Bearer media", "X-Media": "secret"}
    stream = streams_from_payload({
        "src": "https://video.test/video.mp4",
        "subtitles": [{"src": "https://captions.test/en.vtt", "headers": {"X-Subtitle": "token"}}],
    }, EMBED, headers, referrer_policy=True)[0]
    assert stream.subtitle_tracks[0].headers == {
        "Referer": "https://zokoanime.video/", "User-Agent": "Cue-test", "X-Subtitle": "token",
    }
    assert stream.headers["Authorization"] == "Bearer media"
    assert headers["Referer"] == EMBED


def test_subtitle_explicit_headers_override_case_insensitively() -> None:
    stream = streams_from_payload({
        "src": "/video.mp4", "subtitles": [{"src": "https://captions.test/en.vtt", "headers": {
            "referer": "https://custom.test/page", "user-agent": "Subtitle-Agent", "Authorization": "Bearer subtitle",
        }}],
    }, EMBED, HEADERS, referrer_policy=True)[0]
    assert stream.subtitle_tracks[0].headers == {
        "referer": "https://custom.test/page", "user-agent": "Subtitle-Agent", "Authorization": "Bearer subtitle",
    }


def test_subtitle_referrer_uses_own_destination_and_drops_on_downgrade() -> None:
    stream = streams_from_payload({"src": "https://video.test/video.mp4", "subtitles": [
        {"src": "/same.vtt"}, {"src": "https://captions.test/other.vtt"}, {"src": "http://captions.test/plain.vtt"},
    ]}, EMBED, HEADERS, referrer_policy=True)[0]
    tracks = stream.subtitle_tracks
    assert tracks[0].headers["Referer"] == EMBED
    assert tracks[1].headers["Referer"] == "https://zokoanime.video/"
    assert "Referer" not in tracks[2].headers


def test_invalid_subtitle_headers_are_rejected() -> None:
    with pytest.raises(ProviderError, match="header"):
        streams_from_payload({"src": "/video.mp4", "subtitles": [{"src": "/en.vtt", "headers": {"X-Test": "a\r\nb"}}]}, EMBED, {})


def test_duplicate_media_merges_tracks_without_duplicates() -> None:
    streams = streams_from_payload({"sources": [
        {"src": "/video.mp4", "subtitles": ["/en.vtt", "/en.vtt"]},
        {"src": "/video.mp4", "subtitles": ["/en.vtt", "/fr.vtt"]},
    ]}, EMBED, {})
    assert len(streams) == 1
    assert [track.url.rsplit("/", 1)[-1] for track in streams[0].subtitle_tracks] == ["en.vtt", "fr.vtt"]


async def test_html_video_track_scope_and_kinds() -> None:
    page = '''<track src="/outside.vtt" kind="subtitles">
      <video><source src="/one.mp4"><track src="/en.vtt" srclang="en" label="English">
      <track src="/thumbs.vtt" kind="metadata"><track src="/chapters.vtt" kind="chapters"></video>
      <video src="/two.mp4"><track src="/fr.vtt" kind="captions" srclang="fr" label="French"></video>'''
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=page))) as client:
        streams = await resolve_server(client, "https://megaplay.buzz/embed", referer="https://catalog.test/", user_agent="Cue-test")
    assert [stream.url for stream in streams] == ["https://megaplay.buzz/one.mp4", "https://megaplay.buzz/two.mp4"]
    assert [[track.language for track in stream.subtitle_tracks] for stream in streams] == [["en"], ["fr"]]


async def test_json_script_subtitles_attach_to_sources() -> None:
    page = '<script type="application/json">{"sources":[{"file":"/video.mp4"}],"tracks":[{"file":"/en.vtt","kind":"captions","label":"English"}]}</script>'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=page))) as client:
        streams = await resolve_server(client, "https://megaplay.buzz/embed", referer="https://catalog.test/", user_agent="Cue-test")
    assert streams[0].subtitle_tracks[0].label == "English"
