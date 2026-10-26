from __future__ import annotations

import asyncio

import httpx
from textual.widgets import OptionList, Static

from cue.app import CueApp
from cue.config import Config, SubtitleConfig
from cue.player.session import PlaybackSession
from cue.providers.base import Episode, Show, Stream, StreamSubtitle
from cue.screens.player import NowPlayingScreen, SubtitlePicker
from cue.subtitles.provider import ProviderSubtitles


VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nTest subtitle\n"


class TestPlayer:
    __test__ = False

    def __init__(self):
        self.observers = {}
        self.commands = []

    def observe(self, name, callback):
        self.observers[name] = callback

    def on_close(self, callback):
        pass

    async def launch(self, url, **kwargs):
        self.observers["duration"](20)
        self.observers["time-pos"](1)

    async def command(self, *command):
        self.commands.append(command)

    async def quit(self):
        pass

    def kill(self):
        pass


class TestProvider:
    __test__ = False
    name = "test"
    language = "sub"

    async def stream(self, episode):
        return [Stream("https://media.test/video.mp4", subtitle_tracks=[
            StreamSubtitle(f"https://subs.test/{episode.label}/en.vtt", "en", "English"),
            StreamSubtitle(f"https://subs.test/{episode.label}/fr.vtt", "fr", "French"),
        ])]


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


async def test_shift_c_picker_works_without_api_key_and_displays_audio_mode(tmp_path, store, monkeypatch):
    player = TestPlayer()
    monkeypatch.setattr("cue.app.create_providers", lambda *args: ({"test": TestProvider()}, {}))
    app = CueApp(
        Config(subtitles=SubtitleConfig(auto_download=False)), store=store,
        player_factory=lambda: player, plugin_dir=tmp_path,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=VTT))),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        app.play("test", Show("show", "Show"), [Episode("show", 1)], 0)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, NowPlayingScreen)
        assert "[SUB]" in str(screen.query_one("#np-episode", Static).render())
        await pilot.press("shift+c")
        await pilot.pause()
        assert isinstance(app.screen, SubtitlePicker)
        assert app.screen.query_one(OptionList).option_count == 2
        await pilot.press("down", "enter")
        await wait_for(lambda: bool(player.commands))
        assert app.screen is screen
        assert screen.session.state.subtitles == "fr: French"
        assert player.commands[-1][0] == "sub-add" and player.commands[-1][2] == "select"
        screen.session.stop()
        await app.workers.wait_for_complete()


async def test_picker_left_open_across_next_episode_cannot_apply_old_track(tmp_path, store, monkeypatch):
    players = []

    def factory():
        player = TestPlayer()
        players.append(player)
        return player

    monkeypatch.setattr("cue.app.create_providers", lambda *args: ({"test": TestProvider()}, {}))
    app = CueApp(
        Config(subtitles=SubtitleConfig(auto_download=False)), store=store,
        player_factory=factory, plugin_dir=tmp_path,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=VTT))),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        app.play("test", Show("show", "Show"), [Episode("show", 1), Episode("show", 2)], 0)
        await pilot.pause()
        screen = app.screen
        await pilot.press("shift+c")
        await pilot.pause()
        assert isinstance(app.screen, SubtitlePicker)
        screen.session.next()
        await wait_for(lambda: len(players) == 2)
        await pilot.press("enter")
        await pilot.pause()
        assert not players[1].commands
        assert any("Episode changed" in str(notification.message) for notification in app._notifications)
        screen.session.stop()
        await app.workers.wait_for_complete()


async def test_inflight_automatic_subtitle_command_cannot_override_manual_choice(tmp_path, store):
    automatic_started, release_automatic, manual_fetched = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class DelayedPlayer(TestPlayer):
        async def command(self, *command):
            if not automatic_started.is_set():
                automatic_started.set()
                await release_automatic.wait()
            self.commands.append(command)

    async def respond(request):
        if request.url.path.endswith("fr.vtt"):
            manual_fetched.set()
        return httpx.Response(200, text=VTT)

    player = DelayedPlayer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = ProviderSubtitles(client, tmp_path)
        provider = TestProvider()
        episode = Episode("show", 1)
        stream = (await provider.stream(episode))[0]
        session = PlaybackSession(
            provider_name="test", provider=provider, show=Show("show", "Show"),
            queue=[episode], store=store, player_factory=lambda: player, provider_subtitles=service,
        )
        session._player = player
        session._stream = stream
        session._provider_tracks = service.choices(stream)
        session._generation = 1
        session.state.episode = episode
        session._loaded.set()
        tracks = await session.subtitle_choices()
        automatic = asyncio.create_task(session._load_subtitles(1, episode, stream))
        await asyncio.wait_for(automatic_started.wait(), 5)
        manual = asyncio.create_task(session.use_subtitle(tracks[1]))
        await asyncio.wait_for(manual_fetched.wait(), 5)
        release_automatic.set()
        await asyncio.wait_for(asyncio.gather(automatic, manual), 5)
        french_path = await service.fetch(tracks[1])
        assert player.commands[-1] == ("sub-add", french_path, "select")
        assert session.state.subtitles == "fr: French"


async def test_manual_command_finishing_after_episode_change_cannot_update_new_state(tmp_path, store):
    command_started, release_command = asyncio.Event(), asyncio.Event()

    class DelayedPlayer(TestPlayer):
        async def command(self, *command):
            command_started.set()
            await release_command.wait()
            self.commands.append(command)

    player = DelayedPlayer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=VTT))) as client:
        service = ProviderSubtitles(client, tmp_path)
        provider = TestProvider()
        episode = Episode("show", 1)
        stream = (await provider.stream(episode))[0]
        session = PlaybackSession(
            provider_name="test", provider=provider, show=Show("show", "Show"),
            queue=[episode], store=store, player_factory=lambda: player, provider_subtitles=service,
        )
        session._player = player
        session._stream = stream
        session._provider_tracks = service.choices(stream)
        session._generation = 1
        session.state.episode = episode
        session._loaded.set()
        tracks = await session.subtitle_choices()
        manual = asyncio.create_task(session.use_subtitle(tracks[0]))
        await asyncio.wait_for(command_started.wait(), 5)
        session._generation += 1
        session.state.episode = Episode("show", 2)
        session.state.subtitles = "new episode subtitles"
        session.state.message = "new episode"
        release_command.set()
        await asyncio.wait_for(manual, 5)
        assert session.state.subtitles == "new episode subtitles"
        assert session.state.message == "new episode"
