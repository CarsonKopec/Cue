"""cue install / uninstall: PATH editing, copying, Apps & Features, and the first-run offer."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from cue import __version__
from cue.commands import DECLINED, offer_install, run
from cue.install import (
    ENVIRONMENT_KEY, REG_DWORD, REG_EXPAND_SZ, REG_SZ, UNINSTALL_KEY, UserRegistry, install, installed,
    path_with, path_without, uninstall,
)


class FakeRegistry:
    def __init__(self, path: str | None = None, kind: int = REG_EXPAND_SZ) -> None:
        self.keys: dict[str, dict[str, tuple[object, int]]] = {}
        if path is not None:
            self.keys[ENVIRONMENT_KEY] = {"Path": (path, kind)}
        self.broadcasts = 0

    def get(self, key, name):
        return self.keys.get(key, {}).get(name)

    def set(self, key, values):
        self.keys.setdefault(key, {}).update(values)

    def delete_key(self, key):
        self.keys.pop(key, None)

    def environment_changed(self):
        self.broadcasts += 1


def test_path_editing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "Programs" / "Cue"
    assert path_with("", folder) == str(folder)
    assert path_with(r"C:\Windows;;C:\Tools;", folder) == rf"C:\Windows;C:\Tools;{folder}"
    for spelling in (str(folder).upper(), f"{folder}\\", f'"{folder}"'):
        assert path_with(rf"C:\Windows;{spelling}", folder) is None
    monkeypatch.setenv("CUE_TEST_BASE", str(tmp_path))
    assert path_with(r"%CUE_TEST_BASE%\Programs\Cue", folder) is None
    assert path_without(rf"C:\Windows;%CUE_TEST_BASE%\Programs\Cue;C:\Tools", folder) == r"C:\Windows;C:\Tools"
    assert path_without(r"C:\Windows", folder) is None


def _download(tmp_path: Path, content: bytes = b"cue v2") -> Path:
    download = tmp_path / "Downloads" / "cue"
    (download / "mpv").mkdir(parents=True)
    (download / "cue.exe").write_bytes(content)
    (download / "README.md").write_text("readme", encoding="utf-8")
    (download / "mpv" / "mpv.exe").write_bytes(b"mpv")
    return download / "cue.exe"


def test_install_copies_registers_and_adds_to_path(tmp_path: Path) -> None:
    registry = FakeRegistry(r"C:\Windows;%USERPROFILE%\bin")
    folder = tmp_path / "Programs" / "Cue"
    target, path_changed = install(_download(tmp_path), registry, folder)
    assert path_changed and target == folder / "cue.exe" and target.read_bytes() == b"cue v2"
    assert (folder / "README.md").is_file() and (folder / "mpv" / "mpv.exe").is_file()
    assert registry.get(ENVIRONMENT_KEY, "Path") == (rf"C:\Windows;%USERPROFILE%\bin;{folder}", REG_EXPAND_SZ)
    assert registry.get(UNINSTALL_KEY, "UninstallString") == (f'"{target}" uninstall', REG_SZ)
    assert registry.get(UNINSTALL_KEY, "NoModify") == (1, REG_DWORD)
    assert registry.broadcasts == 1
    found = installed(registry)
    assert found is not None and found.folder == folder and found.version == __version__

    # Installing again (a newer download) replaces the program but leaves PATH alone.
    target, path_changed = install(_download(tmp_path / "newer", b"cue v3"), registry, folder)
    assert not path_changed and target.read_bytes() == b"cue v3" and registry.broadcasts == 1
    assert not list(folder.glob("cue.old-*")) and not list(folder.glob("*.new"))


def test_install_keeps_a_plain_path_value_type(tmp_path: Path) -> None:
    registry = FakeRegistry(r"C:\Windows", REG_SZ)
    install(_download(tmp_path), registry, tmp_path / "Programs" / "Cue")
    assert registry.get(ENVIRONMENT_KEY, "Path")[1] == REG_SZ


def test_uninstall_removes_only_cues_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = FakeRegistry(r"C:\Windows")
    folder = tmp_path / "Programs" / "Cue"
    install(_download(tmp_path), registry, folder)
    (folder / "notes.txt").write_text("mine", encoding="utf-8")
    assert uninstall(registry, running=tmp_path / "elsewhere.exe") == folder
    assert registry.get(ENVIRONMENT_KEY, "Path") == (r"C:\Windows", REG_EXPAND_SZ)
    assert UNINSTALL_KEY not in registry.keys
    assert sorted(path.name for path in folder.iterdir()) == ["notes.txt"]

    (folder / "notes.txt").unlink()
    install(_download(tmp_path / "again"), registry, folder)
    scheduled = []
    monkeypatch.setattr("cue.install._delete_after_exit", lambda paths, where: scheduled.append((paths, where)))
    uninstall(registry, running=folder / "cue.exe")
    assert scheduled == [([folder / "cue.exe"], folder)]  # a running program can't delete itself


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_user_registry_round_trip() -> None:
    key = rf"Software\CueTests\{uuid.uuid4().hex}"
    registry = UserRegistry()
    try:
        registry.set(key, {"Text": ("a", REG_SZ), "Expand": (r"%TEMP%\x", REG_EXPAND_SZ), "Number": (7, REG_DWORD)})
        assert registry.get(key, "Text") == ("a", REG_SZ)
        assert registry.get(key, "Expand") == (r"%TEMP%\x", REG_EXPAND_SZ)
        assert registry.get(key, "Number") == (7, REG_DWORD)
        assert registry.get(key, "Missing") is None
    finally:
        registry.delete_key(key)
        registry.delete_key(r"Software\CueTests")
    assert registry.get(key, "Text") is None


def test_install_command_from_source(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["install"]) == 1
    assert "pip already put `cue` on PATH" in capsys.readouterr().out


class _Terminal:
    def isatty(self) -> bool:
        return True


@pytest.fixture
def offer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    registry = FakeRegistry("")
    installs: list[Path] = []
    monkeypatch.delenv("CUE_HOME")
    monkeypatch.setattr("cue.commands.app_dir", lambda: tmp_path / "appdata")
    monkeypatch.setattr("cue.commands.is_frozen", lambda: True)
    monkeypatch.setattr("cue.commands.UserRegistry", lambda: registry)
    monkeypatch.setattr("cue.commands.install_dir", lambda: tmp_path / "Programs" / "Cue")
    monkeypatch.setattr("cue.commands.install", lambda source, reg: installs.append(source) or (source, True))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Downloads" / "cue.exe"))
    monkeypatch.setattr(sys, "stdin", _Terminal())
    return registry, installs, tmp_path


@pytest.mark.skipif(sys.platform != "win32", reason="the offer is for cue.exe on Windows")
def test_first_run_offer(offer) -> None:
    registry, installs, tmp_path = offer
    questions: list[str] = []

    def answer(reply: str):
        def ask(question: str) -> str:
            questions.append(question)
            return reply
        return ask

    offer_install(answer("n"))
    assert "Install Cue for this user and add it to PATH" in questions[-1] and installs == []
    offer_install(answer(""))
    assert installs == [Path(sys.executable)]

    folder = tmp_path / "Programs" / "Cue"
    folder.mkdir(parents=True)
    (folder / "cue.exe").write_bytes(b"")
    registry.set(UNINSTALL_KEY, {"InstallLocation": (str(folder), REG_SZ), "DisplayVersion": ("0.1.0", REG_SZ)})
    offer_install(answer("n"))
    assert f"Update the installed Cue 0.1.0 to {__version__}?" in questions[-1]

    registry.set(UNINSTALL_KEY, {"DisplayVersion": (__version__, REG_SZ)})
    count = len(questions)
    offer_install(answer("y"))  # the same version is installed: nothing to ask
    assert len(questions) == count

    registry.delete_key(UNINSTALL_KEY)
    offer_install(answer("never"))
    assert (tmp_path / "appdata" / DECLINED).is_file()
    offer_install(answer("y"))
    assert len(questions) == count + 1  # never asked again
