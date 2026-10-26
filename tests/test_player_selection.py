from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cue.player import locate
from cue.player.base import PlayerError
from cue.player.session import PlaybackSession
from cue.providers.base import Episode, Show, Stream
from cue.store import Store


@pytest.fixture
def vlc_search(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CUE_VLC", raising=False)
    monkeypatch.setattr(locate.sys, "platform", "win32")
    monkeypatch.setattr(locate, "app_folder", lambda: tmp_path / "cue")
    monkeypatch.setattr(locate.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "programs"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "programs32"))
    return tmp_path


def _executable(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return str(path)


def test_vlc_environment_override_takes_priority(vlc_search: Path, monkeypatch) -> None:
    chosen = _executable(vlc_search / "portable" / "vlc.exe")
    _executable(vlc_search / "cue" / "vlc.exe")
    monkeypatch.setenv("CUE_VLC", chosen)
    assert locate.find_vlc() == chosen


def test_invalid_vlc_override_is_not_silently_ignored(vlc_search: Path, monkeypatch) -> None:
    _executable(vlc_search / "cue" / "vlc.exe")
    monkeypatch.setenv("CUE_VLC", str(vlc_search / "missing.exe"))
    assert locate.find_vlc() is None


@pytest.mark.parametrize("subfolder", ["", "vlc"])
def test_vlc_beside_cue_precedes_path(vlc_search: Path, monkeypatch, subfolder: str) -> None:
    chosen = _executable(vlc_search / "cue" / subfolder / "vlc.exe")
    monkeypatch.setattr(locate.shutil, "which", lambda name: "from-path")
    assert locate.find_vlc() == chosen


def test_vlc_from_path(vlc_search: Path, monkeypatch) -> None:
    monkeypatch.setattr(locate.shutil, "which", lambda name: "from-path" if name == "vlc.exe" else None)
    assert locate.find_vlc() == "from-path"


@pytest.mark.parametrize("folder", ["programs", "programs32"])
def test_vlc_standard_windows_install(vlc_search: Path, folder: str) -> None:
    chosen = _executable(vlc_search / folder / "VideoLAN" / "VLC" / "vlc.exe")
    assert locate.find_vlc() == chosen


def test_vlc_mac_app_locations(vlc_search: Path, monkeypatch) -> None:
    monkeypatch.setattr(locate.sys, "platform", "darwin")
    assert Path("/Applications/VLC.app/Contents/MacOS/VLC") in locate._vlc_locations()


def test_vlc_missing(vlc_search: Path) -> None:
    assert locate.find_vlc() is None


@pytest.mark.parametrize("during_launch", [True, False])
async def test_session_reports_backend_errors(store: Store, during_launch: bool) -> None:
    class FailingPlayer:
        def __init__(self):
            self.observers = {}
            self.closed = False

        def observe(self, name, callback):
            self.observers[name] = callback

        def on_close(self, callback):
            pass

        async def launch(self, url, **options):
            if during_launch:
                raise PlayerError("Playback unavailable")
            asyncio.get_running_loop().call_soon(self.observers["error"], "Playback unavailable")

        async def quit(self):
            self.closed = True

    player = FailingPlayer()
    session = PlaybackSession(
        provider_name="test",
        provider=SimpleNamespace(stream=AsyncMock(return_value=[Stream("https://example.test/video.mp4")])),
        show=Show("show", "A show"),
        queue=[Episode("show", 1), Episode("show", 2)],
        store=store,
        player_factory=lambda: player,
    )
    await asyncio.wait_for(session.run(), 1.0)
    assert session.state.finished
    assert session.state.error == "Playback unavailable"
    assert session.state.message == "Playback unavailable"
    assert session.index == 0
    assert not store.watched("test", "show")
    if not during_launch:
        assert player.closed
