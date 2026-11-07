"""Plugins: manifests, archives, the catalog, installing and loading, config edits and the CLI."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import uuid
import zipfile
from pathlib import Path

import httpx
import pytest
import tomllib

from cue import providers
from cue.commands import run
from cue.config import (
    ConfigError, edit_config, load_config, set_plugin_disabled, set_provider_enabled, set_provider_options,
)
from cue.player.registry import player_origin, players
from cue.plugins import PluginError, PluginManager, fetch_archive, fetch_catalog
from cue.plugins.actions import after_install, after_remove, find_source, updates
from cue.plugins.archive import extract, pack
from cue.plugins.catalog import parse_catalog
from cue.plugins.manifest import check_compatible, is_newer, parse_manifest, version_key
from cue.providers import create_providers

PROVIDER = '''
from cue.providers import register
from .helper import GREETING

@register
class Demo:
    name = "{provider}"
    title = "Demo"
    greeting = GREETING
    def __init__(self, options): self.options = options
    async def search(self, query): return []
    async def episodes(self, show): return []
    async def stream(self, episode): return []
'''


def write_plugin(folder: Path, name: str, version: str = "1.0.0", *, greeting: str = "hello",
                 body: str | None = None, extra: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plugin.toml").write_text(
        f'[plugin]\nname = "{name}"\nversion = "{version}"\ntitle = "Demo {name}"\n'
        f'description = "A test plugin."\nrequires = "0.2"\nproviders = ["{name}-p"]\n{extra}',
        encoding="utf-8",
    )
    (folder / "__init__.py").write_text(body if body is not None else PROVIDER.format(provider=f"{name}-p"),
                                        encoding="utf-8")
    (folder / "helper.py").write_text(f"GREETING = {greeting!r}\n", encoding="utf-8")
    return folder


@pytest.fixture
def name() -> str:
    return f"t{uuid.uuid4().hex[:8]}"


@pytest.fixture
def manager(tmp_path: Path):
    plugins = PluginManager(tmp_path / "plugins")
    yield plugins
    for plugin in plugins.plugins():
        plugins.unload(plugin.name)


# -- manifests -------------------------------------------------------------------------


def test_manifest_fields_and_defaults() -> None:
    manifest = parse_manifest({"name": "my-plugin", "version": "1.2", "providers": ["a"], "players": ["b"]})
    assert manifest.title == "my-plugin" and manifest.module == "my_plugin" and manifest.requires == "0"
    assert manifest.providers == ("a",) and manifest.players == ("b",)


@pytest.mark.parametrize("table, problem", [
    ({"version": "1.0"}, "name is missing"),
    ({"name": "Bad Name", "version": "1.0"}, "lowercase"),
    ({"name": "1abc", "version": "1.0"}, "lowercase"),
    ({"name": "ok", "version": "1.0-beta"}, "version number"),
    ({"name": "ok", "version": "1.0", "requires": "x"}, "version number"),
    ({"name": "ok", "version": "1.0", "providers": "anime"}, "providers must be a list"),
    ({"name": "ok", "version": "1.0", "players": [1]}, "players must be a list"),
    ({"name": "ok", "version": "1.0", "homepage": "javascript:alert(1)"}, "homepage"),
])
def test_manifest_problems(table: dict, problem: str) -> None:
    with pytest.raises(PluginError, match=problem):
        parse_manifest(table)


def test_versions_compare_numerically() -> None:
    assert version_key("1.2") == (1, 2, 0, 0)
    assert is_newer("1.10.0", "1.9.9") and not is_newer("1.0", "1.0.0")
    with pytest.raises(PluginError, match="needs Cue 99.0"):
        check_compatible(parse_manifest({"name": "future", "version": "1.0", "requires": "99.0"}))


# -- archives -------------------------------------------------------------------------


def test_pack_is_reproducible_and_skips_caches(tmp_path: Path, name: str) -> None:
    folder = write_plugin(tmp_path / "src", name)
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "helper.cpython-314.pyc").write_bytes(b"x")
    (folder / ".git").mkdir()
    (folder / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    first = pack(folder, tmp_path / "a.zip")
    second = pack(folder, tmp_path / "b.zip")
    assert first == second == hashlib.sha256((tmp_path / "a.zip").read_bytes()).hexdigest()
    with zipfile.ZipFile(tmp_path / "a.zip") as bundle:
        assert sorted(bundle.namelist()) == ["__init__.py", "helper.py", "plugin.toml"]


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for path, data in files.items():
            bundle.writestr(path, data)
    return buffer.getvalue()


def test_extract_accepts_a_single_top_folder(tmp_path: Path) -> None:
    data = _zip({"Cue-plugin-main/plugin.toml": b"[plugin]", "Cue-plugin-main/__init__.py": b"",
                 "Cue-plugin-main/sub/x.py": b"x = 1"})
    extract(data, tmp_path / "out")
    assert (tmp_path / "out" / "plugin.toml").is_file() and (tmp_path / "out" / "sub" / "x.py").is_file()


@pytest.mark.parametrize("files, problem", [
    ({"plugin.toml": b"", "../evil.py": b""}, "unsafe path"),
    ({"plugin.toml": b"", "/abs.py": b""}, "unsafe path"),
    ({"plugin.toml": b"", "C:/x.py": b""}, "unsafe path"),
    ({"a/plugin.toml": b"", "b/x.py": b""}, "no plugin.toml"),
    ({"x.py": b""}, "no plugin.toml"),
])
def test_extract_refuses_unsafe_archives(tmp_path: Path, files: dict, problem: str) -> None:
    with pytest.raises(PluginError, match=problem):
        extract(_zip(files), tmp_path / "out")
    assert not (tmp_path / "evil.py").exists()


def test_extract_refuses_backslash_names(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("plugin.toml", b"")
        sneaky = zipfile.ZipInfo("placeholder")
        sneaky.filename = "..\\evil.py"  # set after creation: ZipInfo turns \ into / on Windows
        bundle.writestr(sneaky, b"")
    with pytest.raises(PluginError, match="unsafe path"):
        extract(buffer.getvalue(), tmp_path / "out")


def test_extract_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PluginError, match="not a zip"):
        extract(b"not a zip", tmp_path / "out")
    monkeypatch.setattr("cue.plugins.archive.MAX_UNPACKED_BYTES", 100)
    with pytest.raises(PluginError, match="too much data"):
        extract(_zip({"plugin.toml": b"x" * 200}), tmp_path / "out")
    link = zipfile.ZipInfo("plugin.toml")
    link.external_attr = 0o120777 << 16
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(link, "/etc/passwd")
    with pytest.raises(PluginError, match="link"):
        extract(buffer.getvalue(), tmp_path / "out")


# -- installing and loading ----------------------------------------------------------------


def test_install_from_folder_loads_providers(tmp_path: Path, manager: PluginManager, name: str) -> None:
    plugin = manager.install(write_plugin(tmp_path / "src", name), "a folder")
    assert plugin.loaded and plugin.error is None and plugin.providers == (f"{name}-p",)
    assert providers.origin_of(f"{name}-p") == f"plugin:{name}"
    assert providers.discover()[f"{name}-p"].greeting == "hello"  # relative import of helper.py worked
    assert plugin.source == "a folder" and (manager.root / name / "helper.py").is_file()
    assert sys.modules[f"cue_plugins.{name}.helper"].GREETING == "hello"


def test_update_replaces_code_and_failed_install_keeps_the_old(tmp_path: Path, manager: PluginManager,
                                                              name: str) -> None:
    manager.install(write_plugin(tmp_path / "v1", name), "v1")
    pack(write_plugin(tmp_path / "v2", name, "2.0.0", greeting="hi again"), tmp_path / "v2.zip")
    plugin = manager.install(tmp_path / "v2.zip", "v2")
    assert plugin.manifest.version == "2.0.0"
    assert providers.discover()[f"{name}-p"].greeting == "hi again"  # helper.py was re-imported too

    broken = write_plugin(tmp_path / "v3", name, "3.0.0")
    (broken / "__init__.py").unlink()
    with pytest.raises(PluginError, match="no __init__.py"):
        manager.install(broken, "v3")
    assert manager.get(name).manifest.version == "2.0.0" and manager.get(name).loaded
    assert not [p for p in manager.root.iterdir() if p.name.startswith(".")]


def test_disabled_and_broken_plugins(tmp_path: Path, name: str) -> None:
    root = tmp_path / "plugins"
    write_plugin(root / f"{name}a", f"{name}a")
    write_plugin(root / f"{name}b", f"{name}b", body="raise RuntimeError('boom')\n")
    (root / "bad").mkdir()
    (root / "bad" / "plugin.toml").write_text("[plugin]\nname = 'Bad'\n", encoding="utf-8")
    plugins = PluginManager(root, disabled={f"{name}a"})
    try:
        loaded = {plugin.name: plugin for plugin in plugins.load_enabled()}
        assert not loaded[f"{name}a"].loaded and f"{name}a-p" not in providers.discover()
        assert "RuntimeError: boom" in loaded[f"{name}b"].error and not loaded[f"{name}b"].loaded
        assert not loaded["bad"].valid and "lowercase" in loaded["bad"].error
        assert f"cue_plugins.{name}b" not in sys.modules

        assert plugins.set_enabled(f"{name}a", True).loaded
        assert f"{name}a-p" in providers.discover()
        plugins.set_enabled(f"{name}a", False)
        assert f"{name}a-p" not in providers.discover() and f"{name}a" in plugins.disabled
    finally:
        for plugin in plugins.plugins():
            plugins.unload(plugin.name)


def test_remove_and_clean_up(tmp_path: Path, manager: PluginManager, name: str) -> None:
    manager.install(write_plugin(tmp_path / "src", name), "src")
    assert manager.remove(name) == [f"{name}-p"]
    assert not (manager.root / name).exists() and f"{name}-p" not in providers.discover()
    with pytest.raises(PluginError, match="isn't installed"):
        manager.remove(name)
    (manager.root / ".install-x").mkdir()
    (manager.root / ".old-y-1").mkdir()
    manager.clean_up()
    assert list(manager.root.iterdir()) == []


def test_lib_folder_and_players(tmp_path: Path, manager: PluginManager, name: str) -> None:
    folder = write_plugin(tmp_path / "src", name, body=(
        "import vendored_dep\n"
        "from cue.player.registry import PlayerBackend, register_player\n"
        f"register_player(PlayerBackend('{name}-player', 'Test', lambda o: 'prog', lambda p, o: None))\n"
    ), extra=f'players = ["{name}-player"]\n')
    (folder / "lib").mkdir()
    (folder / "lib" / "vendored_dep.py").write_text("VALUE = 1\n", encoding="utf-8")
    plugin = manager.install(folder, "src")
    assert plugin.players == (f"{name}-player",) and player_origin(f"{name}-player") == f"plugin:{name}"
    manager.unload(name)
    assert f"{name}-player" not in players()


def test_plugin_needing_newer_cue_is_refused(tmp_path: Path, manager: PluginManager, name: str) -> None:
    folder = write_plugin(tmp_path / "src", name)
    toml = (folder / "plugin.toml").read_text(encoding="utf-8").replace('requires = "0.2"', 'requires = "9.0"')
    (folder / "plugin.toml").write_text(toml, encoding="utf-8")
    with pytest.raises(PluginError, match="needs Cue 9.0"):
        manager.install(folder, "src")
    assert not (manager.root / name).exists()


# -- catalog ------------------------------------------------------------------------------


def _entry(name: str, url: str, sha: str = "a" * 64, version: str = "1.0.0") -> dict:
    return {"name": name, "version": version, "url": url, "sha256": sha, "providers": [f"{name}-p"]}


def test_parse_catalog_resolves_links_and_skips_bad_entries() -> None:
    entries = parse_catalog({"plugins": [
        _entry("rel", "rel-1.0.0.zip"),
        _entry("abs", "https://cdn.example/abs.zip"),
        _entry("plain", "http://example.com/plain.zip"),  # not https
        _entry("nosha", "x.zip", sha="short"),
        {"name": "Bad", "version": "1"},
        "junk",
    ]}, "https://github.com/o/r/releases/download/plugins/index.json")
    assert [(e.manifest.name, e.url) for e in entries] == [
        ("abs", "https://cdn.example/abs.zip"),
        ("rel", "https://github.com/o/r/releases/download/plugins/rel-1.0.0.zip"),
    ]
    with pytest.raises(PluginError, match="no plugins list"):
        parse_catalog({"nope": []}, "https://x/index.json")


async def test_fetch_catalog_and_archive(tmp_path: Path) -> None:
    archive = _zip({"plugin.toml": b"[plugin]"})
    digest = hashlib.sha256(archive).hexdigest()
    index = {"plugins": [_entry("demo", "demo.zip", digest)]}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, json=index)
        if request.url.path.endswith("demo.zip"):
            return httpx.Response(200, content=archive)
        if request.url.path.endswith("downgrade.zip"):
            return httpx.Response(302, headers={"Location": "http://evil.example/x.zip"})
        if request.url.path.endswith("moved.zip"):  # as GitHub release downloads do
            return httpx.Response(302, headers={"Location": "https://objects.example/files/demo.zip"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        entries = await fetch_catalog(http, "https://example.test/plugins/index.json")
        assert entries[0].url == "https://example.test/plugins/demo.zip"
        assert await fetch_archive(http, entries[0].url, digest) == archive
        assert await fetch_archive(http, "https://example.test/moved.zip", digest) == archive
        with pytest.raises(PluginError, match="checksum"):
            await fetch_archive(http, entries[0].url, "b" * 64)
        with pytest.raises(PluginError, match="HTTP 404"):
            await fetch_catalog(http, "https://example.test/missing.json")
        with pytest.raises(PluginError, match="only downloaded over https"):
            await fetch_archive(http, "https://example.test/downgrade.zip")
        with pytest.raises(PluginError, match="only downloaded over https"):
            await fetch_catalog(http, "http://example.test/index.json")

    local = tmp_path / "catalog" / "index.json"
    local.parent.mkdir()
    local.write_text(json.dumps(index), encoding="utf-8")
    async with httpx.AsyncClient() as http:
        entries = await fetch_catalog(http, str(local))
    assert entries[0].url == str(local.parent / "demo.zip")


def test_find_source_and_updates(tmp_path: Path) -> None:
    catalog = parse_catalog({"plugins": [_entry("demo", "demo-2.0.0.zip", version="2.0.0")]},
                            "https://example.test/index.json")
    assert find_source("Demo", catalog).entry is catalog[0]
    assert find_source("https://example.test/x.zip", catalog).location == "https://example.test/x.zip"
    assert find_source(str(tmp_path), catalog).location == str(tmp_path.resolve())
    with pytest.raises(PluginError, match="only downloaded over https"):
        find_source("http://example.test/x.zip", catalog)
    with pytest.raises(PluginError, match="no plugin called 'nope'"):
        find_source("nope", catalog)
    with pytest.raises(PluginError, match="catalog couldn't be loaded"):
        find_source("nope", None)

    root = tmp_path / "plugins"
    write_plugin(root / "demo", "demo", "1.5.0")
    write_plugin(root / "other", "other")
    assert set(updates(PluginManager(root).scan(), catalog)) == {"demo"}


# -- providers and config --------------------------------------------------------------------


def test_create_providers_respects_enabled_missing_and_reuse(media_dir: Path) -> None:
    settings = {"local": {"path": str(media_dir)}, "jellyfin": {"url": "x", "enabled": False},
                "not-installed": {}}
    ready, problems = create_providers(settings)
    assert list(ready) == ["local"] and "enabled" not in ready["local"].roots
    assert problems == {"not-installed": "not installed; press p on the Home screen to find its plugin"}

    again, _ = create_providers(settings, reuse={"local": (ready["local"], {"path": str(media_dir)})})
    assert again["local"] is ready["local"]
    changed, _ = create_providers({"local": {"path": str(media_dir / "x")}},
                                  reuse={"local": (ready["local"], {"path": str(media_dir)})})
    assert changed["local"] is not ready["local"]


def test_config_edits_keep_comments_and_refuse_invalid_results(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '# my settings\nplayer = "vlc"  # the player\n\n[providers.local]\npath = "D:/Shows"  # media\n',
        encoding="utf-8",
    )
    config = edit_config(path, set_provider_options("jellyfin", {"url": "http://x", "api_key": "k"}),
                         set_provider_enabled("local", False), set_plugin_disabled("demo", True))
    text = path.read_text(encoding="utf-8")
    assert "# my settings" in text and "# the player" in text and "# media" in text
    assert config.providers["local"]["enabled"] is False and config.plugins.disabled == ["demo"]
    assert config.providers["jellyfin"] == {"url": "http://x", "api_key": "k"}
    assert "[providers.jellyfin]\nurl" in text and "\n\n[plugins]" in text

    config = edit_config(path, set_provider_enabled("local", True), set_provider_options("jellyfin", {"url": None}))
    assert "enabled" not in config.providers["local"] and config.providers["jellyfin"] == {"api_key": "k"}

    before = path.read_text(encoding="utf-8")
    with pytest.raises(ConfigError, match="enabled has the wrong type"):
        edit_config(path, set_provider_options("local", {"enabled": "yes"}))
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_config_edits_keep_line_endings(tmp_path: Path, newline: str) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(newline.join(["# mine", 'player = "vlc"', "", "[providers.local]", 'path = ""', ""]).encode())
    edit_config(path, set_provider_enabled("local", True))
    data = path.read_bytes()
    assert b"\r\r\n" not in data
    assert data.count(b"\r\n") == (data.count(b"\n") if newline == "\r\n" else 0)


def test_after_install_and_remove_changes(tmp_path: Path, manager: PluginManager, name: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f'player = "gone"\n[plugins]\ndisabled = ["{name}"]\n', encoding="utf-8")
    config = load_config(path)
    folder = write_plugin(tmp_path / "src", name, body=(
        PROVIDER.format(provider=f"{name}-p")
        + "from cue.player.registry import PlayerBackend, register_player\n"
        + f"register_player(PlayerBackend('{name}-player', 'T', lambda o: 'p', lambda p, o: None))\n"
    ))
    plugin = manager.install(folder, "src")
    config = edit_config(path, *after_install(plugin, config))
    assert config.player == f"{name}-player" and config.plugins.disabled == []
    assert config.providers[f"{name}-p"] == {}

    provided = manager.remove(name)
    config = edit_config(path, *after_remove(plugin, provided, config))
    assert config.player == "vlc" and config.providers[f"{name}-p"] == {"enabled": False}


# -- the command line -----------------------------------------------------------------------


def test_cli_installs_lists_disables_and_removes(tmp_path: Path, cue_home: Path, name: str,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    config = cue_home / "config.toml"
    archive = tmp_path / f"{name}.zip"
    pack(write_plugin(tmp_path / "src", name), archive)
    try:
        assert run(["plugin", "install", str(archive), "--yes"]) == 0
        assert f"Installed Demo {name} 1.0.0 (adds {name}-p)" in capsys.readouterr().out
        assert (cue_home / "plugins" / name / "plugin.toml").is_file()
        assert tomllib.loads(config.read_text(encoding="utf-8"))["providers"][f"{name}-p"] == {}

        assert run(["plugin", "list"]) == 0
        assert f"{name}" in capsys.readouterr().out
        assert run(["plugin", "disable", name]) == 0
        assert load_config(config).plugins.disabled == [name]
        assert run(["plugin", "enable", name]) == 0
        assert load_config(config).plugins.disabled == []

        assert run(["plugin", "remove", name]) == 0
        assert not (cue_home / "plugins" / name).exists()
        assert load_config(config).providers[f"{name}-p"] == {"enabled": False}
        assert run(["plugin", "remove", name]) == 1
        assert "isn't installed" in capsys.readouterr().err
    finally:
        PluginManager(cue_home / "plugins").load_enabled()
        providers.unregister(f"plugin:{name}")


def test_cli_installs_and_updates_from_a_local_catalog(tmp_path: Path, cue_home: Path, name: str,
                                                      monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    catalog = tmp_path / "catalog"
    sha = pack(write_plugin(tmp_path / "v1", name), catalog / f"{name}-1.0.0.zip")
    (catalog / "index.json").write_text(json.dumps({"plugins": [_entry(name, f"{name}-1.0.0.zip", sha)]}),
                                        encoding="utf-8")
    cue_home.mkdir(parents=True, exist_ok=True)
    (cue_home / "config.toml").write_text(
        f'[plugins]\ncatalog = "{(catalog / "index.json").as_posix()}"\n', encoding="utf-8")
    try:
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert run(["plugin", "install", name]) == 1
        assert "Nothing installed" in capsys.readouterr().out

        monkeypatch.setattr("builtins.input", lambda prompt: "y")
        assert run(["plugin", "install", name]) == 0
        assert run(["plugin", "update"]) == 0
        assert "Everything is up to date" in capsys.readouterr().out

        sha = pack(write_plugin(tmp_path / "v2", name, "1.1.0"), catalog / f"{name}-1.1.0.zip")
        (catalog / "index.json").write_text(
            json.dumps({"plugins": [_entry(name, f"{name}-1.1.0.zip", sha, "1.1.0")]}), encoding="utf-8")
        assert run(["plugin", "catalog"]) == 0
        assert "(update from 1.0.0)" in capsys.readouterr().out
        assert run(["plugin", "update", "--yes"]) == 0
        assert PluginManager(cue_home / "plugins").scan()[0].manifest.version == "1.1.0"
    finally:
        providers.unregister(f"plugin:{name}")
