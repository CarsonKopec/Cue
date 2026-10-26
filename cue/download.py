"""Optional episode downloads through yt-dlp, as in ani-cli.

Uses the ``yt_dlp`` module when it is installed (``pip install cue[download]``),
otherwise a ``yt-dlp`` executable next to ``cue.exe`` or on PATH.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cue.player.locate import beside_app
from cue.providers.base import Stream, is_local_file

ProgressCallback = Callable[[float], None]

_PERCENT = re.compile(r"\[download\]\s+([\d.]+)%")


class DownloadError(Exception):
    """The episode could not be downloaded."""


def find_ytdlp() -> str | None:
    return beside_app("yt-dlp.exe", "yt-dlp") or shutil.which("yt-dlp")


async def download(stream: Stream, folder: Path, name: str, progress: ProgressCallback | None = None) -> Path:
    """Save ``stream`` as ``folder/name.<ext>``; ``progress`` receives percentages."""
    if is_local_file(stream.url):
        raise DownloadError("Already on disk")
    folder.mkdir(parents=True, exist_ok=True)
    template = str(folder / f"{name}.%(ext)s")

    try:
        import yt_dlp
    except ImportError:
        yt_dlp = None
    if yt_dlp is not None:
        loop = asyncio.get_running_loop()

        def report(percent: float) -> None:
            if progress is not None:
                loop.call_soon_threadsafe(progress, percent)

        await asyncio.to_thread(_download_with_module, yt_dlp, stream, template, report)
        return folder

    executable = find_ytdlp()
    if executable is None:
        raise DownloadError("yt-dlp not found: install it or put yt-dlp.exe next to cue.exe")
    args = [executable, "--newline", "-o", template]
    for header, value in (stream.headers or {}).items():
        args += ["--add-header", f"{header}:{value}"]
    args += ["--", stream.url]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None
    last_line = ""
    async for raw in process.stdout:
        line = raw.decode(errors="replace").strip()
        if line:
            last_line = line
        match = _PERCENT.search(line)
        if match and progress is not None:
            progress(float(match.group(1)))
    if await process.wait() != 0:
        raise DownloadError(f"yt-dlp failed: {last_line}")
    return folder


def _download_with_module(yt_dlp: Any, stream: Stream, template: str, report: ProgressCallback) -> None:
    def hook(status: dict[str, Any]) -> None:
        if status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            if total:
                report(100.0 * status.get("downloaded_bytes", 0) / total)

    options = {
        "outtmpl": template,
        "http_headers": dict(stream.headers or {}),
        "progress_hooks": [hook],
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([stream.url])
    except Exception as exc:
        raise DownloadError(f"yt-dlp failed: {exc}") from exc
