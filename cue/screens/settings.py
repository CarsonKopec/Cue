"""Settings: Cue's own settings, a section at a time. Plugins, providers and players have their own menu."""

from __future__ import annotations

import re
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Label, Static
from textual.widgets.option_list import Option

from cue.config import set_subtitle_settings
from cue.providers.base import Setting
from cue.screens import CueScreen
from cue.screens.plugins import ProviderSettingsDialog
from cue.widgets import StatusBar, VimOptionList

SUBTITLES, PLUGINS = "subtitles", "plugins"

SUBTITLE_SETTINGS = (
    Setting("languages", "Languages", default="en",
            help="Two-letter codes in order of preference, separated by commas, such as en, ja. "
                 "pt-br prefers that region; pt takes any."),
    Setting("auto_download", "Load subtitles by themselves", "bool", True,
            help="Off: only the ones you pick with Shift+C while watching."),
    Setting("burn_in", "Burn them into the picture for the Default app", "bool", True,
            help="Media Player won't switch subtitles on by itself, so Cue draws your first language onto the "
                 "video as it downloads, with the graphics card when it can. They always show, and can't be "
                 "turned off while watching.\nOff: they go inside the file instead, every language, and you "
                 "switch them on with Media Player's subtitles button each time."),
)

_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*")


def parse_languages(text: str | None) -> list[str]:
    """``"en, ja"`` as ``["en", "ja"]``; raises ValueError on anything that isn't a language code."""
    languages = [part.lower() for part in re.split(r"[\s,;]+", text or "") if part]
    wrong = [language for language in languages if not _LANGUAGE.fullmatch(language)]
    if wrong:
        raise ValueError(f"{', '.join(wrong)} isn't a language code; use two letters, such as en or ja.")
    return list(dict.fromkeys(languages))


class SettingsScreen(CueScreen):
    BINDINGS = [Binding("q,escape", "back", "Back")]

    SECTIONS = (
        (SUBTITLES, "Subtitles", "Your languages, loading them by themselves, burning them in for the Default app"),
        (PLUGINS, "Plugins, providers and players",
         "Where shows come from, which player plays them, and where subtitles come from"),
    )

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(classes="view"):
            yield Label("Settings", classes="section")
            yield VimOptionList(*(Option(self._describe(title, about), id=key) for key, title, about in self.SECTIONS),
                                id="settings-sections")
            yield Static("Enter opens a section. Settings are saved in config.toml, keeping your comments.",
                         id="settings-hint", classes="hint")
        yield StatusBar()
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Settings"
        sections = self.query_one("#settings-sections", VimOptionList)
        sections.highlighted = 0
        sections.focus()

    @staticmethod
    def _describe(title: str, about: str) -> Text:
        text = Text(title, style="bold")
        text.append(f"\n  {about}", style="dim")
        return text

    def on_option_list_option_selected(self, event: VimOptionList.OptionSelected) -> None:
        if event.option.id == SUBTITLES:
            self.edit_subtitles()
        elif event.option.id == PLUGINS:
            self.cue.open_plugins()

    def edit_subtitles(self) -> None:
        current = self.cue.config.subtitles
        values: dict[str, Any] = {
            "languages": ", ".join(current.languages),
            "auto_download": current.auto_download,
            "burn_in": current.burn_in,
        }

        def saved(result: dict[str, Any] | None) -> None:
            if result is None:
                return
            try:
                languages = parse_languages(result.get("languages"))
            except ValueError as exc:
                self.notify(str(exc), title="Subtitles not saved", severity="error", timeout=8)
                return
            change = set_subtitle_settings({
                "languages": languages or None,  # none given: back to English
                "auto_download": bool(result.get("auto_download", True)),
                "burn_in": bool(result.get("burn_in", True)),
            })
            if self.cue.update_config(change):
                self.notify("Subtitle settings saved.")

        note = "Where subtitles come from (OpenSubtitles, SubDL, Addic7ed) is under Plugins → Subtitles."
        self.app.push_screen(ProviderSettingsDialog("Subtitles", SUBTITLE_SETTINGS, values, note), saved)

    def action_back(self) -> None:
        self.app.pop_screen()
