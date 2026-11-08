"""Build Cue for this platform, test the build, and gather what a release carries for it.

    python build/release.py
    python build/release.py --mpv C:/tools/mpv --yt-dlp C:/tools/yt-dlp.exe

PyInstaller makes one program: ``dist/cue.exe`` on Windows, ``dist/cue`` on Linux and
macOS. It is started with ``--version`` and ``--smoke-test`` (headless, with a throwaway
``CUE_HOME``), so a build that can't run fails here. Then ``dist/release/`` gets exactly
this platform's release files:

* the program under its release name, which ``cue update`` looks for: ``cue.exe``,
  ``cue-linux-x64``, ``cue-macos-arm64``, ...
* ``cue-<version>-<platform>.zip`` (Windows) or ``.tar.gz``: the program in a ``cue``
  folder with the README and licence, and ``--mpv`` / ``--yt-dlp`` if given.

``--mpv`` takes an mpv folder (copied to ``cue/mpv/``) or a single mpv program; Cue looks
for mpv next to itself first, then on PATH.
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mpv", type=Path, help="mpv folder or mpv program to bundle")
    parser.add_argument("--yt-dlp", type=Path, help="yt-dlp program to bundle")
    parser.add_argument("--skip-build", action="store_true", help="package an existing build in dist/")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT))
    from cue import __version__
    from cue.update import asset_name, platform_tag

    windows = sys.platform == "win32"
    program = ROOT / "dist" / ("cue.exe" if windows else "cue")
    if not args.skip_build:
        env = dict(os.environ)
        # A conda environment keeps its DLLs in Library\bin. PyInstaller finds DLLs on PATH, so
        # without the environment activated it could bundle another environment's copy (a
        # libexpat.dll that this Python's pyexpat can't load, say). Put this one's first.
        library_bin = Path(sys.prefix) / "Library" / "bin"
        if windows and library_bin.is_dir():
            env["PATH"] = os.pathsep.join([str(library_bin), env.get("PATH", "")])
        subprocess.run(
            [
                sys.executable, "-m", "PyInstaller", str(ROOT / "build" / "cue.spec"), "--noconfirm",
                "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "work"),
            ],
            check=True, env=env,
        )
    subprocess.run([str(program), "--version"], check=True)
    with tempfile.TemporaryDirectory() as home:  # a start-up check that can't touch real settings
        subprocess.run([str(program), "--smoke-test"], check=True, env={**os.environ, "CUE_HOME": home})

    out = ROOT / "dist" / "release"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    shutil.copy2(program, out / asset_name())

    files: list[tuple[Path, str]] = [(program, f"cue/{program.name}"), (ROOT / "README.md", "cue/README.md"),
                                     (ROOT / "LICENSE", "cue/LICENSE")]
    if args.mpv:
        if args.mpv.is_dir():
            files += [(path, f"cue/mpv/{path.relative_to(args.mpv).as_posix()}")
                      for path in sorted(args.mpv.rglob("*")) if path.is_file()]
        else:
            files.append((args.mpv, f"cue/{args.mpv.name}"))
    if args.yt_dlp:
        files.append((args.yt_dlp, f"cue/{args.yt_dlp.name}"))

    stem = f"cue-{__version__}-{platform_tag()}"
    if windows:
        archive = out / f"{stem}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path, name in files:
                bundle.write(path, name)
    else:
        archive = out / f"{stem}.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for path, name in files:
                info = bundle.gettarinfo(str(path), name)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if path == program:
                    info.mode = 0o755
                with path.open("rb") as data:
                    bundle.addfile(info, io.BufferedReader(data))  # type: ignore[arg-type]
    for path in sorted(out.iterdir()):
        print(f"Wrote {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
