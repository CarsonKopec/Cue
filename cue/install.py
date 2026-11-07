"""``cue install``: copy cue.exe to a folder of its own and put that folder on PATH (Windows).

No administrator rights are needed: the program goes to ``%LOCALAPPDATA%\\Programs\\Cue``,
PATH is the user's own (``HKEY_CURRENT_USER\\Environment``), and Apps & Features gets an
entry whose Uninstall runs ``cue uninstall``. Settings, history and plugins in
``%APPDATA%\\cue`` are never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cue import __version__

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Cue"
ENVIRONMENT_KEY = "Environment"
REG_SZ, REG_EXPAND_SZ, REG_DWORD = 1, 2, 4  # winreg's value types, without importing winreg elsewhere
EXE = "cue.exe"
# Files a release zip may carry next to cue.exe, which Cue looks for beside itself.
COMPANIONS = ("mpv", "mpv.exe", "vlc", "yt-dlp.exe", "README.md", "LICENSE")


class InstallError(Exception):
    """Cue can't be installed or uninstalled here."""


def install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    return Path(base) / "Programs" / "Cue"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# -- PATH ---------------------------------------------------------------------------
# Plain functions of the PATH text, so they can be checked without the registry.


def _same_folder(entry: str, folder: Path) -> bool:
    cleaned = os.path.expandvars(entry.strip().strip('"'))
    return bool(cleaned) and os.path.normcase(os.path.normpath(cleaned)) == os.path.normcase(os.path.normpath(folder))


def path_with(value: str, folder: Path) -> str | None:
    """``value`` with ``folder`` appended, or ``None`` when it's already there."""
    entries = [entry for entry in value.split(";") if entry.strip()]
    if any(_same_folder(entry, folder) for entry in entries):
        return None
    return ";".join([*entries, str(folder)])


def path_without(value: str, folder: Path) -> str | None:
    """``value`` without ``folder``, or ``None`` when it wasn't there."""
    entries = [entry for entry in value.split(";") if entry.strip()]
    kept = [entry for entry in entries if not _same_folder(entry, folder)]
    return None if len(kept) == len(entries) else ";".join(kept)


# -- registry -----------------------------------------------------------------------


class Registry(Protocol):
    def get(self, key: str, name: str) -> tuple[Any, int] | None: ...
    def set(self, key: str, values: dict[str, tuple[Any, int]]) -> None: ...
    def delete_key(self, key: str) -> None: ...
    def environment_changed(self) -> None: ...


class UserRegistry:
    """The ``HKEY_CURRENT_USER`` reads and writes that installing needs."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise InstallError("installing works on Windows only; elsewhere use pip install")
        import winreg

        self._winreg = winreg

    def get(self, key: str, name: str) -> tuple[Any, int] | None:
        try:
            with self._winreg.OpenKey(self._winreg.HKEY_CURRENT_USER, key) as handle:
                return self._winreg.QueryValueEx(handle, name)
        except FileNotFoundError:
            return None

    def set(self, key: str, values: dict[str, tuple[Any, int]]) -> None:
        with self._winreg.CreateKeyEx(self._winreg.HKEY_CURRENT_USER, key, 0, self._winreg.KEY_SET_VALUE) as handle:
            for name, (value, kind) in values.items():
                self._winreg.SetValueEx(handle, name, 0, kind, value)

    def delete_key(self, key: str) -> None:
        try:
            self._winreg.DeleteKey(self._winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            pass

    def environment_changed(self) -> None:
        """Tell Explorer that PATH changed, so terminals opened from now on see it."""
        import ctypes
        from ctypes import wintypes

        result = wintypes.DWORD()
        ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result),  # HWND_BROADCAST, WM_SETTINGCHANGE
        )


# -- install and uninstall --------------------------------------------------------------


@dataclass(frozen=True)
class Installation:
    folder: Path
    version: str | None


def installed(registry: Registry) -> Installation | None:
    location = registry.get(UNINSTALL_KEY, "InstallLocation")
    if location is None or not (Path(str(location[0])) / EXE).is_file():
        return None
    version = registry.get(UNINSTALL_KEY, "DisplayVersion")
    return Installation(Path(str(location[0])), str(version[0]) if version else None)


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or (b.exists() and os.path.samefile(a, b))
    except OSError:
        return False


def _replace(source: Path, target: Path) -> None:
    """Copy ``source`` over ``target``, even while that copy of Cue is running.

    Windows won't overwrite a running program but will rename it, so the old copy is
    moved aside and deleted if it isn't in use (otherwise on the next install).
    """
    staged = target.with_name(target.name + ".new")
    shutil.copy2(source, staged)
    if target.exists():
        retired = target.with_name(f"{target.stem}.old-{uuid.uuid4().hex[:8]}{target.suffix}")
        target.rename(retired)
        try:
            retired.unlink()
        except OSError:
            pass
    staged.rename(target)


def _clear_leftovers(folder: Path) -> None:
    for leftover in folder.glob("cue.old-*.exe"):
        try:
            leftover.unlink()
        except OSError:
            pass  # still running


def install(source: Path, registry: Registry, folder: Path | None = None) -> tuple[Path, bool]:
    """Install ``source`` (cue.exe) and the files beside it; returns the new exe and whether PATH changed."""
    folder = folder or install_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / EXE
    _clear_leftovers(folder)
    if not _same_file(source, target):
        _replace(source, target)
        for name in COMPANIONS:
            item = source.parent / name
            if item.is_dir():
                shutil.copytree(item, folder / name, dirs_exist_ok=True)
            elif item.is_file():
                shutil.copy2(item, folder / name)

    current = registry.get(ENVIRONMENT_KEY, "Path")
    value, kind = (str(current[0]), current[1]) if current else ("", REG_EXPAND_SZ)
    new_path = path_with(value, folder)
    if new_path is not None:
        registry.set(ENVIRONMENT_KEY, {"Path": (new_path, kind if kind in (REG_SZ, REG_EXPAND_SZ) else REG_EXPAND_SZ)})
    size_kb = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file()) // 1024
    registry.set(UNINSTALL_KEY, {
        "DisplayName": ("Cue", REG_SZ),
        "DisplayVersion": (__version__, REG_SZ),
        "Publisher": ("Cue", REG_SZ),
        "InstallLocation": (str(folder), REG_SZ),
        "DisplayIcon": (str(target), REG_SZ),
        "UninstallString": (f'"{target}" uninstall', REG_SZ),
        "QuietUninstallString": (f'"{target}" uninstall --yes', REG_SZ),
        "NoModify": (1, REG_DWORD),
        "NoRepair": (1, REG_DWORD),
        "EstimatedSize": (size_kb, REG_DWORD),
    })
    if new_path is not None:
        registry.environment_changed()
    return target, new_path is not None


def _delete_after_exit(paths: list[Path], folder: Path) -> None:
    """Delete files (and then the folder, if empty) once this process has exited."""
    steps = " & ".join(f'del /f /q "{path}"' for path in paths)
    command = f'ping -n 3 127.0.0.1 >nul & {steps} & rmdir "{folder}"'
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(["cmd.exe", "/d", "/c", command], creationflags=flags, close_fds=True,  # noqa: S603
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def uninstall(registry: Registry, running: Path | None = None) -> Path:
    """Remove the installed copy, its PATH entry and its Apps & Features entry.

    Only Cue's own files are deleted, never anything else in the folder. Returns the folder.
    """
    found = installed(registry)
    folder = found.folder if found else install_dir()
    current = registry.get(ENVIRONMENT_KEY, "Path")
    if current is not None:
        new_path = path_without(str(current[0]), folder)
        if new_path is not None:
            registry.set(ENVIRONMENT_KEY, {"Path": (new_path, current[1])})
    registry.delete_key(UNINSTALL_KEY)
    registry.environment_changed()

    for name in COMPANIONS:
        item = folder / name
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        elif item.is_file():
            item.unlink(missing_ok=True)
    _clear_leftovers(folder)
    target = folder / EXE
    running = running or Path(sys.executable)
    if target.exists() and _same_file(running, target):
        _delete_after_exit([target], folder)  # a running program can't delete itself
    else:
        target.unlink(missing_ok=True)
        try:
            folder.rmdir()
        except OSError:
            pass  # not empty (other files) or already gone
    return folder
