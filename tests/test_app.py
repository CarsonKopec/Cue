from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from textual.widgets import Input, OptionList

from cue.app import CueApp
from cue.config import Config
from cue.screens.episodes import EpisodesScreen
from cue.screens.home import HomeScreen
from cue.screens.player import NowPlayingScreen
from cue.screens.results import ResultsScreen, fuzzy_filter
from cue.providers.base import Show
from cue.store import Store
from cue.widgets import EpisodeGrid


def _app(media_dir: Path, store: Store, tmp_path: Path, player_factory=None, **config) -> CueApp:
    offline = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    return CueApp(
        Config(providers={"local": {"path": str(media_dir)}, "jellyfin": {"url": ""}}, **config),
        store=store,
        http=offline,
        player_factory=player_factory,
        plugin_dir=tmp_path / "no-plugins",
    )


def test_fuzzy_filter() -> None:
    shows = [Show("1", "Sousou no Frieren"), Show("2", "Spy x Family"), Show("3", "Frieren Specials")]
    assert {s.id for s in fuzzy_filter("frieren", shows)} == {"1", "3"}
    assert fuzzy_filter("spy", shows)[0].id == "2"
    assert fuzzy_filter("zzzz", shows) == []
    assert fuzzy_filter("", shows) == shows


async def test_home_shows_setup_problems(media_dir: Path, store: Store, tmp_path: Path) -> None:
    app = _app(media_dir, store, tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        assert isinstance(app.screen, HomeScreen)
        assert app.provider_name == "local"  # jellyfin is the default but not set up
        assert "jellyfin: set url and api_key" in app.setup_hint()
        await pilot.press("tab")
        assert app.provider_name == "local"
        await pilot.press("escape", "a")
        assert app.auto_skip is False


async def test_search_to_playback_and_back(media_dir: Path, store: Store, tmp_path: Path, fake_mpv) -> None:
    factory, log = fake_mpv
    app = _app(media_dir, store, tmp_path, player_factory=factory)
    async with app.run_test(size=(100, 30)) as pilot:
        app.screen.query_one("#search", Input).value = "frieren"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, ResultsScreen)
        assert app.screen.query_one("#results", OptionList).option_count == 1

        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, EpisodesScreen)
        assert [e.number for e in screen.episodes] == [1, 2, 3]
        assert screen.query_one(EpisodeGrid).row_count == 1

        # Play a range: episodes 2-3, back to back.
        screen.query_one("#range", Input).value = "2-3"
        screen.query_one("#range", Input).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, NowPlayingScreen)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, EpisodesScreen)

        assert len(log.launches()) == 2
        show_id = screen.show.id
        assert store.watched("local", show_id) == {2.0, 3.0}
        cell = screen.query_one(EpisodeGrid).get_cell_at((0, 1))
        assert str(cell).startswith("✓")

        # q twice goes back through Results to Home, where the show is in Continue Watching.
        await pilot.press("escape", "q")
        await pilot.pause()
        assert isinstance(app.screen, ResultsScreen)
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)
        assert app.screen.query_one("#continue", OptionList).option_count == 1


async def test_grid_enter_plays_from_that_episode(media_dir: Path, store: Store, tmp_path: Path, fake_mpv) -> None:
    factory, log = fake_mpv
    app = _app(media_dir, store, tmp_path, player_factory=factory)
    async with app.run_test(size=(100, 30)) as pilot:
        provider = app.providers["local"]
        app.open_show("local", (await provider.search("frieren"))[0])
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("l", "enter")  # vim-right to episode 2, then play
        await pilot.pause()
        assert isinstance(app.screen, NowPlayingScreen)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, EpisodesScreen)
    played = [args[-1].rsplit("Frieren - ", 1)[1][:2] for args in log.launches()]
    assert played == ["02", "03"]


async def test_continue_watching_resumes(media_dir: Path, store: Store, tmp_path: Path, fake_mpv) -> None:
    factory, log = fake_mpv
    app = _app(media_dir, store, tmp_path, player_factory=factory)
    show_id = "sousou-no-frieren"
    store.mark_watched("local", show_id, 1)
    store.save_progress("local", show_id, "Sousou no Frieren", 2, 7.5)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.press("down", "enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, EpisodesScreen)
    first = log.launches()[0]
    assert "--start=7.50" in first and first[-1].endswith("Frieren - 02 (1080p) [ABCD1234].mkv")


@pytest.mark.parametrize("player,label", [("vlc", "VLC"), ("mpv", "mpv")])
async def test_missing_player_is_reported(media_dir: Path, store: Store, tmp_path: Path, monkeypatch, player, label) -> None:
    monkeypatch.setattr(f"cue.app.find_{player}", lambda: None)
    app = _app(media_dir, store, tmp_path, player=player)
    async with app.run_test(size=(100, 30)) as pilot:
        provider = app.providers["local"]
        show = (await provider.search("frieren"))[0]
        app.play("local", show, await provider.episodes(show), 0)
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)
        assert any(f"{label} not found" in str(n.message) for n in app._notifications)


@pytest.mark.parametrize("player,backend", [("vlc", "VlcPlayer"), ("mpv", "MpvPlayer")])
async def test_configured_player_is_used(
    media_dir: Path, store: Store, tmp_path: Path, monkeypatch, fake_mpv, player, backend
) -> None:
    factory, log = fake_mpv
    chosen = []

    def player_class(executable):
        chosen.append(executable)
        return factory()

    monkeypatch.setattr(f"cue.app.find_{player}", lambda: "selected-player")
    monkeypatch.setattr(f"cue.app.{backend}", player_class)
    app = _app(media_dir, store, tmp_path, player=player, auto_next=False)
    async with app.run_test(size=(100, 30)) as pilot:
        provider = app.providers["local"]
        show = (await provider.search("frieren"))[0]
        app.play("local", show, await provider.episodes(show), 0)
        await pilot.pause()
        await app.workers.wait_for_complete()
    assert chosen == ["selected-player"]
    assert len(log.launches()) == 1
