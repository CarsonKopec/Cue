from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from cue.player.base import PlayerError
from cue.player.session import PlaybackSession
from cue.providers.base import Episode, Show, Stream, StreamSubtitle
from cue.subtitles import SubtitleError, SubtitleTrack
from cue.subtitles.provider import ProviderSubtitles

VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nHello from the server\n"


class Player:
    def __init__(self):
        self.commands = []
        self.observers = {}
        self.running = True
        self.reject = False

    def observe(self, name, callback):
        self.observers[name] = callback

    def on_close(self, callback):
        self.closed = callback

    async def launch(self, url, **kwargs):
        self.observers["duration"](20)
        self.observers["time-pos"](1)

    async def command(self, *command):
        if self.reject:
            raise PlayerError("subtitle rejected")
        self.commands.append(command)

    async def quit(self):
        self.running = False

    def kill(self):
        self.running = False


class Provider:
    name = "anime"
    language = "sub"

    async def stream(self, episode):
        return [Stream("https://media.test/video.m3u8", subtitle_tracks=[
            StreamSubtitle(f"https://subs.test/{episode.label}.vtt", "en", "English"),
        ])]


class ExtraSubtitles:
    def __init__(self, path, *, fails=False):
        self.path = path
        self.calls = 0
        self.fails = fails

    async def ensure(self, *args, **kwargs):
        self.calls += 1
        return [("en", str(self.path))]

    async def search(self, *args, **kwargs):
        if self.fails:
            raise SubtitleError("service unavailable")
        return [SubtitleTrack("en", "Additional result", source="opensubtitles", id="1")]

    async def choose(self, *args):
        return str(self.path)


def session_for(store, downloader, stream, **options):
    player = Player()
    session = PlaybackSession(
        provider_name="anime", provider=Provider(), show=Show("s", "Show"),
        queue=[Episode("s", 1)], store=store, player_factory=lambda: player,
        provider_subtitles=downloader, **options,
    )
    session._player = player
    session._stream = stream
    session._provider_tracks = downloader.choices(stream)
    session._generation = 1
    session.state.episode = Episode("s", 1)
    session._loaded.set()
    return session, player


async def load(session):
    await session._load_subtitles(session._generation, session.state.episode, session._stream)


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


async def test_server_subtitles_load_without_api_key_and_follow_episode_queue(tmp_path, store):
    requested, players = [], []

    def respond(request):
        requested.append(request.url.path)
        return httpx.Response(200, text=VTT)

    def factory():
        player = Player()
        players.append(player)
        return player

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        session = PlaybackSession(
            provider_name="anime", provider=Provider(), show=Show("s", "Show"),
            queue=[Episode("s", 1), Episode("s", 2)], store=store, player_factory=factory,
            provider_subtitles=ProviderSubtitles(client, tmp_path),
        )
        task = asyncio.create_task(session.run())
        try:
            await wait_for(lambda: len(players) == 1 and players[0].commands)
            players[0].observers["eof-reached"](True)
            await wait_for(lambda: len(players) == 2 and players[1].commands)
            players[1].observers["eof-reached"](True)
            await asyncio.wait_for(task, 5)
        finally:
            session.stop()
            await asyncio.wait_for(task, 5)
    assert requested == ["/1.vtt", "/2.vtt"]
    assert all(player.commands[0][0] == "sub-add" for player in players)
    assert all(player.commands[0][2] == "select" for player in players)
    assert all(Path(player.commands[0][1]).read_text(encoding="utf-8").startswith("WEBVTT") for player in players)
    assert players[0].commands[0][1] != players[1].commands[0][1]
    assert session.state.error is None


async def test_preferred_track_selected_before_other_language_and_no_optional_search(tmp_path, store):
    requested = []

    def respond(request):
        requested.append(request.url.path)
        return httpx.Response(200, text=VTT)

    stream = Stream("https://media.test/video.m3u8", subtitle_tracks=[
        StreamSubtitle("https://subs.test/ar.vtt", "en", "Arabic"),
        StreamSubtitle("https://subs.test/en.vtt", "en", "English"),
    ])
    extra = ExtraSubtitles(tmp_path / "extra.srt")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        session, player = session_for(store, ProviderSubtitles(client, tmp_path), stream, subtitles=extra)
        await load(session)
    assert requested == ["/en.vtt"]
    assert len(player.commands) == 1 and player.commands[0][2] == "select"
    assert extra.calls == 0
    assert session.state.subtitles == "en"


async def test_manual_picker_loads_other_language_with_auto_loading_off(tmp_path, store):
    stream = Stream("https://media.test/video.m3u8", subtitle_tracks=[
        StreamSubtitle("https://subs.test/fr.vtt", "fr", "French"),
    ])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=VTT))) as client:
        session, player = session_for(store, ProviderSubtitles(client, tmp_path), stream, auto_subtitles=False)
        await load(session)
        assert player.commands == []
        tracks = await session.subtitle_choices()
        await session.use_subtitle(tracks[0])
    assert player.commands[0][0::2] == ("sub-add", "select")
    assert session.state.subtitles == "fr: French"


async def test_unavailable_preference_is_not_replaced_with_wrong_language(tmp_path, store):
    stream = Stream("https://media.test/video.m3u8", subtitle_tracks=[
        StreamSubtitle("https://subs.test/fr.vtt", "fr", "French"),
    ])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No matching track should download"))) as client:
        session, player = session_for(store, ProviderSubtitles(client, tmp_path), stream)
        await load(session)
    assert not player.commands
    assert "preferred language unavailable" in session.state.subtitles


async def test_restricted_subtitle_falls_back_to_optional_service(tmp_path, store):
    path = tmp_path / "extra.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:05,000\nFallback\n")
    stream = Stream("https://media.test/video.m3u8", subtitle_tracks=[
        StreamSubtitle("https://subs.test/en.vtt", "en", "English"),
    ])
    extra = ExtraSubtitles(path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403))) as client:
        session, player = session_for(store, ProviderSubtitles(client, tmp_path), stream, subtitles=extra)
        await load(session)
    assert player.commands == [("sub-add", str(path), "select")]
    assert extra.calls == 1 and session.state.error is None


async def test_picker_retains_server_tracks_if_optional_search_fails(tmp_path, store):
    async with httpx.AsyncClient() as client:
        session, _ = session_for(store, ProviderSubtitles(client, tmp_path),
            Stream("https://media.test/video.m3u8", subtitles=["https://subs.test/en.vtt"]),
            subtitles=ExtraSubtitles(tmp_path / "extra.srt", fails=True))
        assert len(await session.subtitle_choices()) == 1


async def test_old_picker_cannot_add_subtitles_to_new_episode(tmp_path, store):
    async with httpx.AsyncClient() as client:
        session, player = session_for(store, ProviderSubtitles(client, tmp_path),
            Stream("https://media.test/video.m3u8", subtitles=["https://subs.test/en.vtt"]))
        tracks = await session.subtitle_choices()
        session._generation += 1
        with pytest.raises(SubtitleError, match="Episode changed"):
            await session.use_subtitle(tracks[0])
        assert not player.commands


async def test_late_download_cannot_override_manual_selection_or_new_episode(tmp_path, store):
    started, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        started.set()
        await release.wait()
        return httpx.Response(200, text=VTT)

    stream = Stream("https://media.test/video.m3u8", subtitle_tracks=[
        StreamSubtitle("https://subs.test/en.vtt", "en", "English"),
    ])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        session, player = session_for(store, ProviderSubtitles(client, tmp_path), stream)
        task = asyncio.create_task(load(session))
        await started.wait()
        session._subtitle_revision += 1
        release.set()
        await task
        assert not player.commands


async def test_player_subtitle_failure_is_reported_without_stopping_video(tmp_path, store):
    stream = Stream("https://media.test/video.m3u8", subtitle_tracks=[
        StreamSubtitle("https://subs.test/en.vtt", "en", "English"),
    ])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=VTT))) as client:
        session, player = session_for(store, ProviderSubtitles(client, tmp_path), stream)
        player.reject = True
        await load(session)
        assert "could not load" in session.state.subtitles
        assert session.state.error is None


@pytest.mark.parametrize("track,preference,matches", [
    ("en-US", "en", True), ("en", "en-US", True), ("en-GB", "en-US", False),
    ("fr", "en", False), ("eng", "en", True),
])
def test_language_matching_respects_region_when_requested(track, preference, matches):
    assert PlaybackSession._language_matches(track, preference) is matches
