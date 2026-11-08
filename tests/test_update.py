"""Updates: finding the latest release, verified downloads, swapping cue.exe, plugins, the daily check, cue update."""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from cue import __version__, update
from cue.app import CueApp
from cue.commands import run as run_command
from cue.config import load_config
from cue.install import REG_SZ, UNINSTALL_KEY
from cue.plugins import PluginManager
from cue.plugins.archive import pack
from cue.store import Store
from test_install import FakeRegistry
from test_plugins import write_plugin

NEW = "9.9.9"
@pytest.fixture(autouse=True)
def windows_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests use Windows' cue.exe, whatever system runs them."""
    monkeypatch.setattr(update, "ASSET", "cue.exe")
    monkeypatch.setattr(update, "MAGIC", (b"MZ",))


def test_each_platform_has_its_own_program() -> None:
    assert update.asset_name("win32", "AMD64") == "cue.exe"
    assert update.asset_name("linux", "x86_64") == "cue-linux-x64"
    assert update.asset_name("linux", "aarch64") == "cue-linux-arm64"
    assert update.asset_name("darwin", "arm64") == "cue-macos-arm64"
    assert update.platform_tag("win32", "AMD64") == "windows-x64"


EXE_BYTES = b"MZ" + b"\x90" * 2000 + b"new cue"
API = update.RELEASES
DOWNLOADS = f"https://github.com/CarsonKopec/Cue/releases/download/v{NEW}/"


def release_json(version: str = NEW, assets: tuple[str, ...] = ("cue.exe", "SHA256SUMS.txt")) -> dict:
    return {
        "tag_name": f"v{version}",
        "html_url": f"https://github.com/CarsonKopec/Cue/releases/tag/v{version}",
        "assets": [{"name": name, "browser_download_url": DOWNLOADS + name} for name in assets],
    }


class GitHub:
    """A stand-in for GitHub's API and release downloads (which redirect to a file host)."""

    def __init__(self, release: dict | None = None, exe: bytes = EXE_BYTES, sums: str | None = None,
                 status: int = 200) -> None:
        self.release = release or release_json()
        self.exe = exe
        self.sums = sums if sums is not None else f"{hashlib.sha256(EXE_BYTES).hexdigest()}  cue.exe\n"
        self.status = status
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url == API:
            return httpx.Response(self.status, json=self.release)
        if url.startswith(DOWNLOADS):
            return httpx.Response(302, headers={"Location": "https://objects.example/" + url.rsplit("/", 1)[1]})
        if url == "https://objects.example/cue.exe":
            return httpx.Response(200, content=self.exe)
        if url == "https://objects.example/SHA256SUMS.txt":
            return httpx.Response(200, text=self.sums)
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


# -- releases and downloads ---------------------------------------------------------------------


async def test_latest_release_and_verified_download() -> None:
    github = GitHub()
    async with github.client() as http:
        release = await update.latest_release(http)
        assert (release.version, release.newer) == (NEW, True)
        assert release.exe == DOWNLOADS + "cue.exe" and release.page.endswith(f"/v{NEW}")
        assert await update.download(http, release) == EXE_BYTES
    # Followed GitHub's redirect to its file host for both files.
    assert "https://objects.example/cue.exe" in github.requests


async def test_release_problems_are_reported() -> None:
    for github, message in (
        (GitHub(release=release_json(assets=("cue.exe",))), "has no SHA256SUMS.txt"),
        (GitHub(release={"tag_name": "nightly", "assets": []}), "isn't a version number"),
        (GitHub(status=404), "answered HTTP 404"),
    ):
        async with github.client() as http:
            with pytest.raises(update.UpdateError, match=message):
                await update.latest_release(http)
    assert not update.Release("0.0.1", "", "", "").newer


async def test_download_refuses_what_doesnt_check_out() -> None:
    cases = (
        (GitHub(exe=b"MZ tampered"), "doesn't match"),
        (GitHub(sums="0" * 64 + "  other.exe\n"), "doesn't list cue.exe"),
        (GitHub(exe=b"not a program", sums=f"{hashlib.sha256(b'not a program').hexdigest()} *cue.exe\n"),
         "isn't a program for this computer"),
    )
    for github, message in cases:
        async with github.client() as http:
            release = await update.latest_release(http)
            with pytest.raises(update.UpdateError, match=message):
                await update.download(http, release)

    def downgrade(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://insecure.example/cue.exe"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(downgrade)) as http:
        with pytest.raises(update.UpdateError, match="only downloaded over https"):
            await update.download(http, update.Release(NEW, "", DOWNLOADS + "cue.exe", DOWNLOADS + "SHA256SUMS.txt"))


# -- swapping cue.exe -----------------------------------------------------------------------------------


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "Programs" / "Cue"
    folder.mkdir(parents=True)
    (folder / "cue.exe").write_bytes(b"MZ old cue")
    (folder / "cue.old-1234abcd.exe").write_bytes(b"left over")
    return folder


def test_apply_replaces_the_installed_copy_and_its_apps_version(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    registry = FakeRegistry()
    registry.set(UNINSTALL_KEY, {"InstallLocation": (str(folder), REG_SZ), "DisplayVersion": (__version__, REG_SZ)})
    target = update.apply(EXE_BYTES, NEW, folder / "cue.exe", registry)
    assert target.read_bytes() == EXE_BYTES
    assert sorted(path.name for path in folder.iterdir()) == ["cue.exe"]  # nothing staged or left over
    assert registry.get(UNINSTALL_KEY, "DisplayVersion") == (NEW, REG_SZ)


def test_apply_to_a_portable_copy_leaves_the_registry_alone(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    installed_elsewhere = tmp_path / "Installed"
    installed_elsewhere.mkdir()
    (installed_elsewhere / "cue.exe").write_bytes(b"MZ")
    registry = FakeRegistry()
    registry.set(UNINSTALL_KEY, {"InstallLocation": (str(installed_elsewhere), REG_SZ),
                                 "DisplayVersion": (__version__, REG_SZ)})
    update.apply(EXE_BYTES, NEW, folder / "cue.exe", registry)
    assert (folder / "cue.exe").read_bytes() == EXE_BYTES
    assert registry.get(UNINSTALL_KEY, "DisplayVersion") == (__version__, REG_SZ)


# -- the daily check ------------------------------------------------------------------------------------


def test_checks_happen_once_a_day(cue_home: Path) -> None:
    now = datetime(2026, 11, 8, 12, tzinfo=UTC)
    assert update.due(now)
    update.mark_checked(now)
    assert not update.due(now + timedelta(hours=23))
    assert update.due(now + timedelta(hours=24))
    assert update.due(now - timedelta(hours=1))  # the clock went back
    (cue_home / update.STATE_FILE).write_text("not json", encoding="utf-8")
    assert update.due(now)


# -- plugins ----------------------------------------------------------------------------------------------


def _catalog(tmp_path: Path, name: str, version: str) -> Path:
    folder = tmp_path / "catalog"
    sha = pack(write_plugin(tmp_path / f"src-{version}", name, version), folder / f"{name}-{version}.zip")
    entry = {"name": name, "version": version, "url": f"{name}-{version}.zip", "sha256": sha,
             "providers": [f"{name}-p"]}
    (folder / "index.json").write_text(json.dumps({"plugins": [entry]}), encoding="utf-8")
    return folder / "index.json"


async def test_plugins_update_unless_turned_off(tmp_path: Path) -> None:
    name = f"t{uuid.uuid4().hex[:8]}"
    catalog = _catalog(tmp_path, name, "2.0.0")
    root = tmp_path / "plugins"
    write_plugin(root / name, name, "1.0.0")
    manager = PluginManager(root)
    manager.load_enabled()
    try:
        async with httpx.AsyncClient() as http:
            listed = await update.run(http, manager, str(catalog), install=False, app=False)
            assert [entry.manifest.version for entry in listed.plugins_available] == ["2.0.0"]
            assert manager.get(name).manifest.version == "1.0.0"

            done = await update.run(http, manager, str(catalog), install=True, app=False)
            assert [plugin.manifest.version for plugin in done.plugins_installed] == ["2.0.0"]
            assert manager.get(name).loaded and done.notices() == [("Plugins updated", f"Updated Demo {name} 2.0.0.")]

            catalog = _catalog(tmp_path / "next", name, "3.0.0")
            manager.set_enabled(name, False)
            skipped = await update.run(http, manager, str(catalog), install=True, app=False)
            assert skipped.plugins_installed == [] and manager.get(name).manifest.version == "2.0.0"
    finally:
        for plugin in manager.plugins():
            manager.unload(plugin.name)


# -- in the app ------------------------------------------------------------------------------------------------


def _app(tmp_path: Path, store: Store, github: GitHub, config: str = "") -> CueApp:
    path = tmp_path / "config.toml"
    path.write_text('[plugins]\ncatalog = ""\n' + config, encoding="utf-8")
    return CueApp(load_config(path), store=store, http=github.client(),
                  dropin_dir=tmp_path / "no-dropins", plugin_root=tmp_path / "no-plugins")


@pytest.fixture
def frozen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Pretend to be cue.exe, running from a temporary folder, with a stand-in registry."""
    exe = tmp_path / "bin" / "cue.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ old cue")
    monkeypatch.setattr("cue.install.is_frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr("cue.update.user_registry", lambda: FakeRegistry())
    return exe


async def _settle(app: CueApp, pilot) -> None:
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


async def test_the_app_installs_a_new_cue_once_a_day(frozen: Path, store: Store, tmp_path: Path) -> None:
    github = GitHub()
    app = _app(tmp_path, store, github)
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert any("Cue 9.9.9 is installed" in str(n.message) for n in app._notifications)
    assert frozen.read_bytes() == EXE_BYTES

    asked = len(github.requests)
    app = _app(tmp_path, store, github)
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
    assert len(github.requests) == asked  # already checked today


async def test_the_app_only_tells_when_install_is_off(frozen: Path, store: Store, tmp_path: Path) -> None:
    app = _app(tmp_path, store, GitHub(), "[updates]\ninstall = false\n")
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert any("Cue 9.9.9 is available: run cue update" in str(n.message) for n in app._notifications)
    assert frozen.read_bytes() == b"MZ old cue"


async def test_offline_or_unfrozen_stays_quiet(frozen: Path, store: Store, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    offline = GitHub(status=503)
    app = _app(tmp_path, store, offline)
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert not app._notifications and offline.requests == [API]
    assert frozen.read_bytes() == b"MZ old cue"

    (tmp_path / "cue-home" / update.STATE_FILE).unlink()
    for config in ("[updates]\ncheck = false\n", ""):
        if not config:
            monkeypatch.setattr("cue.install.is_frozen", lambda: False)  # running from source
        github = GitHub()
        app = _app(tmp_path, store, github, config)
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(app, pilot)
        assert github.requests == []


# -- cue update -------------------------------------------------------------------------------------------------


def test_cue_update_command(frozen: Path, tmp_path: Path, cue_home: Path, monkeypatch: pytest.MonkeyPatch,
                            capsys: pytest.CaptureFixture[str]) -> None:
    cue_home.mkdir(parents=True, exist_ok=True)
    (cue_home / "config.toml").write_text('[plugins]\ncatalog = ""\n', encoding="utf-8")
    github = GitHub()
    monkeypatch.setattr("cue.commands._client", github.client)
    monkeypatch.setattr("cue.commands.is_frozen", lambda: True)

    assert run_command(["update", "--check"]) == 0
    assert f"Cue {NEW} is available (this is {__version__})" in capsys.readouterr().out
    assert frozen.read_bytes() == b"MZ old cue"

    assert run_command(["update"]) == 0
    out = capsys.readouterr().out
    assert f"Updated Cue {__version__} to {NEW}. Open cue again to use it." in out
    assert "the catalog is turned off" in out and frozen.read_bytes() == EXE_BYTES
    assert not update.due()  # counts as today's check

    monkeypatch.setattr("cue.commands.is_frozen", lambda: False)
    assert run_command(["update"]) == 0
    assert "update it with git pull" in capsys.readouterr().out
