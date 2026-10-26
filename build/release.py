"""Build dist/cue.exe and a release zip, optionally bundling mpv and yt-dlp.

    python build/release.py --mpv C:/tools/mpv --yt-dlp C:/tools/yt-dlp.exe

``--mpv`` takes an mpv folder (copied to ``cue/mpv/``) or a single ``mpv.exe``;
Cue looks for mpv next to ``cue.exe`` first, then on PATH.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mpv", type=Path, help="mpv folder or mpv.exe to bundle")
    parser.add_argument("--yt-dlp", type=Path, help="yt-dlp.exe to bundle")
    parser.add_argument("--skip-build", action="store_true", help="zip an existing dist/cue.exe")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT))
    from cue import __version__

    exe = ROOT / "dist" / "cue.exe"
    if not args.skip_build:
        subprocess.run(
            [
                sys.executable, "-m", "PyInstaller", str(ROOT / "build" / "cue.spec"), "--noconfirm",
                "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "work"),
            ],
            check=True,
        )
    subprocess.run([str(exe), "--version"], check=True)

    archive = ROOT / "dist" / f"cue-{__version__}-win64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(exe, "cue/cue.exe")
        bundle.write(ROOT / "README.md", "cue/README.md")
        bundle.write(ROOT / "LICENSE", "cue/LICENSE")
        if args.mpv:
            if args.mpv.is_dir():
                for path in sorted(args.mpv.rglob("*")):
                    if path.is_file():
                        bundle.write(path, f"cue/mpv/{path.relative_to(args.mpv).as_posix()}")
            else:
                bundle.write(args.mpv, "cue/mpv.exe")
        if args.yt_dlp:
            bundle.write(args.yt_dlp, "cue/yt-dlp.exe")
    print(f"Wrote {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
