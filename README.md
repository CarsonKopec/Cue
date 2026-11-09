<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/cue-wordmark-dark.svg">
    <img src="assets/cue-wordmark-light.svg" alt="Cue" width="320">
  </picture>
</p>

# Cue — Terminal Media Player

A keyboard-driven terminal app for finding and watching shows and movies, on Windows,
Linux and macOS. Search, pick an episode and it plays; when it ends, the next one starts.
Anime openings and endings are skipped for you, your place is remembered, and what you
pick can play on a TV.

Inspired by [ani-cli](https://github.com/pystardust/ani-cli) (flow, player handling,
history) and [ani-skip](https://github.com/synacktraa/ani-skip) (AniSkip integration).
Licensed under the GPL-3.0.

**📖 Documentation: [the Cue wiki](https://github.com/CarsonKopec/Cue/wiki)**

## Features

- **One full-screen app**: search → pick → play → next episode without leaving it.
- **Any source**: Jellyfin and folders on your PC are built in; HiAnime, HydraHD and others
  are [plugins](https://github.com/CarsonKopec/Cue/wiki/Plugins) you install from Cue's
  Plugins menu.
- **Searches by group**: Movies, Shows, Anime or your own. One search asks every provider
  in the group at once. [More](https://github.com/CarsonKopec/Cue/wiki/Search-Groups)
- **Skips anime openings and endings** using the AniSkip database.
  [More](https://github.com/CarsonKopec/Cue/wiki/Skipping)
- **Resumes anywhere**: history and positions stay on your PC.
- **Subtitles** from the streaming server, with OpenSubtitles, Addic7ed and SubDL as
  fallbacks. [More](https://github.com/CarsonKopec/Cue/wiki/Subtitles)
- **Any video player**: VLC (the default), mpv, MPC-HC, ffplay, even Windows' Media
  Player. Cue adds headers, joins streams or downloads the episode first, whatever the
  player can't do itself. [More](https://github.com/CarsonKopec/Cue/wiki/Players)
- **Casts to a TV**: smart TVs over DLNA (Samsung, LG, Sony…), VLC on an Xbox, Chromecast
  and Apple TV, with resume and subtitles.
  [More](https://github.com/CarsonKopec/Cue/wiki/Casting)
- **Installs and updates itself**, plugins included, with no administrator rights.
  [More](https://github.com/CarsonKopec/Cue/wiki/Updates)

## Quick start

1. Download Cue for your system from the
   [latest release](https://github.com/CarsonKopec/Cue/releases/latest):

   | System | File |
   |---|---|
   | Windows | `cue.exe` (or `cue-<version>-windows-x64.zip`) |
   | Linux x64 | `cue-linux-x64` |
   | macOS (Apple silicon) | `cue-macos-arm64` |

2. Install [VLC](https://www.videolan.org/vlc/), or see the
   [other players](https://github.com/CarsonKopec/Cue/wiki/Players).
3. Run it. On Windows, Cue offers to install itself and put `cue` on your PATH, and to
   download FFmpeg if you don't have it. On Linux and macOS, put the program on your PATH
   yourself ([Installing](https://github.com/CarsonKopec/Cue/wiki/Installing)).
4. Add somewhere to watch from: on the Home screen press `Esc`, then `p`.
   - **Providers** tab: set up Jellyfin or your own folders.
   - **Plugins** tab: install HiAnime (`anime`) or HydraHD (`hydra`).
5. Type a title, press `Enter`, pick an episode, and press `Enter` again.

| Key | What it does |
|---|---|
| `/` | Search, filter, or type an episode range |
| `Enter` | Choose / play |
| `n` / `p` | Next / previous episode |
| `s` | Skip ahead 85 seconds |
| `Shift+C` | Choose subtitles |
| `t` | Cast to a TV |
| `Tab` | Switch search group |
| `q` | Back / stop |

All the keys are on the [Keys](https://github.com/CarsonKopec/Cue/wiki/Keys) page.

## Documentation

| Using Cue | How it works | Developers |
|---|---|---|
| [Installing](https://github.com/CarsonKopec/Cue/wiki/Installing) | [How Cue Works](https://github.com/CarsonKopec/Cue/wiki/How-Cue-Works) | [Writing a Plugin](https://github.com/CarsonKopec/Cue/wiki/Writing-a-Plugin) |
| [Providers](https://github.com/CarsonKopec/Cue/wiki/Providers) | [Playback and the Local Relay](https://github.com/CarsonKopec/Cue/wiki/Playback-and-the-Local-Relay) | [Building and Releasing](https://github.com/CarsonKopec/Cue/wiki/Building-and-Releasing) |
| [Players](https://github.com/CarsonKopec/Cue/wiki/Players) | [How Casting Works](https://github.com/CarsonKopec/Cue/wiki/How-Casting-Works) | [Development](https://github.com/CarsonKopec/Cue/wiki/Development) |
| [Casting](https://github.com/CarsonKopec/Cue/wiki/Casting) | [How Plugins and Updates Work](https://github.com/CarsonKopec/Cue/wiki/How-Plugins-and-Updates-Work) | |
| [Configuration](https://github.com/CarsonKopec/Cue/wiki/Configuration) | | |
| [Troubleshooting](https://github.com/CarsonKopec/Cue/wiki/Troubleshooting) | | |

[CHANGELOG.md](CHANGELOG.md) lists what each version changed, and
[VALIDATION.md](VALIDATION.md) records the test runs and live playback checks.

## From source

```bash
conda env create -f environment.yml
```

```bash
conda activate cue
```

Then run `cue` (or `python -m cue`), and `python -m pytest` for the tests. Any Python 3.11+
with `pip install -e ".[dev]"` works too. See
[Development](https://github.com/CarsonKopec/Cue/wiki/Development) and
[Building and Releasing](https://github.com/CarsonKopec/Cue/wiki/Building-and-Releasing).

The wiki's pages live in [docs/wiki/](docs/wiki/) and are published to the wiki on every
push, so they change with the code.

Plugins are kept in [cue-plugins](https://github.com/CarsonKopec/cue-plugins);
[Writing a Plugin](https://github.com/CarsonKopec/Cue/wiki/Writing-a-Plugin) shows how to
make one.
