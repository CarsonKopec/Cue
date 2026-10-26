"""SUB/DUB selection must stay fixed for an entire playback/download queue."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path

import httpx
import pytest
from textual.widgets import Select

from cue.app import CueApp
from cue.config import Config
from cue.providers.anime import AnimeProvider
from cue.providers.base import Episode, ProviderConfigError, ProviderError, Show
from cue.screens.episodes import EpisodesScreen
from cue.store import Store


SHOW = Show("example-123", "Example")
EPISODES = [Episode(SHOW.id, 1), Episode(SHOW.id, 2)]


def make_provider(language="sub", sources=None):
    if sources is None:
        sources = [
            {"file": "https://media.test/sub.mp4", "language": "sub"},
            {"file": "https://media.test/dub.mp4", "language": "dub"},
        ]

    def respond(request):
        if "/episode/list/" in request.url.path:
            html = "".join(
                f'<div class="ep-item" data-number="{episode.label}" data-id="{episode.label}">'
                f'<a href="/watch/{SHOW.id}?ep={episode.label}">{episode.label}</a></div>'
                for episode in EPISODES
            )
            return httpx.Response(200, json={"html": html})
        return httpx.Response(200, json={"sources": sources})

    provider = AnimeProvider({"language": language}, transport=httpx.MockTransport(respond))
    provider._episode_ids.update({(SHOW.id, episode.number): episode.label for episode in EPISODES})
    return provider


def make_app(provider, store, tmp_path, monkeypatch):
    monkeypatch.setattr("cue.app.create_providers", lambda *args: ({"anime": provider}, {}))
    return CueApp(
        Config(default_provider="anime", download_dir=str(tmp_path / "downloads")),
        store=store,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))),
        player_factory=lambda: None,
        plugin_dir=tmp_path,
    )


async def test_language_views_are_immutable_and_independent():
    provider = make_provider()
    try:
        sub = provider.for_language("sub")
        dub = provider.for_language("dub")
        with pytest.raises(FrozenInstanceError):
            sub.language = "dub"
        # Changing the provider default cannot affect a previously bound queue.
        provider.language = "dub"
        for episode in EPISODES:
            sub_streams, dub_streams = await asyncio.gather(sub.stream(episode), dub.stream(episode))
            assert [stream.url for stream in sub_streams] == ["https://media.test/sub.mp4"]
            assert [stream.url for stream in dub_streams] == ["https://media.test/dub.mp4"]
        with pytest.raises(ProviderConfigError):
            provider.for_language("both")
    finally:
        await provider.aclose()


async def test_unavailable_mode_is_clear_and_never_falls_back():
    provider = make_provider(sources=[{"file": "https://media.test/sub.mp4", "language": "sub"}])
    try:
        with pytest.raises(ProviderError, match="no DUB streams"):
            await provider.for_language("dub").stream(EPISODES[0])
        assert await provider.for_language("sub").stream(EPISODES[0])
    finally:
        await provider.aclose()


async def test_episode_selector_honors_config_and_keyboard_toggle(store: Store, tmp_path: Path, monkeypatch):
    provider = make_provider("dub")
    app = make_app(provider, store, tmp_path, monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        app.open_show("anime", SHOW)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, EpisodesScreen)
        selector = app.screen.query_one("#audio-mode", Select)
        assert selector.value == "dub"
        await pilot.press("b")
        await pilot.pause()
        assert selector.value == "sub"
        assert app.audio_mode("anime") == "sub"
        assert provider.language == "dub"
        selector.value = "dub"
        await pilot.pause()
        assert app.audio_mode("anime") == "dub"


async def test_playback_keeps_mode_after_selection_changes(store: Store, tmp_path: Path, monkeypatch):
    app = make_app(make_provider(), store, tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        # Capture the screen before it starts the real playback worker.
        screens = []
        monkeypatch.setattr(app, "push_screen", lambda screen, **kwargs: screens.append(screen))
        app.set_audio_mode("anime", "sub")
        app.play("anime", SHOW, EPISODES, 0)
        app.set_audio_mode("anime", "dub")
        app.play("anime", SHOW, EPISODES, 0)
        sub, dub = (screen.session.provider for screen in screens)
        assert sub.language == "sub" and dub.language == "dub"
        for episode in EPISODES:
            assert (await sub.stream(episode))[0].url.endswith("/sub.mp4")
            assert (await dub.stream(episode))[0].url.endswith("/dub.mp4")


async def test_download_binds_before_worker_starts_and_names_variants(store: Store, tmp_path: Path, monkeypatch):
    app = make_app(make_provider(), store, tmp_path, monkeypatch)
    downloads = []

    async def save(stream, folder, name, progress):
        downloads.append((stream.url, name))

    monkeypatch.setattr("cue.app.download", save)
    async with app.run_test() as pilot:
        workers = []
        monkeypatch.setattr(app, "run_worker", lambda coroutine, **kwargs: workers.append(coroutine))
        app.download("anime", SHOW, EPISODES)
        app.set_audio_mode("anime", "dub")
        app.download("anime", SHOW, EPISODES)
        for coroutine in workers:
            await coroutine
    assert downloads == [
        ("https://media.test/sub.mp4", "Example - 01 [SUB]"),
        ("https://media.test/sub.mp4", "Example - 02 [SUB]"),
        ("https://media.test/dub.mp4", "Example - 01 [DUB]"),
        ("https://media.test/dub.mp4", "Example - 02 [DUB]"),
    ]


async def test_generic_provider_has_no_audio_selector(media_dir, store, tmp_path):
    app = CueApp(
        Config(providers={"local": {"path": str(media_dir)}}),
        store=store,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))),
        plugin_dir=tmp_path,
    )
    async with app.run_test() as pilot:
        provider = app.providers["local"]
        show = (await provider.search("Frieren"))[0]
        app.open_show("local", show)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not list(app.screen.query("#audio-mode"))
        assert app.audio_mode("local") is None
        assert app._playback_provider("local") is provider
        assert app.screen.check_action("toggle_audio", ()) is None
