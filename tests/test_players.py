"""Player registry, ProcessPlayer, the ffplay and default-app plugins, and playing through a plugin player."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from cue.app import CueApp
from cue.config import Config
from cue.origins import loading
from cue.player.base import PlayerError
from cue.player.process import ProcessPlayer
from cue.player.registry import PlayerBackend, player_origin, players, register_player, unregister_players
from cue.store import Store


class Scripted(ProcessPlayer):
    """Runs a Python snippet as the 'player'; it gets the URL, start, headers and title as arguments."""

    title = "Scripted"
    headers = frozenset({"referer"})

    def __init__(self, script: str) -> None:
        super().__init__(sys.executable)
        self.script = script

    def arguments(self, url, *, start, headers, title, extra_args):
        return ["-c", self.script, url, str(start), json.dumps(dict(headers)), title or ""]


async def play_until_closed(player: ProcessPlayer, url: str = "media.mkv", **options) -> list[str]:
    closed, errors = asyncio.Event(), []
    player.on_close(closed.set)
    player.observe("error", errors.append)
    await player.launch(url, **options)
    await asyncio.wait_for(closed.wait(), 20)
    return errors


async def test_process_player_passes_the_launch_details(tmp_path: Path) -> None:
    record = tmp_path / "args.json"
    script = f"import json, sys; open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))"
    errors = await play_until_closed(Scripted(script), "https://cdn.example/a.m3u8", start=12.5,
                                     headers={"Referer": "https://site.example/"}, title="Show — Episode 2")
    assert errors == []
    assert json.loads(record.read_text()) == [
        "https://cdn.example/a.m3u8", "12.5", '{"Referer": "https://site.example/"}', "Show — Episode 2",
    ]


async def test_process_player_reports_failures_and_survives_noisy_programs() -> None:
    failed = Scripted("import sys; sys.stderr.write('warming up\\ncannot open the stream\\n'); sys.exit(3)")
    assert await play_until_closed(failed) == ["Scripted: cannot open the stream"]
    # A program that writes more than a pipe holds must not hang (it once did).
    noisy = Scripted("import sys; sys.stderr.write('x' * 500_000 + '\\r' * 1000)")
    assert await play_until_closed(noisy) == []


async def test_process_player_quit_and_refusals() -> None:
    player = Scripted("import time; time.sleep(60)")
    closed = asyncio.Event()
    player.on_close(closed.set)
    await player.launch("media.mkv")
    assert player.running
    with pytest.raises(PlayerError, match="can't be controlled"):
        await player.command("seek", 10)
    with pytest.raises(PlayerError, match="already started"):
        await player.launch("other.mkv")
    await player.quit(timeout=10)
    await asyncio.wait_for(closed.wait(), 10)
    assert not player.running

    with pytest.raises(PlayerError, match="can't send the Cookie header"):
        await Scripted("pass").launch("u", headers={"Cookie": "session"})
    with pytest.raises(PlayerError, match="Invalid playback HTTP header"):
        await Scripted("pass").launch("u", headers={"Referer": "a\r\nInjected: 1"})
    missing = Scripted("pass")
    missing.program = str(Path("no-such-dir") / "player.exe")
    with pytest.raises(PlayerError, match="Could not start Scripted"):
        await missing.launch("u")


def test_builtin_players_and_program_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert {"vlc", "mpv"} <= set(players())
    assert player_origin("vlc") == player_origin("mpv") == "built-in"
    vlc = players()["vlc"]
    program = tmp_path / "vlc.exe"
    program.write_bytes(b"")
    monkeypatch.setattr("cue.player.registry.find_vlc", lambda: "found-vlc")
    assert vlc.program({}) == "found-vlc"
    assert vlc.program({"path": f'"{program}"'}) == str(program)
    assert vlc.program({"path": str(tmp_path / "missing.exe")}) is None  # a wrong path isn't silently ignored

    elsewhere = PlayerBackend("x", "X", lambda options: "auto", lambda p, o: None, platforms=("no-such-os",),
                              has_program=False)
    assert not elsewhere.supported and elsewhere.program({"path": str(program)}) == "auto"


def _app(media_dir: Path, store: Store, tmp_path: Path, **config) -> CueApp:
    return CueApp(
        Config(providers={"local": {"path": str(media_dir)}}, auto_next=False, **config),
        store=store,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))),
        dropin_dir=tmp_path / "no-dropins",
        plugin_root=tmp_path / "no-plugins",
    )


async def test_playing_through_a_plugin_player(media_dir: Path, store: Store, tmp_path: Path) -> None:
    program = tmp_path / "player.exe"
    program.write_bytes(b"")
    made = []

    def create(path: str, options) -> ProcessPlayer:
        made.append((path, dict(options)))
        return Scripted("import time; time.sleep(60)")

    with loading("plugin:test-players"):
        register_player(PlayerBackend("slowplayer", "Slow Player", lambda options: None, create, controls=False))
    app = _app(media_dir, store, tmp_path, player="slowplayer",
               players={"slowplayer": {"path": str(program), "speed": 2}})
    try:
        async with app.run_test(size=(100, 30)) as pilot:
            provider = app.providers["local"]
            show = (await provider.search("frieren"))[0]
            app.play("local", show, await provider.episodes(show), 0)
            for _ in range(50):
                await pilot.pause(0.1)
                session = next(iter(app._sessions), None)
                if session is not None and "can't control" in session.state.message:
                    break
            assert made == [(str(program), {"path": str(program), "speed": 2})]
            assert "Playing in Slow Player, which Cue can't control" in session.state.message
            await pilot.press("q")
            await pilot.pause(0.5)
    finally:
        unregister_players("plugin:test-players")


async def test_missing_or_foreign_player_is_reported(media_dir: Path, store: Store, tmp_path: Path) -> None:
    for player, message in (("nope", "The nope player isn't installed"),
                            ("elsewhere", "doesn't run on this system")):
        with loading("plugin:test-players"):
            register_player(PlayerBackend("elsewhere", "Elsewhere", lambda o: "p", lambda p, o: None,
                                          platforms=("no-such-os",)))
        app = _app(media_dir, store, tmp_path, player=player)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                provider = app.providers["local"]
                show = (await provider.search("frieren"))[0]
                app.play("local", show, await provider.episodes(show), 0)
                await pilot.pause()
                assert any(message in str(n.message) for n in app._notifications)
        finally:
            unregister_players("plugin:test-players")
