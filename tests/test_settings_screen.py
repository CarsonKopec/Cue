"""The Settings menu: its Subtitles section, and the way to the Plugins menu."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import tomllib
from textual.widgets import Input, Switch

from cue.app import CueApp
from cue.config import ConfigError, load_config, parse_config
from cue.screens.home import HomeScreen
from cue.screens.plugins import PluginsScreen, ProviderSettingsDialog
from cue.screens.settings import SettingsScreen, parse_languages
from cue.store import Store


@pytest.fixture
def config(cue_home: Path, media_dir: Path) -> Path:
    cue_home.mkdir(parents=True, exist_ok=True)
    path = cue_home / "config.toml"
    path.write_text(
        "# my settings\n"
        f'default_provider = "local"\n\n[providers.local]\npath = "{media_dir.as_posix()}"\n\n'
        "[subtitles]\nlanguages = [\"en\"]  # mine\n\n"
        '[subtitles.opensubtitles]\napi_key = "os-key"\n',
        encoding="utf-8")
    return path


def _app(config: Path, store: Store, tmp_path: Path, cue_home: Path) -> CueApp:
    return CueApp(
        load_config(config), store=store,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))),
        dropin_dir=tmp_path / "no-dropins", plugin_root=cue_home / "plugins",
    )


def _saved(config: Path) -> dict:
    return tomllib.loads(config.read_text(encoding="utf-8"))


async def test_subtitles_section_saves_languages_and_burning_in(config: Path, store: Store, tmp_path: Path,
                                                                 cue_home: Path) -> None:
    app = _app(config, store, tmp_path, cue_home)
    assert app.config.subtitles.burn_in  # on unless you turn it off
    async with app.run_test(size=(120, 40)) as pilot:
        assert isinstance(app.screen, HomeScreen)
        await pilot.press("escape", "s")  # out of the search box, then Settings
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("enter")  # Subtitles is first
        dialog = app.screen
        assert isinstance(dialog, ProviderSettingsDialog)
        assert dialog.query_one("#field-languages", Input).value == "en"
        assert dialog.query_one("#field-burn_in", Switch).value is True
        dialog.query_one("#field-languages", Input).value = "en, JA"
        dialog.query_one("#field-burn_in", Switch).value = False
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        saved = _saved(config)
        assert saved["subtitles"]["languages"] == ["en", "ja"] and saved["subtitles"]["burn_in"] is False
        assert saved["subtitles"]["auto_download"] is True
        assert saved["subtitles"]["opensubtitles"] == {"api_key": "os-key"}  # the source's table is untouched
        assert config.read_text(encoding="utf-8").startswith("# my settings\n")
        assert app.config.subtitles.burn_in is False and app.config.subtitles.languages == ["en", "ja"]
        assert app.subtitles is not None and app.subtitles.languages == ["en", "ja"]

        # Something that isn't a language code is refused, and nothing changes.
        await pilot.press("enter")
        app.screen.query_one("#field-languages", Input).value = "english!"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert _saved(config)["subtitles"]["languages"] == ["en", "ja"]

        # The other section opens the Plugins menu.
        await pilot.press("down", "enter")
        assert isinstance(app.screen, PluginsScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, HomeScreen)


async def test_the_command_palette_has_one_way_into_settings(config: Path, store: Store, tmp_path: Path,
                                                            cue_home: Path) -> None:
    app = _app(config, store, tmp_path, cue_home)
    async with app.run_test(size=(120, 40)):
        titles = [command.title for command in app.get_system_commands(app.screen)]
    assert titles.count("Settings") == 1
    assert not any("Plugins" in title for title in titles)  # they're a section of Settings


def test_language_codes() -> None:
    assert parse_languages("en, JA pt-br;fr  en") == ["en", "ja", "pt-br", "fr"]
    assert parse_languages("") == [] and parse_languages(None) == []
    with pytest.raises(ValueError, match="english"):
        parse_languages("en, english")


def test_burn_in_must_be_true_or_false() -> None:
    assert parse_config({"subtitles": {"burn_in": False}}).subtitles.burn_in is False
    assert parse_config({}).subtitles.burn_in is True
    with pytest.raises(ConfigError, match="burn_in"):
        parse_config({"subtitles": {"burn_in": "no"}})
