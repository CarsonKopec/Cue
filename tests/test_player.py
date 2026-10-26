from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cue.player.mpv import MpvError, MpvPlayer
from cue.player.session import PlaybackSession
from cue.providers.base import Episode, Show
from cue.providers.local import LocalProvider
from cue.skip import SkipInfo, SkipInterval
from cue.store import Store


async def test_mpv_launch_ipc_and_quit(fake_mpv) -> None:
    factory, log = fake_mpv
    player = factory()
    positions: list[float] = []
    closed = asyncio.Event()
    player.observe("time-pos", lambda value: positions.append(value))
    player.on_close(closed.set)

    await player.launch("C:/video.mkv", start=3.0, headers={"Authorization": 'MediaBrowser Token="t"'},
                        title="Show — Episode 1", extra_args=["--fs"])
    await asyncio.sleep(0.3)
    assert positions and positions[0] >= 3.0
    assert await player.command("get_property", "duration") == 20.0
    await player.seek(15, "absolute")
    await player.quit()
    assert closed.is_set() and not player.running

    args = log.launches()[0]
    assert f"--input-ipc-server={player.address}" in args
    assert "--force-window=immediate" in args and "--keep-open=yes" in args
    assert "--start=3.00" in args and "--fs" in args
    assert '--http-header-fields-append=Authorization: MediaBrowser Token="t"' in args
    assert args[-2:] == ["--", "C:/video.mkv"]
    assert ["seek", 15, "absolute"] in log.commands("seek")
    with pytest.raises(MpvError):
        await player.command("get_property", "duration")


async def test_mpv_that_never_starts_is_reported(tmp_path: Path) -> None:
    player = MpvPlayer(str(tmp_path / "missing-mpv.exe"))
    with pytest.raises(MpvError, match="Cannot start mpv"):
        await player.launch("x")


class FakeSkip:
    def __init__(self, intervals: list[SkipInterval]) -> None:
        self.intervals = intervals

    async def lookup(self, provider: str, show: Show, episode: Episode) -> SkipInfo:
        return SkipInfo(1, list(self.intervals))


async def _session(media_dir: Path, store: Store, factory, **options) -> tuple[PlaybackSession, Show]:
    provider = LocalProvider({"path": str(media_dir)})
    show = (await provider.search("frieren"))[0]
    episodes = await provider.episodes(show)
    session = PlaybackSession(
        provider_name="local",
        provider=provider,
        show=show,
        queue=episodes,
        store=store,
        player_factory=factory,
        **options,
    )
    return session, show


async def test_session_skips_saves_and_auto_advances(media_dir: Path, store: Store, fake_mpv) -> None:
    factory, log = fake_mpv
    session, show = await _session(
        media_dir, store, factory, skip=FakeSkip([SkipInterval("op", 2.0, 10.0)]), index=1
    )
    states = []
    session.listener = lambda state: states.append((state.episode.number if state.episode else None, state.message))
    await asyncio.wait_for(session.run(), 30)

    # Episodes 2 and 3 played back to back (auto-next), each skipping its opening.
    assert len(log.launches()) == 2
    assert log.launches()[1][-1].endswith("Sousou no Frieren - 03 (1080p).mkv")
    seeks = log.commands("seek")
    assert seeks.count(["seek", 10.0, "absolute"]) == 2
    assert ["show-text", "Skipped opening", 2000] in log.commands("show-text")
    assert store.watched("local", show.id) == {2.0, 3.0}
    entry = store.get_history("local", show.id)
    assert entry is not None and entry.last_episode == 3 and entry.position == 0
    assert session.state.finished and session.state.error is None
    assert (2.0, "Skipped opening") in states


async def until(predicate, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


def playing(session: PlaybackSession, number: float, after: float = 0.3):
    """True once ``number`` has been playing for a moment."""

    def check() -> bool:
        state = session.state
        return state.episode is not None and state.episode.number == number and (state.position or 0) > after

    return check


async def test_session_resumes_and_stops(media_dir: Path, store: Store, fake_mpv, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MPV_SPEED", "4")
    factory, log = fake_mpv
    session, show = await _session(media_dir, store, factory, auto_skip=lambda: False,
                                   skip=FakeSkip([SkipInterval("op", 0.0, 30.0)]))
    store.save_progress("local", show.id, show.title, 1, 12.0)
    task = asyncio.create_task(session.run())
    await until(playing(session, 1.0, after=13.0))
    session.stop()
    await asyncio.wait_for(task, 10)

    assert "--start=12.00" in log.launches()[0]
    assert log.commands("seek") == []  # auto-skip off
    entry = store.get_history("local", show.id)
    assert entry is not None and entry.last_episode == 1 and entry.position > 12
    assert store.watched("local", show.id) == set()


async def test_session_next_previous_and_manual_skip(media_dir: Path, store: Store, fake_mpv, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_MPV_SPEED", "1")
    monkeypatch.setenv("FAKE_MPV_DURATION", "300")
    factory, log = fake_mpv
    session, _show = await _session(media_dir, store, factory)
    task = asyncio.create_task(session.run())
    await until(playing(session, 1.0))
    await session.manual_skip()
    await until(lambda: (session.state.position or 0) > 85)
    session.next()
    await until(playing(session, 2.0))
    session.previous()
    await until(playing(session, 1.0))
    session.previous()  # already on the first: just a message
    assert session.state.message == "No previous episode"
    session.stop()
    await asyncio.wait_for(task, 10)

    launched = [args[-1].rsplit("Frieren - ", 1)[1][:2] for args in log.launches()]
    assert launched == ["01", "02", "01"]
    assert ["seek", 85.0, "relative"] in log.commands("seek")


async def test_session_reports_missing_player(media_dir: Path, store: Store, tmp_path: Path) -> None:
    session, _show = await _session(media_dir, store, lambda: MpvPlayer(str(tmp_path / "nope.exe")))
    await asyncio.wait_for(session.run(), 10)
    assert session.state.error and "Cannot start mpv" in session.state.error
