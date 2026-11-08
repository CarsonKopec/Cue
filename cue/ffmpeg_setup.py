"""Getting FFmpeg on Windows: gyan.dev's "essentials" build, put next to the installed cue.exe.

Cue uses FFmpeg to join a stream for players that can't play it themselves, such as
Windows' Media Player through the Default app. ``cue install`` offers to fetch it when
Cue can't already find one (``CUE_FFMPEG``, next to Cue, or on PATH). gyan.dev is one
of the two Windows builds ffmpeg.org links to, and its essentials build has the
smallest ``ffmpeg.exe`` (about 105 MB).

The download (about 115 MB) comes over https only, is checked against the SHA-256
gyan.dev publishes beside it, and is deleted once ``ffmpeg.exe`` is out: ffprobe,
ffplay and the rest aren't kept. ``cue uninstall`` removes it with Cue.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx

from cue import __version__

FFMPEG = "ffmpeg.exe"
ARCHIVE = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
CHECKSUM = ARCHIVE + ".sha256"
DOWNLOAD_MB, INSTALLED_MB = 115, 105  # for the question; the real sizes are checked as it downloads
MAX_ARCHIVE_BYTES = 600 * 1024 * 1024
MAX_REDIRECTS = 5
CHUNK = 1024 * 1024
TIMEOUT = httpx.Timeout(120, connect=15)


class FFmpegError(Exception):
    """FFmpeg couldn't be downloaded or installed; nothing was changed."""


def download_ffmpeg(
    folder: Path, *, client: httpx.Client | None = None, progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Download FFmpeg, check it, and put ``ffmpeg.exe`` in ``folder``; returns its path.

    ``progress(done, total)`` hears about the download in bytes (``total`` is 0 when unknown).
    """
    own = client is None
    http = client or httpx.Client(timeout=TIMEOUT, headers={"User-Agent": f"cue/{__version__}"})
    try:
        expected = _checksum(_get(http, CHECKSUM, 4096).decode("utf-8", "replace"))
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder, prefix="ffmpeg-download-") as work:
            archive = Path(work) / "ffmpeg.zip"
            if _download(http, ARCHIVE, archive, progress) != expected:
                raise FFmpegError("the download doesn't match gyan.dev's checksum; nothing was installed")
            staged = Path(work) / FFMPEG
            _extract(archive, staged)
            archive.unlink()
            target = folder / FFMPEG
            if target.exists():  # Windows won't overwrite a program in use, but will rename it
                retired = target.with_name(f"ffmpeg.old-{uuid.uuid4().hex[:8]}.exe")
                target.rename(retired)
                with contextlib.suppress(OSError):
                    retired.unlink()
            os.replace(staged, target)  # the same drive: a rename, not another copy
        return target
    except httpx.HTTPError as exc:
        raise FFmpegError(f"couldn't reach {urlsplit(ARCHIVE).hostname} ({type(exc).__name__})") from exc
    except (OSError, zipfile.BadZipFile) as exc:
        raise FFmpegError(f"couldn't install it in {folder}: {exc}") from exc
    finally:
        if own:
            http.close()


def _checksum(text: str) -> str:
    match = re.search(r"\b[0-9a-fA-F]{64}\b", text)
    if match is None:
        raise FFmpegError("gyan.dev's checksum file has no SHA-256 in it")
    return match[0].lower()


def _open(http: httpx.Client, url: str) -> httpx.Response:
    """A streamed GET over https only, following redirects one at a time."""
    for _ in range(MAX_REDIRECTS + 1):
        if urlsplit(url).scheme != "https":
            raise FFmpegError(f"FFmpeg is only downloaded over https, not {url}")
        response = http.send(http.build_request("GET", url), stream=True, follow_redirects=False)
        if response.is_redirect:
            response.close()
            url = urljoin(url, response.headers.get("location", ""))
            continue
        if response.status_code != 200:
            response.close()
            raise FFmpegError(f"{urlsplit(url).hostname} answered HTTP {response.status_code}")
        return response
    raise FFmpegError(f"{ARCHIVE} redirects too many times")


def _get(http: httpx.Client, url: str, limit: int) -> bytes:
    with contextlib.closing(_open(http, url)) as response:
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise FFmpegError(f"{url} is larger than expected")
        return bytes(body)


def _download(http: httpx.Client, url: str, path: Path, progress: Callable[[int, int], None] | None) -> str:
    """Save ``url`` to ``path``; returns its SHA-256."""
    digest = hashlib.sha256()
    with contextlib.closing(_open(http, url)) as response, path.open("wb") as out:
        total = int(response.headers.get("content-length") or 0)
        if total > MAX_ARCHIVE_BYTES:
            raise FFmpegError("the FFmpeg download is larger than expected")
        done = 0
        for chunk in response.iter_bytes(CHUNK):
            done += len(chunk)
            if done > MAX_ARCHIVE_BYTES:
                raise FFmpegError("the FFmpeg download is larger than expected")
            digest.update(chunk)
            out.write(chunk)
            if progress is not None:
                progress(done, total)
    return digest.hexdigest()


def _extract(archive: Path, target: Path) -> None:
    """Copy the archive's ``bin/ffmpeg.exe`` (and nothing else) to ``target``."""
    with zipfile.ZipFile(archive) as bundle:
        found = [info for info in bundle.infolist()
                 if PurePosixPath(info.filename).name.lower() == FFMPEG and PurePosixPath(info.filename).parent.name == "bin"]
        if len(found) != 1:
            raise FFmpegError("the download has no bin/ffmpeg.exe in it")
        with bundle.open(found[0]) as source, target.open("wb") as out:
            shutil.copyfileobj(source, out, CHUNK)
    with target.open("rb") as program:
        if program.read(2) != b"MZ":
            raise FFmpegError("the download's ffmpeg.exe isn't a Windows program")
