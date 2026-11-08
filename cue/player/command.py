"""Any video player, run with a command line you write: the "Another program" player."""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence

from cue.player.process import ProcessPlayer

DEFAULT_ARGUMENTS = '"{url}"'


class _Fields(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"  # leave unknown placeholders alone


def split_arguments(template: str) -> list[str]:
    """Split like a Windows command line: spaces separate, double quotes group, backslashes stay."""
    return [part[1:-1] if len(part) > 1 and part[0] == part[-1] == '"' else part
            for part in shlex.split(template, posix=False)]


class CommandPlayer(ProcessPlayer):
    """Runs ``path`` with ``arguments`` from ``[players.other]``, filling in the link and resume point.

    Cue gives it a link it needs no headers for (its local relay), so ``headers`` stays empty.
    """

    title = "Your player"
    headers = frozenset()

    def arguments(
        self, url: str, *, start: float | None, headers: Mapping[str, str], title: str | None,
        extra_args: Sequence[str],
    ) -> list[str]:
        # extra_args (Cue's player_args) are written for VLC and mpv, so they aren't passed on.
        template = str(self.options.get("arguments") or DEFAULT_ARGUMENTS)
        seconds = max(0.0, start or 0.0)
        fields = _Fields(url=url, title=title or "Cue", start=f"{seconds:.0f}", start_ms=f"{seconds * 1000:.0f}")
        parts = split_arguments(template)
        arguments = [part.format_map(fields) for part in parts]
        return arguments if any("{url}" in part for part in parts) else [*arguments, url]
