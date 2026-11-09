"""Choosing where to cast: VLC browsing Cue's media server, or a DLNA, AirPlay or Chromecast TV."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from cue.cast import devices
from cue.cast.devices import Device
from cue.widgets import VimOptionList

DLNA = "dlna"  # VLC on the TV opens Cue's media server itself


class CastPicker(ModalScreen[Device | str | None]):
    """VLC (DLNA) right away, plus the DLNA, AirPlay and Chromecast TVs found on the network."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", show=False),
        Binding("r", "rescan", "Look again"),
    ]

    def __init__(self, credentials: Path, server_name: str, remembered: str = "") -> None:
        super().__init__()
        self._credentials = credentials
        self._server_name = server_name
        self._remembered = remembered  # the TV cast to last, highlighted when found
        self._found: list[Device] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Label("Cast to", classes="section")
            yield VimOptionList(id="targets")

    def on_mount(self) -> None:
        self._show(searching=True)
        self.query_one("#targets", OptionList).focus()
        self._scan()

    def _scan(self) -> None:
        self.run_worker(self._discover(), group="cast-scan", exclusive=True)

    async def _discover(self) -> None:
        self._found = await devices.discover(self._credentials)
        self._show(searching=False)

    def _show(self, *, searching: bool) -> None:
        targets = self.query_one("#targets", OptionList)
        targets.clear_options()
        targets.add_option(Option(Text.assemble(
            ("VLC on the TV", "bold"), (f" · opens {self._server_name} from VLC's Network tab (Xbox, DLNA)", "dim"),
        ), id=DLNA))
        for number, device in enumerate(self._found):
            note = f"{device.kind_label}{', ' + device.model if device.model else ''}"
            if device.needs_pairing:
                note += " · asks for a code the first time"
            targets.add_option(Option(Text.assemble((device.name, "bold"), (f" · {note}", "dim")), id=str(number)))
        if searching:
            targets.add_option(Option(Text("Looking for TVs (DLNA, AirPlay, Chromecast)…", "dim"), disabled=True))
        elif not self._found:
            targets.add_option(Option(Text("No other TVs found · r looks again", "dim"), disabled=True))
        remembered = next((number for number, device in enumerate(self._found)
                           if device.identifier == self._remembered), None)
        targets.highlighted = remembered + 1 if remembered is not None else 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == DLNA:
            self.dismiss(DLNA)
        elif event.option.id is not None:
            self.dismiss(self._found[int(event.option.id)])

    def action_rescan(self) -> None:
        self._found = []
        self._show(searching=True)
        self._scan()

    def action_cancel(self) -> None:
        self.dismiss(None)


class PairingDialog(ModalScreen[str | None]):
    """The code an AirPlay TV shows while pairing."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label(f"Pair with {self._name}", classes="section")
            yield Static("Type the code shown on the TV. Cue remembers the pairing, so this happens once.",
                         classes="hint")
            yield Input(placeholder="Code from the TV", id="pairing-code", restrict=r"[0-9 ]*", max_length=12)

    def on_mount(self) -> None:
        self.query_one("#pairing-code", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.replace(" ", "") or None)

    def action_cancel(self) -> None:
        self.dismiss(None)
