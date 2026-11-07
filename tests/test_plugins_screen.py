"""The Plugins menu: providers on/off and settings, choosing a player, installing and removing plugins."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
import tomllib
from textual.widgets import DataTable, Input, TabbedContent

from cue import providers
from cue.app import CueApp
from cue.config import load_config
from cue.plugins.archive import pack
from cue.screens.plugins import PLAYERS, PLUGINS, AddPluginDialog, ConfirmDialog, PluginsScreen, ProviderSettingsDialog
from cue.store import Store

PLUGIN = '''
from cue.providers import register

@register
class Demo:
    name = "{name}-p"
    title = "Demo"
    def __init__(self, options): pass
    async def search(self, query): return []
    async def episodes(self, show): return []
    async def stream(self, episode): return []
'''


@pytest.fixture
def setup(tmp_path: Path, cue_home: Path, media_dir: Path):
    name = f"t{uuid.uuid4().hex[:8]}"
    source = tmp_path / "src"
    source.mkdir()
    (source / "plugin.toml").write_text(
        f'[plugin]\nname = "{name}"\nversion = "1.0.0"\ntitle = "Demo plugin"\nproviders = ["{name}-p"]\n',
        encoding="utf-8")
    (source / "__init__.py").write_text(PLUGIN.format(name=name), encoding="utf-8")
    catalog = tmp_path / "catalog"
    sha = pack(source, catalog / f"{name}-1.0.0.zip")
    (catalog / "index.json").write_text(json.dumps({"plugins": [{
        "name": name, "version": "1.0.0", "title": "Demo plugin", "providers": [f"{name}-p"],
        "url": f"{name}-1.0.0.zip", "sha256": sha,
    }]}), encoding="utf-8")
    cue_home.mkdir(parents=True, exist_ok=True)
    config = cue_home / "config.toml"
    config.write_text(
        "# my settings\n"
        f'default_provider = "jellyfin"\n\n[providers.local]\npath = "{media_dir.as_posix()}"\n\n'
        '[providers.jellyfin]\nurl = ""\napi_key = ""\n\n'
        f'[plugins]\ncatalog = "{(catalog / "index.json").as_posix()}"\n',
        encoding="utf-8")
    yield name, config, catalog / f"{name}-1.0.0.zip"
    providers.unregister(f"plugin:{name}")


def _app(config: Path, store: Store, tmp_path: Path, cue_home: Path) -> CueApp:
    return CueApp(
        load_config(config), store=store,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))),
        dropin_dir=tmp_path / "no-dropins", plugin_root=cue_home / "plugins",
    )


def _saved(config: Path) -> dict:
    return tomllib.loads(config.read_text(encoding="utf-8"))


def _select(screen: PluginsScreen, table: str, rows: list[str], name: str) -> None:
    screen.query_one(table, DataTable).move_cursor(row=rows.index(name))


async def _settle(app: CueApp, pilot) -> None:
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


async def test_providers_and_players_tabs(setup, store: Store, tmp_path: Path, cue_home: Path) -> None:
    name, config, _ = setup
    app = _app(config, store, tmp_path, cue_home)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape", "p")
        screen = app.screen
        assert isinstance(screen, PluginsScreen)
        await _settle(app, pilot)
        assert "jellyfin" in app.provider_problems and "local" in app.providers

        _select(screen, "#provider-table", screen._provider_rows, "local")
        await pilot.press("space")
        assert _saved(config)["providers"]["local"]["enabled"] is False and "local" not in app.providers
        await pilot.press("space")
        assert "enabled" not in _saved(config)["providers"]["local"] and "local" in app.providers
        await pilot.press("d")
        assert _saved(config)["default_provider"] == "local" and app.provider_name == "local"

        _select(screen, "#provider-table", screen._provider_rows, "jellyfin")
        await pilot.press("enter")
        dialog = app.screen
        assert isinstance(dialog, ProviderSettingsDialog)
        dialog.query_one("#field-url", Input).value = "http://192.168.1.10:8096"
        await pilot.click("#save")
        assert "API key is required" in str(dialog.query_one("#form-error").render())
        dialog.query_one("#field-api_key", Input).value = "secret"
        await pilot.pause(0.3)  # Textual ignores a second click on a button within 0.2 s
        await pilot.click("#save")
        await pilot.pause()
        assert app.screen is screen
        saved = _saved(config)
        assert saved["providers"]["jellyfin"] == {"url": "http://192.168.1.10:8096", "api_key": "secret"}
        assert "jellyfin" in app.providers and "user_id" not in saved["providers"]["jellyfin"]
        assert config.read_text(encoding="utf-8").startswith("# my settings")

        await pilot.press("right")
        assert screen.query_one(TabbedContent).active == PLAYERS
        _select(screen, "#player-table", screen._player_rows, "mpv")
        await pilot.press("d")
        assert _saved(config)["player"] == "mpv" and app.config.player == "mpv"
        await pilot.press("enter")
        assert isinstance(app.screen, ProviderSettingsDialog)
        app.screen.query_one("#field-path", Input).value = str(tmp_path / "mpv.exe")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert _saved(config)["players"]["mpv"] == {"path": str(tmp_path / "mpv.exe")}


async def test_plugins_tab_installs_toggles_and_removes(setup, store: Store, tmp_path: Path, cue_home: Path) -> None:
    name, config, archive = setup
    provider = f"{name}-p"
    app = _app(config, store, tmp_path, cue_home)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape", "p")
        screen = app.screen
        await _settle(app, pilot)
        screen.query_one(TabbedContent).active = PLUGINS
        await pilot.pause()
        assert name in screen._plugin_rows
        assert "catalog lists 1 plugin" in str(screen.query_one("#plugin-detail").render())

        _select(screen, "#plugin-table", screen._plugin_rows, name)
        await pilot.press("enter")
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("y")
        await _settle(app, pilot)
        assert (cue_home / "plugins" / name / "__init__.py").is_file()
        assert provider in app.providers and _saved(config)["providers"][provider] == {}

        _select(screen, "#plugin-table", screen._plugin_rows, name)
        await pilot.press("space")
        assert _saved(config)["plugins"]["disabled"] == [name]
        assert provider not in app.providers and provider not in app.provider_problems
        await pilot.press("space")
        assert _saved(config)["plugins"]["disabled"] == [] and provider in app.providers

        await pilot.press("x")
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("y")
        await _settle(app, pilot)
        assert not (cue_home / "plugins" / name).exists()
        assert provider not in app.providers and _saved(config)["providers"][provider] == {"enabled": False}

        await pilot.press("a")
        assert isinstance(app.screen, AddPluginDialog)
        app.screen.query_one(Input).value = str(archive)
        await pilot.press("enter")
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("y")
        await _settle(app, pilot)
        assert provider in app.providers and "enabled" not in _saved(config)["providers"][provider]
        plugin = app.plugins.get(name)
        assert plugin is not None and plugin.source == str(archive.resolve())


async def test_plugins_menu_reports_an_unavailable_catalog(setup, store: Store, tmp_path: Path,
                                                           cue_home: Path) -> None:
    name, config, _ = setup
    config.write_text(config.read_text(encoding="utf-8").replace(
        'catalog = "', 'catalog = "https://example.test/x/'), encoding="utf-8")
    app = _app(config, store, tmp_path, cue_home)
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_plugins()
        await _settle(app, pilot)
        screen = app.screen
        screen.query_one(TabbedContent).active = PLUGINS
        await pilot.pause()
        assert "Catalog unavailable" in str(screen.query_one("#plugin-detail").render())
        await pilot.press("enter")  # nothing selected: no crash
