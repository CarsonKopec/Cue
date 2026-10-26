# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for a single-file, console-attached cue.exe (the TUI *is* the app).
# From the repo root:  python build/release.py   (or: pyinstaller build/cue.spec)
import importlib.util
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent

hiddenimports = (
    collect_submodules("cue.providers")  # found by scanning at runtime, never imported by name
    + collect_submodules("textual")  # widgets and drivers are imported lazily
    + collect_submodules("rapidfuzz")
)
if importlib.util.find_spec("yt_dlp") is not None:  # optional downloads
    hiddenimports += collect_submodules("yt_dlp")

datas = collect_data_files("textual")  # Textual's CSS and other assets
datas.append((
    str(root / "cue" / "player" / "vlc_http" / "requests" / "status.json"),
    "cue/player/vlc_http/requests",
))

a = Analysis(
    [str(root / "cue" / "__main__.py")],
    pathex=[str(root)],
    hiddenimports=hiddenimports,
    datas=datas,
    # Unused optional imports. conda-forge's rapidfuzz pulls in numpy, and with it ~500 MB of MKL.
    excludes=["tkinter", "numpy", "PIL"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="cue",
    console=True,
    upx=False,
    runtime_tmpdir=None,
)
