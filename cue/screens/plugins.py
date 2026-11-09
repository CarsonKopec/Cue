"""Plugins, providers, groups, players and subtitles: set providers up, group them, pick a player,
choose where subtitles come from, install plugins."""

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
    Button, DataTable, Footer, Header, Input, Label, Select, SelectionList, Static, Switch, TabbedContent, TabPane,
)

from cue.config import (
    remove_group, set_default_group, set_group_title, set_player, set_player_options, set_plugin_disabled,
    set_provider_enabled, set_provider_groups, set_provider_options, set_subtitle_enabled, set_subtitle_options,
)
from cue.groups import ALL, NAME, Group, definitions, provider_groups, slug
from cue.player.registry import player_origin, players
from cue.plugins import CatalogEntry, InstalledPlugin, Manifest, PluginError, fetch_catalog
from cue.plugins.actions import WARNING, Source, after_install, after_remove, fetch, find_source, updates
from cue.providers import BUILT_IN, discover, is_enabled, origin_of
from cue.providers.base import Setting
from cue.screens import CueScreen
from cue.subtitles.registry import is_on, subtitle_backends, subtitle_origin
from cue.widgets import StatusBar

PROVIDERS, GROUPS, PLAYERS, SUBTITLES, PLUGINS = "providers-tab", "groups-tab", "players-tab", "subtitles-tab", "plugins-tab"
TABS = [PROVIDERS, GROUPS, PLAYERS, SUBTITLES, PLUGINS]


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


class NewGroupDialog(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label("New group", classes="section")
            yield Static("A name for it, such as Kids or Documentaries. Next you choose its providers.",
                         classes="hint")
            yield Input(placeholder="Group name", id="group-name")

    def on_mount(self) -> None:
        self.query_one("#group-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class GroupDialog(ModalScreen[tuple[str, list[str]] | None]):
    """A group's title and which providers are in it; returns both."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save"),
    ]

    def __init__(self, group: Group, choices: list[tuple[str, str, bool]]) -> None:
        super().__init__()
        self._group = group
        self._choices = choices  # (label, provider name, in the group now)

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label(f"{self._group.title} group", classes="section")
            yield Label("Title", classes="field")
            yield Input(value=self._group.title, id="group-title")
            yield Label("Providers in this group", classes="field")
            yield SelectionList[str](*self._choices, id="group-members")
            yield Static("Space ticks or unticks a provider. A provider can be in several groups.", classes="hint")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    def on_mount(self) -> None:
        self.query_one("#group-members", SelectionList).focus()

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
        title = self.query_one("#group-title", Input).value.strip() or self._group.title
        self.dismiss((title, list(self.query_one("#group-members", SelectionList).selected)))


# -- the screen ---------------------------------------------------------------------


def _adds(manifest: Manifest) -> str:
    """What a plugin adds, for the Plugins table: its providers, then its players and subtitle sources."""
    return ", ".join([*manifest.providers, *(f"{name} (player)" for name in manifest.players),
                      *(f"{name} (subtitles)" for name in manifest.subtitles)])


class PluginsScreen(CueScreen):
    BINDINGS = [
        Binding("right", "switch_tab(1)", "Next tab", priority=True),
        Binding("left", "switch_tab(-1)", show=False, priority=True),
        # Providers tab
        Binding("enter", "settings", "Settings", priority=True),
        Binding("space", "toggle_provider", "On/off"),
        # Groups tab
        Binding("enter", "edit_group", "Edit", priority=True),
        Binding("d", "default_group", "Start here"),
        Binding("n", "new_group", "New group"),
        Binding("x,delete", "remove_group", "Remove"),
        # Players tab
        Binding("enter", "player_settings", "Settings", priority=True),
        Binding("d", "use_player", "Use this player"),
        # Subtitles tab
        Binding("enter", "subtitle_settings", "Settings", priority=True),
        Binding("space", "toggle_subtitles", "On/off"),
        # Plugins tab
        Binding("enter", "install", "Install", priority=True),
        Binding("space", "toggle_plugin", "On/off"),
        Binding("a", "add", "Add file"),
        Binding("x,delete", "remove", "Remove"),
        Binding("r", "reload_catalog", "Refresh"),
        Binding("q,escape", "back", "Back"),
    ]
    PROVIDER_ACTIONS = {"settings", "toggle_provider"}
    GROUP_ACTIONS = {"edit_group", "default_group", "new_group", "remove_group"}
    PLAYER_ACTIONS = {"player_settings", "use_player"}
    SUBTITLE_ACTIONS = {"subtitle_settings", "toggle_subtitles"}
    PLUGIN_ACTIONS = {"install", "toggle_plugin", "add", "remove", "reload_catalog"}

    def __init__(self) -> None:
        super().__init__()
        self._provider_rows: list[str] = []
        self._group_rows: list[Group] = []
        self._player_rows: list[str] = []
        self._subtitle_rows: list[str] = []
        self._plugin_rows: list[str] = []
        self._catalog_note = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="plugins-view", classes="view"):
            with TabPane("Providers", id=PROVIDERS):
                yield DataTable(id="provider-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="provider-detail", classes="hint")
            with TabPane("Groups", id=GROUPS):
                yield DataTable(id="group-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="group-detail", classes="hint")
            with TabPane("Players", id=PLAYERS):
                yield DataTable(id="player-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="player-detail", classes="hint")
            with TabPane("Subtitles", id=SUBTITLES):
                yield DataTable(id="subtitle-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="subtitle-detail", classes="hint")
            with TabPane("Plugins", id=PLUGINS):
                yield DataTable(id="plugin-table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="plugin-detail", classes="hint")
        yield StatusBar()
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Plugins, providers, groups, players and subtitles"
        self.query_one("#provider-table", DataTable).add_columns("Provider", "From", "Groups", "Status")
        self.query_one("#group-table", DataTable).add_columns("Group", "Providers", "Status")
        self.query_one("#player-table", DataTable).add_columns("Player", "From", "Status")
        self.query_one("#subtitle-table", DataTable).add_columns("Source", "From", "Status")
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
        if action in self.GROUP_ACTIONS:
            return self._tab == GROUPS
        if action in self.PLAYER_ACTIONS:
            return self._tab == PLAYERS
        if action in self.SUBTITLE_ACTIONS:
            return self._tab == SUBTITLES
        if action in self.PLUGIN_ACTIONS:
            return self._tab == PLUGINS
        return True

    @on(TabbedContent.TabActivated)
    def _tab_changed(self, event: TabbedContent.TabActivated) -> None:
        table_id = {PROVIDERS: "#provider-table", GROUPS: "#group-table", PLAYERS: "#player-table",
                    SUBTITLES: "#subtitle-table"}.get(event.pane.id, "#plugin-table")
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
            return Text("ready", style="green")
        if origin_of(name) is None:
            owner = next((p for p in self.cue.plugins.plugins() if name in p.manifest.providers), None)
            if owner is not None and not owner.enabled:
                return Text("plugin turned off", style="dim")
            return Text("off · plugin not installed" if table is None or not is_enabled(table)
                        else "plugin not installed", style="red")
        if table is None or not is_enabled(table):
            return Text("off", style="dim")
        return Text(f"needs setup: {self.cue.provider_problems.get(name, 'not started')}", style="yellow")

    def _subtitle_status(self, name: str) -> tuple[Text, str]:
        """The status cell, and the detail line explaining it."""
        backend = subtitle_backends().get(name)
        if backend is None:
            owner = next((p for p in self.cue.plugins.plugins() if name in p.manifest.subtitles), None)
            if owner is not None and not owner.enabled:
                return Text("plugin turned off", style="dim"), f"Its plugin, {owner.name}, is turned off."
            return Text("not installed", style="red"), "Install the plugin that adds it."
        searched = {source.name for source in self.cue.subtitles.sources} if self.cue.subtitles else set()
        if name in searched:
            return Text("ready", style="green"), "Searched with the other ready sources."
        problem = self.cue.subtitle_problems.get(name, "not started")
        if problem == "off":
            return Text("off", style="dim"), "Space turns it on."
        if problem.startswith("needs"):
            where = f" Get one at {backend.homepage}" if backend.homepage else ""
            return Text(problem, style="yellow"), f"Enter to set it up.{where}"
        return Text(problem, style="red"), "Check its settings (Enter), or update its plugin."

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
            groups = ", ".join(self._group_title(group) for group in self._groups_of(name))
            providers_table.add_row(label, self._provider_source(name), groups or Text("none", style="dim"),
                                    self._provider_status(name))
        if keep in self._provider_rows:
            providers_table.move_cursor(row=self._provider_rows.index(keep))

        group_table = self.query_one("#group-table", DataTable)
        keep_group = self._selected_group()
        group_table.clear()
        self._group_rows = self._all_groups()
        start = self.cue.config.default_group or (self.cue.groups[0].name if self.cue.groups else "")
        for group in self._group_rows:
            members = self._members(group)
            ready = [name for name in members if name in self.cue.providers]
            cell = Text(", ".join(ready))
            for name in members:
                if name not in ready:
                    cell.append(f"{', ' if len(cell) else ''}{name} (off)", style="dim")
            if group.name == start:
                status = Text("● start here", style="green")
            elif not ready:
                status = Text("empty: not in Tab", style="dim")
            else:
                status = Text("")
            group_table.add_row(Text(group.title, style="bold"), cell, status)
        if keep_group is not None and keep_group.name in [group.name for group in self._group_rows]:
            group_table.move_cursor(row=[group.name for group in self._group_rows].index(keep_group.name))

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

        subtitle_table = self.query_one("#subtitle-table", DataTable)
        keep = self._selected("#subtitle-table", self._subtitle_rows)
        subtitle_table.clear()
        hidden = [name for plugin in self.cue.plugins.plugins() for name in plugin.manifest.subtitles
                  if name not in subtitle_backends()]
        self._subtitle_rows = [*subtitle_backends(), *sorted(set(hidden))]
        for name in self._subtitle_rows:
            backend = subtitle_backends().get(name)
            label = Text(backend.title if backend else name, style="bold")
            source = self._source(subtitle_origin(name)) or "not installed"
            subtitle_table.add_row(label, source, self._subtitle_status(name)[0])
        if keep in self._subtitle_rows:
            subtitle_table.move_cursor(row=self._subtitle_rows.index(keep))

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
        lines.append("Enter edits settings · Space turns it on or off · the Groups tab says where it's searched")
        self.query_one("#provider-detail", Static).update("\n".join(lines) if cls or name else "")

        group = self._selected_group()
        lines = []
        if group is not None:
            if group.name == ALL:
                lines.append("Searches every provider that's ready.")
            elif group.media == "movie":
                lines.append("Providers that also list series show only their movies here.")
            elif group.media == "series":
                lines.append("Providers that also list movies show only their series here.")
        lines.append("Tab on the Home screen switches between groups.")
        lines.append("Enter picks its providers · d starts searches here · n makes a new group · x removes one")
        self.query_one("#group-detail", Static).update("\n".join(lines))

        name = self._selected("#player-table", self._player_rows)
        lines = []
        if name is not None:
            backend = players().get(name)
            if backend is not None and backend.description:
                lines.append(backend.description)
            lines.append(self._player_status(name)[1])
        lines.append("Enter sets where the program is · d plays with this player")
        self.query_one("#player-detail", Static).update("\n".join(line for line in lines if line))

        name = self._selected("#subtitle-table", self._subtitle_rows)
        lines = []
        if name is not None:
            backend = subtitle_backends().get(name)
            if backend is not None and backend.description:
                lines.append(backend.description)
            lines.append(self._subtitle_status(name)[1])
        lines.append("Enter edits its settings · Space turns it on or off · every ready source is searched")
        self.query_one("#subtitle-detail", Static).update("\n".join(line for line in lines if line))

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

    # -- groups ---------------------------------------------------------------------

    def _groups_of(self, provider: str) -> list[str]:
        return provider_groups(discover().get(provider), self.cue.config.providers.get(provider, {}))

    def _all_groups(self) -> list[Group]:
        """Every group, empty ones too, then All."""
        referenced = [group for name in self._provider_names() for group in self._groups_of(name)]
        return [*definitions(self.cue.config.groups, referenced), Group(ALL, "All", built_in=True)]

    def _group_title(self, name: str) -> str:
        return next((group.title for group in self._all_groups() if group.name == name), name)

    def _members(self, group: Group) -> list[str]:
        if group.name == ALL:
            return list(self.cue.providers)
        return [name for name in self._provider_names() if group.name in self._groups_of(name)]

    def _selected_group(self) -> Group | None:
        table = self.query_one("#group-table", DataTable)
        rows = self._group_rows
        return rows[table.cursor_row] if table.row_count and 0 <= table.cursor_row < len(rows) else None

    def action_edit_group(self) -> None:
        group = self._selected_group()
        if group is None:
            return
        if group.name == ALL:
            self.notify("All always searches every ready provider.")
            return
        self.edit_group(group)

    def edit_group(self, group: Group) -> None:
        classes = discover()
        names = self._provider_names()
        choices = []
        for name in names:
            title = getattr(classes.get(name), "title", "") or name
            label = title if name in self.cue.providers else f"{title} (not ready)"
            choices.append((label, name, group.name in self._groups_of(name)))

        def saved(result: tuple[str, list[str]] | None) -> None:
            if result is None:
                return
            title, chosen = result
            changes = []
            if title != group.title:
                changes.append(set_group_title(group.name, title))
            for name in names:
                current = self._groups_of(name)
                if (name in chosen) != (group.name in current):
                    wanted = [*current, group.name] if name in chosen else [g for g in current if g != group.name]
                    changes.append(set_provider_groups(name, wanted, exists=name in self.cue.config.providers))
            if changes and self.cue.update_config(*changes):
                self.refresh_tables()

        self.app.push_screen(GroupDialog(group, choices), saved)

    def action_default_group(self) -> None:
        group = self._selected_group()
        if group is None or not self.cue.update_config(set_default_group(group.name)):
            return
        if self.cue._choose_group(group.name):
            self.cue.set_status(f"searching {group.title}")
        else:
            self.notify(f"{group.title} has no ready providers yet, so searches start elsewhere for now.")
        self.refresh_tables()

    def action_new_group(self) -> None:
        def named(title: str | None) -> None:
            if not title:
                return
            name = slug(title)
            if not NAME.fullmatch(name) or name == ALL:
                self.notify("Use a name with letters in it, other than All.", severity="warning")
                return
            existing = next((group for group in self._all_groups() if group.name == name), None)
            if existing is not None:
                self.notify(f"There's already a {existing.title} group.", severity="warning")
                return
            if self.cue.update_config(set_group_title(name, title)):
                self.refresh_tables()
                self.edit_group(Group(name, title))

        self.app.push_screen(NewGroupDialog(), named)

    def action_remove_group(self) -> None:
        group = self._selected_group()
        if group is None:
            return
        if group.built_in:
            self.notify(f"{group.title} is built in; when no provider is in it, Tab skips it.")
            return

        def remove() -> None:
            if self.cue.update_config(remove_group(group.name)):
                self.notify(f"Removed the {group.title} group.")
                self.refresh_tables()

        self._confirm(f"Remove the {group.title} group?", "Its providers stay; they just leave this group.",
                      "Remove", remove)

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
        if not backend.all_settings:
            self.notify(f"{backend.title} has no settings.")
            return
        self.app.push_screen(ProviderSettingsDialog(backend.title, backend.all_settings, values), saved)

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

    # -- subtitles ------------------------------------------------------------------

    def action_subtitle_settings(self) -> None:
        name = self._selected("#subtitle-table", self._subtitle_rows)
        backend = subtitle_backends().get(name) if name else None
        if backend is None:
            self.notify("Install or turn on this subtitle source's plugin first.", severity="warning")
            return
        if not backend.settings:
            self.notify(f"{backend.title} has no settings; Space turns it on or off.")
            return

        def saved(values: dict[str, Any] | None) -> None:
            if values is None:
                return
            if self.cue.update_config(set_subtitle_options(backend.name, values), set_subtitle_enabled(backend.name, True)):
                status, detail = self._subtitle_status(backend.name)
                if status.plain == "ready":
                    self.notify(f"{backend.title} is ready.")
                else:
                    self.notify(detail, title=f"{backend.title}: {status.plain}", severity="warning", timeout=8)
            self.refresh_tables()

        values = self.cue.config.subtitles.sources.get(backend.name, {})
        note = f"Sign up or get a key at {backend.homepage}" if backend.homepage else ""
        self.app.push_screen(ProviderSettingsDialog(backend.title, backend.settings, values, note), saved)

    def action_toggle_subtitles(self) -> None:
        name = self._selected("#subtitle-table", self._subtitle_rows)
        backend = subtitle_backends().get(name) if name else None
        if backend is None:
            self.notify("Install or turn on this subtitle source's plugin first.", severity="warning")
            return
        on = not is_on(self.cue.config.subtitles.sources.get(backend.name, {}))
        if self.cue.update_config(set_subtitle_enabled(backend.name, on)):
            self.cue.set_status(f"{backend.title} subtitles {'on' if on else 'off'}")
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
