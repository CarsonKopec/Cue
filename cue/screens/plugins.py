"""Plugins, providers and players: turn providers on or off, pick a player, install plugins."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Header, Input, Label, Select, Static, Switch, TabbedContent, TabPane,
)

from cue.config import (
    set_default_provider, set_player, set_player_options, set_plugin_disabled, set_provider_enabled,
    set_provider_options,
)
from cue.player.registry import PATH_SETTING, player_origin, players
from cue.plugins import CatalogEntry, InstalledPlugin, Manifest, PluginError, fetch_catalog
from cue.plugins.actions import WARNING, Source, after_install, after_remove, fetch, find_source, updates
from cue.providers import BUILT_IN, discover, is_enabled, origin_of
from cue.providers.base import Setting
from cue.screens import CueScreen
from cue.widgets import StatusBar

PROVIDERS, PLAYERS, PLUGINS = "providers-tab", "players-tab", "plugins-tab"
TABS = [PROVIDERS, PLAYERS, PLUGINS]


# -- dialogs ------------------------------------------------------------------------


class ConfirmDialog(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "answer(True)", show=False),
        Binding("n,escape", "answer(False)", "Cancel"),
    ]

    def __init__(self, title: str, message: str, confirm: str) -> None:
        super().__init__()
        self._title, self._message, self._confirm = title, message, confirm

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label(self._title, classes="section")
            yield Static(self._message, classes="dialog-text")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self._confirm, variant="primary", id="confirm")

    def on_mount(self) -> None:
        self.query_one("#confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


class AddPluginDialog(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label("Add a plugin", classes="section")
            yield Static("A name from the catalog, a link to a plugin .zip, or a .zip file or folder on this PC.",
                         classes="hint")
            yield Input(placeholder="anime · https://…/plugin.zip · C:\\Downloads\\plugin.zip", id="plugin-source")

    def on_mount(self) -> None:
        self.query_one("#plugin-source", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _settings_for(cls: type | None, table: Mapping[str, Any]) -> tuple[tuple[Setting, ...], bool]:
    """The provider's declared settings, or ones guessed from its current table (``False``)."""
    declared = tuple(getattr(cls, "settings", ()) or ())
    if declared:
        return declared, True
    guessed = []
    for key, value in table.items():
        if key == "enabled":
            continue
        if isinstance(value, bool):
            guessed.append(Setting(key, key, "bool", value))
        elif isinstance(value, (int, float)):
            guessed.append(Setting(key, key, "number", value))
        elif isinstance(value, str):
            guessed.append(Setting(key, key, "text", ""))
    return tuple(guessed), False


def _display(setting: Setting, value: Any) -> str:
    if value is None:
        return ""
    if setting.kind == "folders" and isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return str(value)


class ProviderSettingsDialog(ModalScreen[dict[str, Any] | None]):
    """A form for one ``[providers.<name>]`` table; returns the values to save."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, title: str, settings: tuple[Setting, ...], values: Mapping[str, Any], note: str = "") -> None:
        super().__init__()
        self._title = title
        self._settings = settings
        self._values = dict(values)
        self._note = note

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label(f"{self._title} settings", classes="section")
            if self._note:
                yield Static(self._note, classes="warning")
            for setting in self._settings:
                value = self._values.get(setting.key)
                yield Label(setting.label + (" *" if setting.required else ""), classes="field")
                field_id = f"field-{setting.key}"
                if setting.kind == "bool":
                    yield Switch(value=bool(setting.default if value is None else value), id=field_id)
                elif setting.kind == "choice" and setting.choices:
                    current = value if value in setting.choices else setting.default
                    yield Select([(choice, choice) for choice in setting.choices], value=current,
                                 allow_blank=False, id=field_id)
                else:
                    yield Input(
                        value=_display(setting, value),
                        placeholder=_display(setting, setting.default),
                        password=setting.kind == "secret",
                        id=field_id,
                    )
                if setting.help:
                    yield Static(setting.help, classes="hint")
            if not self._settings:
                yield Static("This provider has no settings to edit.", classes="hint")
            yield Static("", id="form-error", classes="error")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    def on_mount(self) -> None:
        fields = self.query(".dialog Input, .dialog Select, .dialog Switch")
        (fields.first() if fields else self.query_one("#save", Button)).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        try:
            values = {setting.key: self._read(setting) for setting in self._settings}
        except ValueError as exc:
            self.query_one("#form-error", Static).update(str(exc))
            return
        self.dismiss(values)

    def _read(self, setting: Setting) -> Any:
        """The value to store, or ``None`` to leave the key out (the provider's default)."""
        widget = self.query_one(f"#field-{setting.key}")
        if isinstance(widget, Switch):
            return widget.value
        if isinstance(widget, Select):
            return widget.value
        assert isinstance(widget, Input)
        text = widget.value.strip()
        if not text:
            if setting.required:
                raise ValueError(f"{setting.label} is required.")
            return None
        if setting.kind == "number":
            try:
                number = float(text)
            except ValueError:
                raise ValueError(f"{setting.label} must be a number.") from None
            return int(number) if number.is_integer() else number
        if setting.kind == "folders":
            folders = [part.strip().strip('"') for part in text.split(";") if part.strip()]
            return folders[0] if len(folders) == 1 else folders
        return text


# -- the screen ---------------------------------------------------------------------


def _adds(manifest: Manifest) -> str:
    """What a plugin adds, for the Plugins table: its providers, then its players."""
    return ", ".join([*manifest.providers, *(f"{name} (player)" for name in manifest.players)])


class PluginsScreen(CueScreen):
    BINDINGS = [
        Binding("right", "switch_tab(1)", "Next tab", priority=True),
        Binding("left", "switch_tab(-1)", show=False, priority=True),
        # Providers tab
        Binding("enter", "settings", "Settings", priority=True),
        Binding("space", "toggle_provider", "On/off"),
        Binding("d", "make_default", "Default"),
        # Players tab
        Binding("enter", "player_settings", "Settings", priority=True),
        Binding("d", "use_player", "Use this player"),
        # Plugins tab
        Binding("enter", "install", "Install", priority=True),
        Binding("space", "toggle_plugin", "On/off"),
        Binding("a", "add", "Add file"),
        Binding("x,delete", "remove", "Remove"),
        Binding("r", "reload_catalog", "Refresh"),
        Binding("q,escape", "back", "Back"),
    ]
    PROVIDER_ACTIONS = {"settings", "toggle_provider", "make_default"}
    PLAYER_ACTIONS = {"player_settings", "use_player"}
    PLUGIN_ACTIONS = {"install", "toggle_plugin", "add", "remove", "reload_catalog"}

    def __init__(self) -> None:
        super().__init__()
        self._provider_rows: list[str] = []
        self._player_rows: list[str] = []
        self._plugin_rows: list[str] = []
        self._catalog_note = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="plugins-view", classes="view"):
            with TabPane("Providers", id=PROVIDERS):
                yield DataTable(id="provider-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="provider-detail", classes="hint")
            with TabPane("Players", id=PLAYERS):
                yield DataTable(id="player-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="player-detail", classes="hint")
            with TabPane("Plugins", id=PLUGINS):
                yield DataTable(id="plugin-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="plugin-detail", classes="hint")
        yield StatusBar()
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Plugins, providers and players"
        self.query_one("#provider-table", DataTable).add_columns("Provider", "From", "Status")
        self.query_one("#player-table", DataTable).add_columns("Player", "From", "Status")
        self.query_one("#plugin-table", DataTable).add_columns("Plugin", "Version", "Adds", "Status")
        self.refresh_tables()
        self.query_one("#provider-table", DataTable).focus()
        if self.cue.catalog is None:
            self.action_reload_catalog()

    # -- state ------------------------------------------------------------------------

    @property
    def _tab(self) -> str:
        return self.query_one(TabbedContent).active

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in self.PROVIDER_ACTIONS:
            return self._tab == PROVIDERS
        if action in self.PLAYER_ACTIONS:
            return self._tab == PLAYERS
        if action in self.PLUGIN_ACTIONS:
            return self._tab == PLUGINS
        return True

    @on(TabbedContent.TabActivated)
    def _tab_changed(self, event: TabbedContent.TabActivated) -> None:
        table_id = {PROVIDERS: "#provider-table", PLAYERS: "#player-table"}.get(event.pane.id, "#plugin-table")
        self.query_one(table_id, DataTable).focus()
        self.refresh_bindings()

    def action_switch_tab(self, step: int) -> None:
        self.query_one(TabbedContent).active = TABS[(TABS.index(self._tab) + step) % len(TABS)]

    def action_back(self) -> None:
        self.app.pop_screen()

    def _selected(self, table_id: str, rows: list[str]) -> str | None:
        table = self.query_one(table_id, DataTable)
        return rows[table.cursor_row] if table.row_count and 0 <= table.cursor_row < len(rows) else None

    # -- tables ---------------------------------------------------------------------

    def _provider_names(self) -> list[str]:
        installed = discover()
        hidden = {name for plugin in self.cue.plugins.plugins() for name in plugin.manifest.providers}
        return sorted(set(installed) | set(self.cue.config.providers) | hidden)

    def _source(self, origin: str | None) -> str:
        if origin == BUILT_IN:
            return "built-in"
        if origin and origin.startswith("plugin:"):
            plugin = self.cue.plugins.get(origin.removeprefix("plugin:"))
            return f"plugin {plugin.name} {plugin.manifest.version}" if plugin else origin
        if origin and origin.startswith("package:"):
            return f"package {origin.removeprefix('package:')}"
        if origin and origin.startswith("file:"):
            return origin.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        return ""

    def _provider_source(self, name: str) -> str:
        source = self._source(origin_of(name))
        if source:
            return source
        owner = next((p for p in self.cue.plugins.plugins() if name in p.manifest.providers), None)
        return f"plugin {owner.name} (off)" if owner else "not installed"

    def _player_status(self, name: str) -> tuple[Text, str]:
        """The status cell, and the detail line explaining it."""
        backend = players().get(name)
        in_use = name == self.cue.config.player
        if backend is None:
            owner = next((p for p in self.cue.plugins.plugins() if name in p.manifest.players), None)
            reason = f"Its plugin, {owner.name}, is turned off." if owner else "Install the plugin that adds it."
            return Text("in use · not installed" if in_use else "not installed", style="red"), reason
        if not backend.supported:
            return Text("not for this system", style="dim"), f"{backend.title} runs on {', '.join(backend.platforms)}."
        program = backend.program(self.cue.config.players.get(name, {}))
        if program is None:
            return Text("in use · not found" if in_use else "not found", style="yellow"), backend.missing
        return Text("● in use" if in_use else "found", style="green"), program

    def _provider_status(self, name: str) -> Text:
        table = self.cue.config.providers.get(name)
        if name in self.cue.providers:
            default = name == self.cue.config.default_provider
            return Text("● ready · default" if default else "ready", style="green")
        if origin_of(name) is None:
            owner = next((p for p in self.cue.plugins.plugins() if name in p.manifest.providers), None)
            if owner is not None and not owner.enabled:
                return Text("plugin turned off", style="dim")
            return Text("off · plugin not installed" if table is None or not is_enabled(table)
                        else "plugin not installed", style="red")
        if table is None or not is_enabled(table):
            return Text("off", style="dim")
        return Text(f"needs setup: {self.cue.provider_problems.get(name, 'not started')}", style="yellow")

    def _plugin_status(self, plugin: InstalledPlugin, update: CatalogEntry | None) -> Text:
        if plugin.error:
            return Text(f"error: {plugin.error}", style="red")
        if not plugin.enabled:
            return Text("off", style="dim")
        if update is not None:
            return Text(f"on · update to {update.manifest.version}", style="yellow")
        return Text("on", style="green")

    def refresh_tables(self) -> None:
        providers_table = self.query_one("#provider-table", DataTable)
        keep = self._selected("#provider-table", self._provider_rows)
        providers_table.clear()
        self._provider_rows = self._provider_names()
        classes = discover()
        for name in self._provider_rows:
            title = getattr(classes.get(name), "title", "") or name
            label = Text(title, style="bold")
            if title != name:
                label.append(f"  {name}", style="dim")
            providers_table.add_row(label, self._provider_source(name), self._provider_status(name))
        if keep in self._provider_rows:
            providers_table.move_cursor(row=self._provider_rows.index(keep))

        player_table = self.query_one("#player-table", DataTable)
        keep = self._selected("#player-table", self._player_rows)
        player_table.clear()
        hidden = {name for plugin in self.cue.plugins.plugins() for name in plugin.manifest.players}
        self._player_rows = sorted(set(players()) | {self.cue.config.player} | hidden)
        for name in self._player_rows:
            backend = players().get(name)
            label = Text(backend.title if backend else name, style="bold")
            source = self._source(player_origin(name)) or "not installed"
            player_table.add_row(label, source, self._player_status(name)[0])
        if keep in self._player_rows:
            player_table.move_cursor(row=self._player_rows.index(keep))

        plugin_table = self.query_one("#plugin-table", DataTable)
        keep = self._selected("#plugin-table", self._plugin_rows)
        plugin_table.clear()
        installed = self.cue.plugins.scan()
        catalog = self.cue.catalog or []
        newer = updates(installed, catalog)
        self._plugin_rows = []
        for plugin in installed:
            plugin_table.add_row(
                Text(plugin.manifest.title, style="bold"), plugin.manifest.version,
                _adds(plugin.manifest), self._plugin_status(plugin, newer.get(plugin.name)),
            )
            self._plugin_rows.append(plugin.name)
        for entry in catalog:
            if self.cue.plugins.get(entry.manifest.name) is None:
                plugin_table.add_row(
                    Text(entry.manifest.title), entry.manifest.version, _adds(entry.manifest),
                    Text("available", style="cyan"),
                )
                self._plugin_rows.append(entry.manifest.name)
        if keep in self._plugin_rows:
            plugin_table.move_cursor(row=self._plugin_rows.index(keep))
        self._update_details()

    @on(DataTable.RowHighlighted)
    def _update_details(self) -> None:
        name = self._selected("#provider-table", self._provider_rows)
        cls = discover().get(name) if name else None
        lines = []
        if name and name in self.cue.provider_problems and name not in self.cue.providers:
            lines.append(self.cue.provider_problems[name])
        lines.append("Enter edits settings · Space turns it on or off · d makes it the default for searches")
        self.query_one("#provider-detail", Static).update("\n".join(lines) if cls or name else "")

        name = self._selected("#player-table", self._player_rows)
        lines = []
        if name is not None:
            backend = players().get(name)
            if backend is not None and backend.description:
                lines.append(backend.description)
            lines.append(self._player_status(name)[1])
        lines.append("Enter sets where the program is · d plays with this player")
        self.query_one("#player-detail", Static).update("\n".join(line for line in lines if line))

        name = self._selected("#plugin-table", self._plugin_rows)
        plugin = self.cue.plugins.get(name) if name else None
        entry = self._catalog_entry(name)
        lines = []
        manifest = plugin.manifest if plugin and plugin.valid else entry.manifest if entry else None
        if manifest is not None:
            lines.append(manifest.description or manifest.title)
            byline = " · ".join(part for part in (manifest.author, manifest.homepage) if part)
            if byline:
                lines.append(byline)
        if plugin is not None and plugin.source:
            lines.append(f"Installed from {plugin.source}")
        lines.append(self._catalog_note)
        self.query_one("#plugin-detail", Static).update("\n".join(line for line in lines if line))

    def _catalog_entry(self, name: str | None) -> CatalogEntry | None:
        return next((entry for entry in self.cue.catalog or [] if entry.manifest.name == name), None)

    # -- providers ------------------------------------------------------------------

    def action_settings(self) -> None:
        name = self._selected("#provider-table", self._provider_rows)
        if name is not None:
            self.open_settings(name)

    def open_settings(self, name: str) -> None:
        cls = discover().get(name)
        if cls is None:
            self.notify(f"Install the plugin for {name} first.", severity="warning")
            return
        table = self.cue.config.providers.get(name, {})
        settings, declared = _settings_for(cls, table)
        note = "" if declared else "This provider doesn't describe its settings; these are the keys in config.toml."
        title = getattr(cls, "title", "") or name

        def saved(values: dict[str, Any] | None) -> None:
            if values is None:
                return
            if self.cue.update_config(set_provider_options(name, values), set_provider_enabled(name, True)):
                self._report(name)
            self.refresh_tables()

        self.app.push_screen(ProviderSettingsDialog(title, settings, table, note), saved)

    def _report(self, name: str) -> None:
        if name in self.cue.providers:
            self.notify(f"{name} is ready.")
        elif name in self.cue.provider_problems:
            self.notify(self.cue.provider_problems[name], title=f"{name} needs setup", severity="warning", timeout=8)

    def action_toggle_provider(self) -> None:
        name = self._selected("#provider-table", self._provider_rows)
        if name is None:
            return
        table = self.cue.config.providers.get(name)
        turn_on = table is None or not is_enabled(table)
        if self.cue.update_config(set_provider_enabled(name, turn_on)):
            if turn_on:
                self._report(name)
            self.refresh_tables()

    def action_make_default(self) -> None:
        name = self._selected("#provider-table", self._provider_rows)
        if name is None:
            return
        if name not in self.cue.providers:
            self.notify(f"Set {name} up first; it isn't ready.", severity="warning")
            return
        if self.cue.update_config(set_default_provider(name)):
            self.cue.provider_name = name
            self.cue.set_status(f"searching {name}")
            self.refresh_tables()

    # -- players --------------------------------------------------------------------

    def action_player_settings(self) -> None:
        name = self._selected("#player-table", self._player_rows)
        backend = players().get(name) if name else None
        if backend is None:
            self.notify("Install this player's plugin first.", severity="warning")
            return

        def saved(values: dict[str, Any] | None) -> None:
            if values is not None and self.cue.update_config(set_player_options(backend.name, values)):
                self.refresh_tables()

        values = self.cue.config.players.get(backend.name, {})
        settings = (PATH_SETTING, *backend.settings) if backend.has_program else backend.settings
        self.app.push_screen(ProviderSettingsDialog(backend.title, settings, values), saved)

    def action_use_player(self) -> None:
        name = self._selected("#player-table", self._player_rows)
        backend = players().get(name) if name else None
        if backend is None or not backend.supported:
            self.notify("That player can't be used here.", severity="warning")
            return
        if self.cue.update_config(set_player(backend.name)):
            self.cue.set_status(f"playing in {backend.title}")
            if backend.program(self.cue.config.players.get(backend.name, {})) is None:
                self.notify(f"{backend.title} isn't found yet. {backend.missing}", severity="warning", timeout=8)
            self.refresh_tables()

    # -- plugins --------------------------------------------------------------------

    def action_reload_catalog(self) -> None:
        location = self.cue.config.plugins.catalog
        if not location:
            self._catalog_note = "The catalog is turned off ([plugins] catalog is empty)."
            self._update_details()
            return
        self._catalog_note = "Loading the plugin catalog…"
        self._update_details()
        self.run_worker(self._load_catalog(location), exclusive=True, group="catalog")

    async def _load_catalog(self, location: str) -> None:
        try:
            self.cue.catalog = await fetch_catalog(self.cue.http, location)
        except PluginError as exc:
            self._catalog_note = f"Catalog unavailable: {exc}"
        else:
            count = len(self.cue.catalog)
            self._catalog_note = f"The catalog lists {count} plugin{'s' if count != 1 else ''}."
        self.refresh_tables()

    def action_install(self) -> None:
        name = self._selected("#plugin-table", self._plugin_rows)
        entry = self._catalog_entry(name)
        plugin = self.cue.plugins.get(name) if name else None
        if entry is None:
            self.notify("This plugin isn't in the catalog; use a to add a newer copy from a file or link.")
            return
        if plugin is not None and plugin.valid and entry.manifest.version == plugin.manifest.version:
            self.notify(f"{plugin.manifest.title} is up to date.")
            return
        self.confirm_install(Source(entry.url, entry))

    def action_add(self) -> None:
        def chosen(text: str | None) -> None:
            if text:
                try:
                    self.confirm_install(find_source(text, self.cue.catalog))
                except PluginError as exc:
                    self.notify(str(exc), title="Can't add that plugin", severity="error", timeout=8)

        self.app.push_screen(AddPluginDialog(), chosen)

    def confirm_install(self, source: Source) -> None:
        message = f"From: {source.location}\n\n{WARNING}"
        self._confirm(f"Install {source.title}?", message, "Install", lambda: self.run_worker(self._install(source)))

    def _confirm(self, title: str, message: str, label: str, then: Callable[[], object]) -> None:
        def answered(yes: bool | None) -> None:
            if yes:
                then()

        self.app.push_screen(ConfirmDialog(title, message, label), answered)

    async def _install(self, source: Source) -> None:
        self.cue.set_status(f"installing {source.title}…")
        try:
            files = await fetch(self.cue.http, source)
            plugin = self.cue.plugins.install(files, source.location)
        except (PluginError, OSError) as exc:
            self.cue.set_status("")
            self.notify(str(exc), title="Plugin not installed", severity="error", timeout=10)
            return
        changes = after_install(plugin, self.cue.config)
        if not changes or not self.cue.update_config(*changes):
            self.cue.reload_providers()
        self.cue.set_status("")
        if plugin.error:
            self.notify(plugin.error, title=f"{plugin.manifest.title} installed but not loaded", severity="error",
                        timeout=10)
        else:
            self.notify(f"Installed {plugin.manifest.title} {plugin.manifest.version}.")
        self.refresh_tables()
        needs_setup = [name for name in plugin.providers if name in self.cue.provider_problems]
        if needs_setup:
            self.query_one(TabbedContent).active = PROVIDERS
            self.open_settings(needs_setup[0])

    def action_toggle_plugin(self) -> None:
        name = self._selected("#plugin-table", self._plugin_rows)
        plugin = self.cue.plugins.get(name) if name else None
        if plugin is None:
            return
        turn_on = not plugin.enabled
        if self.cue.update_config(set_plugin_disabled(name, not turn_on)):
            plugin = self.cue.plugins.set_enabled(name, turn_on)
            self.cue.reload_providers()
            if plugin.error:
                self.notify(plugin.error, title=f"{plugin.manifest.title} didn't load", severity="error", timeout=10)
            self.refresh_tables()

    def action_remove(self) -> None:
        name = self._selected("#plugin-table", self._plugin_rows)
        plugin = self.cue.plugins.get(name) if name else None
        if plugin is None:
            return

        def remove() -> None:
            try:
                provided = self.cue.plugins.remove(plugin.name)
            except (PluginError, OSError) as exc:
                self.notify(str(exc), title="Plugin not removed", severity="error")
                return
            changes = after_remove(plugin, provided, self.cue.config)
            if not changes or not self.cue.update_config(*changes):
                self.cue.reload_providers()
            self.notify(f"Removed {plugin.manifest.title}. Its settings stay in config.toml.")
            self.refresh_tables()

        self._confirm(f"Remove {plugin.manifest.title}?", "Its providers are turned off; their settings are kept.",
                      "Remove", remove)
