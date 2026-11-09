# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for a single-file, console program (the TUI *is* the app): dist/cue.exe on
# Windows, dist/cue on Linux and macOS. Build it on each platform for that platform.
# From the repo root:  python build/release.py   (or: pyinstaller build/cue.spec)
import importlib.util
import sys
from pathlib import Path

import certifi

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent


def _no_tests(name):
    return ".test" not in name and ".tests" not in name and "idlelib" not in name


# Plugins are plain Python run by cue.exe, so they can import only what's bundled: all of
# Cue (some modules, such as cue.player.process, exist for plugins), and these standard
# library modules besides the ones Cue itself uses.
PLUGIN_STDLIB_PACKAGES = ["asyncio", "concurrent", "email", "html", "http", "json", "urllib", "xml", "zoneinfo"]
PLUGIN_STDLIB_MODULES = [
    "ast", "base64", "binascii", "bisect", "calendar", "colorsys", "csv", "dataclasses", "datetime", "decimal",
    "difflib", "enum", "fnmatch", "fractions", "functools", "glob", "gzip", "hashlib", "heapq", "hmac",
    "ipaddress", "itertools", "locale", "math", "mimetypes", "operator", "pprint", "queue", "random", "secrets",
    "shlex", "shutil", "socket", "ssl", "statistics", "string", "struct", "subprocess", "tempfile", "textwrap",
    "threading", "tomllib", "unicodedata", "uuid", "webbrowser", "zipfile", "zlib",
]

hiddenimports = (
    collect_submodules("cue")  # providers and players are found at runtime, and plugins import the rest
    + collect_submodules("textual")  # widgets and drivers are imported lazily
    + collect_submodules("rapidfuzz")
    + collect_submodules("httpx")
    + collect_submodules("tomlkit")
    # Casting to AirPlay and Chromecast TVs; pyatv loads its protocols by name.
    + collect_submodules("pyatv", filter=_no_tests)
    + collect_submodules("pychromecast")
    + collect_submodules("zeroconf")
    + [module for package in PLUGIN_STDLIB_PACKAGES for module in collect_submodules(package, filter=_no_tests)]
    + PLUGIN_STDLIB_MODULES
)
if importlib.util.find_spec("yt_dlp") is not None:  # optional downloads
    hiddenimports += collect_submodules("yt_dlp")

datas = collect_data_files("textual")  # Textual's CSS and other assets
# conda-forge's certifi has no cacert.pem of its own: it reads the environment's
# Library\ssl\cacert.pem relative to sys.base_prefix, which is the unpacked exe when frozen.
ca_bundle = Path(certifi.where())
if ca_bundle.parent.name != "certifi":
    datas.append((str(ca_bundle), ca_bundle.parent.relative_to(sys.base_prefix).as_posix()))
datas.append((
    str(root / "cue" / "player" / "vlc_http" / "requests" / "status.json"),
    "cue/player/vlc_http/requests",
))
datas.append((str(root / "cue" / "web" / "static"), "cue/web/static"))  # cue serve's web app, hls.js included

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
    # Rendered from assets/cue-icon.svg. Only Windows programs carry an icon of their own.
    icon=str(root / "assets" / "cue.ico") if sys.platform == "win32" else None,
    console=True,
    upx=False,
    runtime_tmpdir=None,
)
