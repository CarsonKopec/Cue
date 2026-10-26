"""Find a player by environment override, beside Cue, PATH, or common install folders.

The environment override and the per-platform fallbacks follow ani-cli's player
detection.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def app_folder() -> Path:
    """Folder of ``cue.exe`` when frozen, otherwise of the launching script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(sys.argv[0]).resolve().parent


def beside_app(*names: str) -> str | None:
    """First of ``names`` that exists next to the app or in a same-named subfolder."""
    folder = app_folder()
    for name in names:
        stem = Path(name).stem
        for candidate in (folder / name, folder / stem / name):
            if candidate.is_file():
                return str(candidate)
    return None


def _common_locations() -> list[Path]:
    if sys.platform == "win32":
        home = Path.home()
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        return [
            home / "scoop" / "apps" / "mpv" / "current" / "mpv.exe",
            Path(os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey")) / "bin" / "mpv.exe",
            program_files / "mpv" / "mpv.exe",
            home / "AppData" / "Local" / "Programs" / "mpv" / "mpv.exe",
        ]
    if sys.platform == "darwin":
        return [Path("/opt/homebrew/bin/mpv"), Path("/usr/local/bin/mpv"), Path("/Applications/mpv.app/Contents/MacOS/mpv")]
    return []


def find_mpv() -> str | None:
    override = os.environ.get("CUE_MPV")
    if override:
        return shutil.which(override) or (override if Path(override).is_file() else None)
    # mpv.exe rather than mpv.com: the .com wrapper would attach to Cue's console.
    executable = "mpv.exe" if sys.platform == "win32" else "mpv"
    found = beside_app(executable) or shutil.which(executable)
    if found:
        return found
    return next((str(path) for path in _common_locations() if path.is_file()), None)


def _vlc_locations() -> list[Path]:
    if sys.platform == "win32":
        return [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "VideoLAN" / "VLC" / "vlc.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "VideoLAN" / "VLC" / "vlc.exe",
        ]
    if sys.platform == "darwin":
        return [
            Path("/Applications/VLC.app/Contents/MacOS/VLC"),
            Path.home() / "Applications" / "VLC.app" / "Contents" / "MacOS" / "VLC",
        ]
    return []


def find_vlc() -> str | None:
    """Locate VLC, respecting an explicit ``CUE_VLC`` override first."""
    override = os.environ.get("CUE_VLC")
    if override:
        return shutil.which(override) or (override if Path(override).is_file() else None)
    executable = "vlc.exe" if sys.platform == "win32" else "vlc"
    found = beside_app(executable) or shutil.which(executable)
    if found:
        return found
    return next((str(path) for path in _vlc_locations() if path.is_file()), None)
