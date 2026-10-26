"""Small formatting helpers shared by the UI and the file-writing layers."""

from __future__ import annotations

import re

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def format_time(seconds: float | None) -> str:
    """``83`` -> ``1:23``; ``3723`` -> ``1:02:03``."""
    if seconds is None:
        return "--:--"
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def safe_filename(name: str) -> str:
    """Make ``name`` usable as a Windows file or folder name."""
    cleaned = _UNSAFE.sub(" ", name).strip(" .")
    return re.sub(r"\s+", " ", cleaned) or "untitled"
