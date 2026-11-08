Cue is one program. Download the one for your system from the
[latest release](https://github.com/CarsonKopec/Cue/releases/latest):

| System | Program | Or the archive |
|---|---|---|
| Windows (64-bit) | `cue.exe` | `cue-<version>-windows-x64.zip` |
| Linux (x64) | `cue-linux-x64` | `cue-<version>-linux-x64.tar.gz` |
| macOS (Apple silicon) | `cue-macos-arm64` | `cue-<version>-macos-arm64.tar.gz` |

The archives hold the same program with the README and licence. `SHA256SUMS.txt` in the
release lists every file's checksum, if you want to check your download.

You also need a video player. Cue uses [VLC 3](https://www.videolan.org/vlc/) by
default; see [Players](Players) for the others.

## Windows

1. Install [VLC](https://www.videolan.org/vlc/download-windows.html). Cue finds it in
   `C:\Program Files\VideoLAN\VLC` (or `Program Files (x86)`), in a portable `vlc\`
   folder next to `cue.exe`, or on `PATH`. To use a particular copy, set `CUE_VLC` to its
   `vlc.exe`.
2. Run `cue.exe`. The first time, it asks:

   ```
   Install Cue for this user and add it to PATH, so `cue` works in any terminal? [Y/n/never]
   ```

   - **Yes** copies `cue.exe` (with any `mpv`, `vlc` or `yt-dlp.exe` beside it) to
     `%LOCALAPPDATA%\Programs\Cue`, adds that folder to your own PATH, and lists Cue in
     Settings → Apps. No administrator rights are needed. Open a new terminal and type
     `cue`.
   - **No** just starts Cue. It runs fine from any folder without installing.
   - **never** stops the question. Delete `%APPDATA%\cue\install-declined` to bring it
     back.

   Running a newer download later offers to update the installed copy instead. To
   install without the question, run `cue.exe install`.
3. **FFmpeg.** Cue then offers to download [FFmpeg](https://ffmpeg.org), which it needs to
   play streams in players that can't on their own (such as Windows' Media Player). It
   doesn't ask when it already finds FFmpeg, on PATH for example. See
   [FFmpeg](#ffmpeg) below.

`cue uninstall`, or Uninstall in Settings → Apps, removes the program, the FFmpeg it
downloaded and its PATH entry. Your settings, history and plugins in `%APPDATA%\cue`
stay, in case you come back.

### FFmpeg

Cue uses FFmpeg to join a stream into one file or one continuous stream (see
[Playback and the Local Relay](Playback-and-the-Local-Relay)). VLC and mpv don't need
it; Windows' Media Player, ffplay and players without HLS do.

On Windows, `cue install` downloads it for you. It's
[gyan.dev](https://www.gyan.dev/ffmpeg/builds/)'s "essentials" build, the smallest Windows
FFmpeg: about 115 MB to download and 105 MB installed.

- It's downloaded over https only and checked against the SHA-256 that gyan.dev
  publishes beside it. Anything that doesn't match is thrown away.
- Only `ffmpeg.exe` is kept, next to `cue.exe`. The archive, ffprobe and ffplay are
  deleted.

| Command | What it does |
|---|---|
| `cue install` | Asks, if Cue can't find FFmpeg anywhere |
| `cue install --ffmpeg` | Downloads it without asking, even when another copy is on PATH |
| `cue install --no-ffmpeg` | Never downloads it |

Cue looks for FFmpeg in this order: the `CUE_FFMPEG` environment variable, next to Cue,
then `PATH`.

## Linux

```bash
chmod +x cue-linux-x64 && mv cue-linux-x64 ~/.local/bin/cue
```

Any folder on your `PATH` works. Install VLC (or mpv) and FFmpeg with your package
manager; Cue finds them on `PATH`. `cue install` is for Windows only.

## macOS

The program isn't signed, so Gatekeeper refuses it the first time. Clear the flag macOS
puts on downloads, then put it on your `PATH`:

```bash
xattr -d com.apple.quarantine cue-macos-arm64
```

```bash
chmod +x cue-macos-arm64 && mv cue-macos-arm64 /usr/local/bin/cue
```

Install VLC (or mpv) and FFmpeg, for example with [Homebrew](https://brew.sh).

## From source

With [conda](https://docs.conda.io):

```bash
conda env create -f environment.yml
```

```bash
conda activate cue
```

That creates a `cue` environment from conda-forge (Python 3.14, the dependencies, pytest
and PyInstaller) with Cue installed in editable mode. After pulling changes to
`environment.yml`, run `conda env update -f environment.yml --prune`. Plain
`pip install -e ".[dev]"` in any Python 3.11+ environment works too.

Then run `cue` (or `python -m cue`). pip already put `cue` on your `PATH`, so
`cue install` is only for `cue.exe`. A source checkout doesn't update itself; see
[Updates](Updates).

### A ready-made setup

The repository has [config-vlc.toml](https://github.com/CarsonKopec/Cue/blob/master/config-vlc.toml),
a configuration for HiAnime, HydraHD and VLC. HiAnime and HydraHD are plugins, so install
them once, then start Cue with that file:

```bash
cue plugin install anime --yes
```

```bash
cue plugin install hydra --yes
```

```bash
cue --config config-vlc.toml
```

It starts in the Anime group (HiAnime); `Tab` moves to All, Movies and Shows (HydraHD).

## Starting Cue

| Command | What it does |
|---|---|
| `cue` | Opens on the Home screen |
| `cue frieren` | Searches straight away |
| `cue -g movies` | Starts in a [search group](Search-Groups) |
| `cue -p local` | Searches one provider only |
| `cue --config path\to\config.toml` | Uses another config file |
| `cue --version` | Prints the version |

The other commands are `cue install`, `cue uninstall`, `cue update` (see
[Updates](Updates)) and `cue plugin …` (see [Plugins](Plugins)). To search for a show
called "install", type `cue -- install`.

Next: set up a [provider](Providers), so Cue has something to search.
