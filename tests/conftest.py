from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cue.player.mpv import MpvPlayer
from cue.store import Store

FAKE_MPV = Path(__file__).with_name("fake_mpv.py")


@pytest.fixture(autouse=True)
def cue_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "cue-home"
    monkeypatch.setenv("CUE_HOME", str(home))
    return home


@pytest.fixture
def store() -> Store:
    db = Store(":memory:")
    yield db
    db.close()


@pytest.fixture
def media_dir(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    files = [
        "[SubsPlease] Sousou no Frieren - 01 (1080p) [ABCD1234].mkv",
        "[SubsPlease] Sousou no Frieren - 02 (1080p) [ABCD1234].mkv",
        "[SubsPlease] Sousou no Frieren - 02 (720p) [ABCD1234].mkv",
        "[SubsPlease] Sousou no Frieren - 03 (1080p).mkv",
        "Spy x Family/Season 2/01.mkv",
        "Spy x Family/Season 2/02.mkv",
        "Mob.Psycho.100.S01E01.1080p.WEB.mkv",
        "notes.txt",
    ]
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    return root


class FakeMpvLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]

    def launches(self) -> list[list[str]]:
        return [entry["args"] for entry in self.entries() if "args" in entry]

    def commands(self, name: str | None = None) -> list[list]:
        commands = [entry["command"] for entry in self.entries() if "command" in entry]
        return [c for c in commands if name is None or c[0] == name]


@pytest.fixture
def fake_mpv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Callable[[], MpvPlayer], FakeMpvLog]:
    """A player factory running tests/fake_mpv.py, and the log it writes."""
    log = FakeMpvLog(tmp_path / "fake-mpv.log")
    monkeypatch.setenv("FAKE_MPV_LOG", str(log.path))
    monkeypatch.setenv("FAKE_MPV_DURATION", "20")
    monkeypatch.setenv("FAKE_MPV_SPEED", "40")

    def factory() -> MpvPlayer:
        name = f"cue-test-{uuid.uuid4().hex[:12]}"
        address = rf"\\.\pipe\{name}" if sys.platform == "win32" else os.path.join(str(tmp_path), f"{name}.sock")
        return MpvPlayer([sys.executable, str(FAKE_MPV)], address=address)

    return factory, log


def json_transport(routes: dict[str, Callable[[httpx.Request], httpx.Response]]) -> httpx.MockTransport:
    """Route by ``"METHOD host/path"`` prefix; unknown requests get a 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.host}{request.url.path}"
        for prefix, respond in routes.items():
            if key.startswith(prefix):
                return respond(request)
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)
