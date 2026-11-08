"""FFmpeg with cue install: downloaded from gyan.dev, checked, only ffmpeg.exe kept, offered only when missing."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest

from cue import commands
from cue.ffmpeg_setup import ARCHIVE, CHECKSUM, FFmpegError, download_ffmpeg
from cue.install import COMPANIONS

PACKAGE = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip"


def build_zip(program: bytes = b"MZ ffmpeg", name: str = "ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(name, program)
        bundle.writestr("ffmpeg-9.0.2-essentials_build/bin/ffprobe.exe", b"MZ ffprobe")
        bundle.writestr("ffmpeg-9.0.2-essentials_build/LICENSE", b"GPL")
    return buffer.getvalue()


def gyan(archive: bytes, checksum: str | None = None, redirect: str = PACKAGE) -> httpx.Client:
    """gyan.dev as it answers: the checksum, then the archive behind a redirect."""

    def respond(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == CHECKSUM:
            return httpx.Response(200, text=(checksum if checksum is not None else hashlib.sha256(archive).hexdigest()))
        if url == ARCHIVE:
            return httpx.Response(302, headers={"Location": redirect})
        if url == PACKAGE:
            return httpx.Response(200, content=archive, headers={"Content-Length": str(len(archive))})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(respond))


def test_download_keeps_only_ffmpeg_exe(tmp_path: Path) -> None:
    folder = tmp_path / "Cue"
    archive = build_zip()
    seen: list[tuple[int, int]] = []
    path = download_ffmpeg(folder, client=gyan(archive), progress=lambda done, total: seen.append((done, total)))
    assert path == folder / "ffmpeg.exe" and path.read_bytes() == b"MZ ffmpeg"
    assert [p.name for p in folder.iterdir()] == ["ffmpeg.exe"]  # no archive, ffprobe or temporary folder left
    assert seen[-1] == (len(archive), len(archive))

    download_ffmpeg(folder, client=gyan(build_zip(b"MZ newer")))  # again, over the copy already there
    assert path.read_bytes() == b"MZ newer" and [p.name for p in folder.iterdir()] == ["ffmpeg.exe"]


@pytest.mark.parametrize(("client", "message"), [
    (gyan(build_zip(), checksum="0" * 64), "doesn't match"),
    (gyan(build_zip(), checksum="no checksum here"), "no SHA-256"),
    (gyan(build_zip(), redirect="http://www.gyan.dev/plain.zip"), "only downloaded over https"),
    (gyan(build_zip(b"not a program")), "isn't a Windows program"),
    (gyan(build_zip(name="ffmpeg/ffmpeg.exe")), "no bin/ffmpeg.exe"),
])
def test_download_refuses_what_doesnt_check_out(tmp_path: Path, client: httpx.Client, message: str) -> None:
    folder = tmp_path / "Cue"
    with pytest.raises(FFmpegError, match=message):
        download_ffmpeg(folder, client=client)
    assert not folder.exists() or list(folder.iterdir()) == []  # nothing installed, nothing left behind


def test_install_offers_ffmpeg_only_when_cue_cant_find_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    downloads: list[Path] = []

    def fake_download(folder: Path, progress=None) -> Path:
        progress(50, 100)
        downloads.append(folder)
        return folder / "ffmpeg.exe"

    def no_question(question: str) -> str:
        raise AssertionError("asked when it shouldn't")

    monkeypatch.setattr("cue.ffmpeg_setup.download_ffmpeg", fake_download)
    found: list[str | None] = ["C:/tools/ffmpeg.exe"]
    monkeypatch.setattr("cue.player.locate.find_ffmpeg", lambda: found[0])
    folder = tmp_path / "Programs" / "Cue"

    commands.offer_ffmpeg(folder, ask=no_question)  # already on PATH: not asked
    commands.offer_ffmpeg(folder, choice=True, ask=no_question)  # but --ffmpeg puts one next to Cue anyway
    assert downloads == [folder]
    downloads.clear()
    found[0] = None
    commands.offer_ffmpeg(folder, choice=False, ask=no_question)  # cue install --no-ffmpeg
    questions: list[str] = []
    commands.offer_ffmpeg(folder, ask=lambda question: questions.append(question) or "n")
    assert downloads == [] and "gyan.dev" in questions[0] and "Skipped" in capsys.readouterr().out

    commands.offer_ffmpeg(folder, ask=lambda question: "")  # Enter means yes
    commands.offer_ffmpeg(folder, choice=True, ask=no_question)  # cue install --ffmpeg
    assert downloads == [folder, folder] and "FFmpeg is ready" in capsys.readouterr().out

    folder.mkdir(parents=True)
    (folder / "ffmpeg.exe").write_bytes(b"MZ")
    commands.offer_ffmpeg(folder, ask=no_question)  # downloaded before
    assert len(downloads) == 2

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    (folder / "ffmpeg.exe").unlink()
    commands.offer_ffmpeg(folder)  # no one to ask: just say how
    assert "cue install --ffmpeg" in capsys.readouterr().out and len(downloads) == 2


def test_install_flags_and_uninstall_take_ffmpeg_along() -> None:
    parser = commands._parser()
    assert parser.parse_args(["install"]).ffmpeg is None
    assert parser.parse_args(["install", "--ffmpeg"]).ffmpeg is True
    assert parser.parse_args(["install", "--no-ffmpeg"]).ffmpeg is False
    with pytest.raises(SystemExit):
        parser.parse_args(["install", "--ffmpeg", "--no-ffmpeg"])
    assert "ffmpeg.exe" in COMPANIONS  # cue uninstall removes it with Cue
