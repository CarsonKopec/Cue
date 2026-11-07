"""Plugin zip files: packing a plugin folder and unpacking a download safely."""

from __future__ import annotations

import hashlib
import io
import stat
import zipfile
from pathlib import Path, PurePosixPath

from cue.plugins.manifest import MANIFEST, PluginError

MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
MAX_FILES = 2000
_SKIP_PARTS = {"__pycache__"}
_FIXED_TIME = (1980, 1, 1, 0, 0, 0)  # so the same files always give the same zip and checksum


def _packable(path: Path, folder: Path) -> bool:
    parts = path.relative_to(folder).parts
    return path.is_file() and not any(part in _SKIP_PARTS or part.startswith(".") for part in parts) \
        and path.suffix != ".pyc"


def pack(folder: Path, destination: Path) -> str:
    """Zip a plugin folder (files at the top level of the zip) and return its SHA-256."""
    if not (folder / MANIFEST).is_file():
        raise PluginError(f"{folder} has no {MANIFEST}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(p for p in folder.rglob("*") if _packable(p, folder)):
            info = zipfile.ZipInfo(path.relative_to(folder).as_posix(), _FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            bundle.writestr(info, path.read_bytes())
    data = buffer.getvalue()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    name = info.filename
    path = PurePosixPath(name)
    if (
        "\\" in name or name.startswith("/") or ":" in path.parts[0]
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise PluginError(f"the plugin archive contains an unsafe path: {name}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise PluginError(f"the plugin archive contains a link: {name}")
    return path


def extract(data: bytes, destination: Path) -> None:
    """Unpack plugin zip ``data`` into ``destination``.

    ``plugin.toml`` may sit at the top of the zip or inside a single top folder (as in
    GitHub's source downloads). Paths that escape the folder, links, and oversized
    contents are refused.
    """
    if len(data) > MAX_ARCHIVE_BYTES:
        raise PluginError("the plugin archive is too large")
    try:
        bundle = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PluginError("the download is not a zip file") from None
    with bundle:
        members = [info for info in bundle.infolist() if not info.is_dir()]
        if len(members) > MAX_FILES:
            raise PluginError("the plugin archive has too many files")
        if sum(info.file_size for info in members) > MAX_UNPACKED_BYTES:
            raise PluginError("the plugin archive unpacks to too much data")
        paths = [_member_path(info) for info in members]
        if PurePosixPath(MANIFEST) in paths:
            strip = 0
        else:
            tops = {path.parts[0] for path in paths}
            if len(tops) != 1 or PurePosixPath(next(iter(tops)), MANIFEST) not in paths:
                raise PluginError(f"the plugin archive has no {MANIFEST}")
            strip = 1
        written = 0
        for info, path in zip(members, paths):
            parts = path.parts[strip:]
            if not parts or any(part in _SKIP_PARTS for part in parts):
                continue
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("wb") as output:
                while chunk := source.read(65536):
                    written += len(chunk)
                    if written > MAX_UNPACKED_BYTES:  # don't trust the sizes the zip claims
                        raise PluginError("the plugin archive unpacks to too much data")
                    output.write(chunk)
