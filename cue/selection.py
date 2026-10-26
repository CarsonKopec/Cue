"""Episode-range parsing and quality selection, ported from ani-cli."""

from __future__ import annotations

import re
from collections.abc import Sequence

from cue.providers.base import Stream, format_number

_TOKEN = r"-1|\d+(?:\.\d+)?"
_SELECTION = re.compile(rf"^\s*({_TOKEN})(?:\s*(?:-|\s)\s*({_TOKEN}))?\s*$")


class SelectionError(ValueError):
    """The user's episode selection cannot be satisfied."""


def parse_episode_range(text: str, available: Sequence[float]) -> list[float]:
    """Episodes picked by ``text``, in list order.

    Accepts ``3``, ``1-5``, ``1 5``, ``-1`` (the latest) and ``3--1`` (3 to the latest);
    several selections can be joined with commas. As in ani-cli a range follows the
    episode list, so ``12-13`` also picks up a ``12.5`` special.
    """
    numbers = list(available)
    if not numbers:
        raise SelectionError("No episodes available")
    chosen: list[float] = []
    for part in text.split(","):
        if not part.strip():
            continue
        match = _SELECTION.match(part)
        if not match:
            raise SelectionError(f"Not an episode selection: {part.strip()!r}")
        first = _position(match.group(1), numbers)
        last = _position(match.group(2), numbers) if match.group(2) else first
        low, high = sorted((first, last))
        chosen.extend(n for n in numbers[low : high + 1] if n not in chosen)
    if not chosen:
        raise SelectionError("Type an episode or a range, e.g. 1-5")
    return chosen


def _position(token: str, numbers: list[float]) -> int:
    if token == "-1":
        return len(numbers) - 1
    try:
        return numbers.index(float(token))
    except ValueError:
        raise SelectionError(f"Episode {format_number(float(token))} is not available") from None


def resolution(quality: str | None) -> int:
    if not quality:
        return 0
    if quality.strip().lower() in ("4k", "uhd"):
        return 2160
    digits = re.match(r"\d+", quality.strip())
    return int(digits.group()) if digits else 0


def select_quality(streams: Sequence[Stream], quality: str = "best") -> Stream | None:
    """ani-cli's choice: ``best``, ``worst``, or a resolution such as ``720``,
    falling back to the best stream when that resolution is missing."""
    if not streams:
        return None
    ranked = sorted(streams, key=lambda s: resolution(s.quality), reverse=True)
    wanted = str(quality).strip().lower()
    if wanted == "worst":
        return ranked[-1]
    if wanted not in ("", "best"):
        target = resolution(wanted)
        for stream in ranked:
            if resolution(stream.quality) == target:
                return stream
    return ranked[0]
