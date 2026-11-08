<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/cue-wordmark-dark.svg">
    <img src="assets/cue-wordmark-light.svg" alt="Cue" width="320">
  </picture>
</p>

# Cue — Terminal Media Player

A keyboard-driven TUI for browsing and watching shows and movies, with automatic
intro/outro skipping for anime, packaged as a single Windows `.exe`.

Inspired by [ani-cli](https://github.com/pystardust/ani-cli) (flow, player handling,
history) and [ani-skip](https://github.com/synacktraa/ani-skip) (AniSkip integration).
Licensed under the GPL-3.0.

- **One full-screen TUI**: search → pick → play → next episode without leaving the app.
- **Source-agnostic**: media comes from providers. Jellyfin and local folders are built
  in; HiAnime, HydraHD and others are plugins you install from Cue's Plugins menu.
- **Searches by group**: Movies, Shows, Anime or your own groups. A search asks every
  provider in the group at once, and one provider can be in several groups.
- **Skips anime openings and endings automatically** using the AniSkip community database.
- **Resumes anywhere**: watch history and playback positions are saved locally.
- **Subtitles** from the streaming server, with optional OpenSubtitles fallback.
- **Plays in VLC** by default, or mpv; plugins add other players (ffplay, your system's
  default app).
- **Casts to a TV**: VLC on an Xbox (or another DLNA player) plays what you pick in Cue.
- **Installs itself**: `cue install` puts it on your PATH, no administrator rights needed.

## Install

Install [VLC 3 from VideoLAN](https://www.videolan.org/vlc/download-windows.html).
Cue uses VLC by default and detects its standard Windows installation under
`C:\Program Files\VideoLAN\VLC` (or `Program Files (x86)`). A custom installation
can be selected with the `CUE_VLC` environment variable pointing to `vlc.exe`.

**Release zip:** download `cue-<version>-win64.zip` (or just `cue.exe`) from the
[latest release](https://github.com/CarsonKopec/Cue/releases/latest), unzip it and run
`cue.exe`. `SHA256SUMS.txt` in the release lists their checksums. The first time,
it asks whether to install itself:

```
Install Cue for this user and add it to PATH, so `cue` works in any terminal? [Y/n/never]
```

Yes copies `cue.exe` (and the `mpv`, `vlc` or `yt-dlp.exe` files beside it) to
`%LOCALAPPDATA%\Programs\Cue`, adds that folder to your user PATH, and lists Cue in
Settings → Apps. No administrator rights are needed. Open a new terminal and type
`cue`. A newer download offers to update the installed copy instead. *never* stops
the question; delete `%APPDATA%\cue\install-declined` to bring it back. To install
without the question, run `cue.exe install` from the unzipped folder.

`cue uninstall` (or Uninstall in Settings → Apps) removes the program and its PATH
entry. Your settings, history and plugins in `%APPDATA%\cue` are kept. Cue also runs
fine without installing, straight from the unzipped folder.

VLC is installed separately. A complete portable VLC folder at `cue\vlc\` is also
found, as is VLC on `PATH`.

**From source**, with conda:

```bash
conda env create -f environment.yml
```

```bash
conda activate cue
```

This creates a `cue` environment from conda-forge (Python 3.14, the dependencies, pytest
and PyInstaller) with Cue installed in editable mode. After pulling changes to
`environment.yml`, run `conda env update -f environment.yml --prune`. Plain
`pip install -e ".[dev]"` in any Python 3.11+ environment works too.

Then run `cue` (or `python -m cue`). `cue frieren` searches straight away; `cue -g movies`
starts in a group, and `cue -p local` searches just one provider. pip already put `cue`
on PATH, so `cue install` is only for `cue.exe`.

The project folder includes a ready-to-run HiAnime/HydraHD/VLC configuration,
[config-vlc.toml](config-vlc.toml). HiAnime and HydraHD are plugins, so install them
once from the catalog (or, with [cue-plugins](https://github.com/CarsonKopec/cue-plugins)
cloned next to Cue, from `../cue-plugins/providers/anime` and `.../hydra`), then run
from the folder:

```bash
cue plugin install anime --yes
```

```bash
cue plugin install hydra --yes
```

```bash
python -m cue --config config-vlc.toml
```

It starts in the Anime group (HiAnime); `Tab` moves on to All, Movies and Shows (HydraHD).
`-g movies` starts in Movies.
The exe accepts it too: `cue.exe --config <path to>\config-vlc.toml`.

## Setup

On first run Cue writes `%APPDATA%\cue\config.toml`. The quickest way to set up a
provider is the Plugins menu: on the Home screen press `Esc`, then `p` (see
[Plugins, providers and players](#plugins-providers-and-players)). It edits the config
for you and keeps your comments. Or fill in at least one provider by hand:

```toml
default_group = "shows"   # where searches start: movies, shows, anime, all, or your own
auto_skip = true
auto_next = true
quality = "best"          # "best", "worst" or a resolution such as "720"
player = "vlc"           # "vlc" (default), "mpv", or a player a plugin adds
player_args = []          # extra arguments for VLC or mpv
download_dir = ""         # default: %USERPROFILE%\Videos\Cue

[providers.jellyfin]
url = "https://jellyfin.example.home"
api_key = "..."           # Dashboard → API Keys
# user_id = "..."         # only needed for servers older than 10.9

[providers.local]
path = "D:/Media/Shows"   # or a list of folders

[subtitles]
languages = ["en"]
auto_download = true

[subtitles.opensubtitles]
api_key = "..."           # free key from opensubtitles.com/consumers
```

A provider with empty settings is left out, and the Home screen says what is missing.
`enabled = false` in a provider's table turns it off and keeps its settings. The
`anime` (HiAnime) and `hydra` (HydraHD) tables are described under [Providers](#providers).

VLC starts fullscreen; use `player_args = ["--no-fullscreen"]` for a window.
Keep `player` and `player_args` above the provider tables, since they are top-level
settings. An older config without `player` now selects VLC; its old `--fs` argument
is translated to VLC's fullscreen option. Cue does not rewrite an existing config.
Review any other old mpv arguments before using them with VLC.

To keep the optional mpv backend, install [mpv](https://mpv.io/installation/) and set
`player = "mpv"`. Cue finds `mpv.exe` beside `cue.exe`, on `PATH`, or through
`CUE_MPV`. Jellyfin playback currently requires this backend (see limitations below).

## Keys

| Key | Action |
|---|---|
| `/` | Focus search (filter on Results, range on Episodes) |
| `↑ ↓` / `j k` | Move (`h l` too in the episode grid) |
| `Enter` | Select / play |
| `n` / `p` | Next / previous episode |
| `r` | Replay current |
| `s` | Manual skip (+85 s) |
| `a` | Toggle auto-skip |
| `c` / `C` | Cycle subtitles / subtitle picker |
| `b` (episodes) | Switch HiAnime SUB/DUB; also available in the visible selector |
| `d` | Download episode(s) |
| `t` | Cast the episodes (or the typed range) to a TV; while playing, move playback to the TV. See [Casting to a TV](#casting-to-a-tv) |
| `Tab` | Switch search group (Movies, Shows, Anime, ...) |
| `p` (Home) | Plugins, providers, groups and players menu (also in the command palette, `Ctrl+P`) |
| `Esc` | Leave a text box |
| `q` | Back / quit |

**Episode ranges** follow ani-cli: `3`, `1-5`, `1 5`, `-1` (latest), `3--1` (3 to the
latest), and several joined with commas (`1-3,7`). Type one in the Episodes screen's
range box and press Enter to play the batch, or `Esc` then `d` to download it.

## Search groups

You search a group, not a single provider. The status bar shows which one, and `Tab`
switches between them:

| Group | Has by default | Notes |
|---|---|---|
| Movies | HydraHD | Providers that list both movies and series show only their movies here. |
| Shows | HydraHD, Jellyfin, Local | ...and only their series here. |
| Anime | HiAnime, Jellyfin, Local | |
| All | every provider that's ready | Shown when it adds something. |

A search asks every provider in the group at once. Results arrive as each one answers,
ranked against what you typed, each tagged with its provider (`[hydra]`). A provider
that fails is listed under the results, and the others still show. A group with no
ready provider is skipped by `Tab`.

Each provider says which groups it belongs to, and one provider can be in several.
Change that, or make your own groups, in the Plugins menu's **Groups** tab, or in the
config:

```toml
default_group = "kids"

[providers.local]
path = "D:/Media"
groups = ["shows", "kids"]     # replaces the provider's own choice

[groups.kids]
title = "Kids"
media = "series"               # optional: "movie" or "series", as for Movies and Shows
```

`cue -g anime` starts in a group; `cue -p hydra` searches just one provider.
`default_provider` in an older config still works: Cue starts in that provider's first
group.

## Plugins, providers and players

On the Home screen press `Esc`, then `p`. The menu has four tabs; `←` and `→` switch
between them.

| Tab | What it shows | Keys |
|---|---|---|
| **Providers** | Every provider Cue knows: built in, from a plugin, or named in your config but not installed, with its groups. Status says ready, off, or what setup is missing. | `Enter` settings form · `Space` on/off |
| **Groups** | Every group and its providers, including empty ones. | `Enter` choose its providers (and rename it) · `d` start searches here · `n` new group · `x` remove one of your own |
| **Players** | VLC, mpv and players from plugins, and whether each program was found on this PC. | `Enter` where the program is · `d` play with this player |
| **Plugins** | Installed plugins, then the catalog's plugins you don't have yet. | `Enter` install or update · `Space` on/off · `a` add from a file or link · `x` remove · `r` refresh the catalog |

The settings form comes from the provider itself: Jellyfin asks for its address and API
key, Local for its folders. Saving writes the provider's table in `config.toml` (your
comments stay) and restarts just that provider, so there's no need to restart Cue.

### Installing plugins

A plugin is a folder of Python code that adds providers, players or both. Cue installs
one from:

- **the catalog**: a list of plugins with checksums, read from the address in
  `[plugins] catalog` (by default the `plugins` release of this repository). Cue
  refuses a download whose checksum doesn't match.
- **a link** to a plugin `.zip` (https only),
- **a `.zip` file or a folder** on this PC.

Before installing, Cue shows where the plugin comes from and asks. A plugin runs with
your account's access like any program, so install only plugins you trust. Installing
turns its providers on; removing one turns them off and keeps their settings.

The same works from a terminal:

```bash
cue plugin catalog
```

```bash
cue plugin install anime
```

`cue plugin install <name | link | .zip | folder>` installs, `cue plugin update`
updates everything the catalog has newer, `cue plugin list` shows what's installed,
and `cue plugin remove|enable|disable <name>` do the rest. Add `--yes` to skip the
question. (To search for a show called "install", type `cue -- install`.)

```toml
[plugins]
catalog = "https://github.com/CarsonKopec/cue-plugins/releases/download/catalog/index.json"
disabled = []             # plugins that are installed but turned off
```

`catalog` can also be a local `index.json`; an empty value turns the catalog off.

## Providers

| Provider | Source | Notes |
|---|---|---|
| `jellyfin` | Jellyfin server | REST API with an API key, direct-play streams. Playback currently requires `player = "mpv"` for its authorization header. Each season appears as its own show (`Show`, `Show Season 2`, ...) because MAL numbers episodes per season. AniList/AniDB/Kitsu IDs from the metadata feed the MAL lookup. |
| `local` | Folders on disk | Parses `Show Name - 01.mkv`, `[Group] Show - 01 (1080p).mkv`, `Show.S01E02.mkv`, `Show Episode 3.mkv`, and bare `01.mkv` inside `Show/Season 2/`. Several files for one episode become quality choices. |
| `anime` | HiAnime catalog (**`anime` plugin**) | Discovers SUB/DUB server choices and resolves supported public player data to media URLs. See the setup and limitations below. |
| `hydra` | HydraHD catalog (**`hydra` plugin**) | Movies and TV. Each season appears as its own show; a movie is a one-item queue. Plays through the site's VixSrc player with its subtitle tracks. See the setup and limitations below. |

`jellyfin` and `local` are built in. `anime` and `hydra` come from plugins of the same
name, kept in the [cue-plugins](https://github.com/CarsonKopec/cue-plugins) repository.

### HiAnime setup

Install the `anime` plugin (Plugins menu → Plugins tab, or `cue plugin install anime`).
That adds an empty `[providers.anime]` table, which uses these defaults; change them
in the Providers tab or in the config. HiAnime is in the Anime group (`cue -g anime`):

```toml
[providers.anime]
base_api = "https://hianime.at"
language = "sub"          # initial choice: "sub" (default) or "dub"
timeout = 20
```

Set the top-level `default_group = "anime"` if desired. Existing `search_api`,
`episodes_api`, `servers_api`, `agent`, and catalog `headers` options remain
available. Catalog credentials are not forwarded to external player servers.

On the episode screen, use the **SUB / DUB selector** or press **b** before
starting playback. SUB chooses original audio; DUB chooses the advertised dubbed
version. Cue keeps that choice for the whole queue and reports when the selected
version is unavailable. It never silently switches audio versions. The choice is
remembered for the current app run; `language` sets the default for the next run.
Watch/resume history is shared between versions. Download names include `[SUB]`
or `[DUB]` so the versions do not overwrite each other.

The provider preserves base64 `data-hash` server discovery, resolves ZokoAnime's
publicly delivered `window.__P` player configuration, and accepts explicit media
sources on supported server pages. Its Zoko decoder mirrors the fixed byte
encoding in the site's [public player module](https://zokoanime.video/core/obfuscate.js?v=23);
it does not execute downloaded JavaScript, invent source APIs, or bypass login,
HTTP access denials, DRM, or challenges. Unsupported formats produce a provider
error. Alternate servers are tried within the selected language.

Resolved streams carry their playback Referer and User-Agent into VLC, the optional
mpv backend, and downloads. HLS master playlists stay intact so the player can
choose a variant. If the
server publishes only a master playlist, Cue has no per-resolution choices to
rank, so its quality setting cannot force a resolution for that source.

Known limitations: host markup or public encoding may change; unavailable files
cannot be recovered. The saved MegaPlay/HD-1 example currently
reports a removed or missing file; generic explicit-source parsing is covered
offline, but live MegaPlay playback has not been verified. Resolution can try
another server; Cue does not automatically switch servers after playback has started.

### HydraHD setup

Install the `hydra` plugin, the same way. HydraHD is in the Movies and Shows groups:
its movies appear under Movies and its series under Shows. Its table's defaults are:

```toml
[providers.hydra]
base_url = "https://hydrahd.com"   # the default; the site redirects to its current domain
timeout = 20
```

Search finds movies and series. A series is listed once per season
(`Breaking Bad — Season 1`, `— Season 2`, ...), so history and resume positions stay
separate per season, like the Jellyfin provider. A movie opens as a single item.

Cue reads the episode page's advertised server menu and plays the first supported one.
Currently that is **VixSrc**, resolved through its published public page and API
requests to an HLS playlist. Its Referer/User-Agent are forwarded to VLC. The playlist
lists the stream's subtitle tracks (often English, CC, and several other languages),
which Cue loads like server subtitles (see [Subtitles](#subtitles)). Other advertised
servers are not supported; one of them shows a browser challenge, which Cue does not
attempt to pass. Up to six supported servers are tried before Cue reports an error.

AniSkip lookups are turned off for HydraHD, so movies and live-action TV are never
skipped on anime timings. `s` still jumps 85 seconds. The stream's audio language is
the one its playlist marks as default (English on the sampled titles); use VLC's Audio
menu to change it.

Known limitations: HydraHD's markup, domains, or server menu may change; the old
`hydrahd.ru` address did not connect during testing. Cue has no SUB/DUB selector for
HydraHD because the site does not offer one.

[CHANGELOG.md](CHANGELOG.md) lists what each update changed;
[VALIDATION.md](VALIDATION.md) records the test runs and live playback checks.

## Players

| Player | From | What Cue can do with it |
|---|---|---|
| VLC | built in (default) | Everything: resume, auto-skip, auto-next, subtitle choice. Sends only Referer and User-Agent headers. |
| mpv | built in | Everything, and any HTTP header a source needs (Jellyfin's login, for one). |
| ffplay | `ffplay` plugin | Starts at your resume point and sends any header. Cue can't control it while it plays, so there's no auto-skip and no new resume point; closing its window returns to Cue. Needs [FFmpeg](https://ffmpeg.org/download.html). |
| Default app | `default-app` plugin | Opens files on this PC (Local provider, downloads) in your system's default video app: the app chosen in Windows' Default apps, macOS `open`, or Linux `xdg-open`. Streams need VLC or mpv. Press `q` in Cue when you're done. |

Pick one in the Players tab (`d`), or set `player = "<name>"` in the config. Cue finds
each program by itself; to point at a specific copy, set its path in the Players tab
(`Enter`) or in the config:

```toml
[players.ffplay]
path = "C:/Tools/ffmpeg/bin/ffplay.exe"
fullscreen = true
```

For a player Cue can't control, the Now Playing screen says so; `n` still moves to the
next episode.

## Writing a plugin

A plugin is a folder with a `plugin.toml` and an `__init__.py`:

```
my-plugin/
  plugin.toml
  __init__.py      # imported as cue_plugins.my_plugin
  helpers.py       # optional: import it as `from .helpers import x`
  lib/             # optional: pure-Python packages the plugin needs
```

```toml
[plugin]
name = "my-plugin"            # lowercase letters, digits, - and _
version = "1.0.0"
title = "My Plugin"
description = "One line for the Plugins menu."
author = "you"
homepage = "https://github.com/you/my-plugin"
requires = "0.2.0"            # the oldest Cue it works with
providers = ["mine"]          # what it registers, shown before it's installed
players = []
```

Plugins can import the Python standard library and what Cue itself uses (`cue`,
`httpx`, `rapidfuzz`, `textual`). `cue.exe` bundles those, so a plugin works the same
from the exe and from source. Anything else goes, pure Python, in `lib/`.

**A provider** implements the protocol in [cue/providers/base.py](cue/providers/base.py)
and registers its class:

```python
from cue.providers import register
from cue.providers.base import Episode, Setting, Show, Stream

@register
class MyProvider:
    name = "mine"
    title = "My Source"
    groups = ("movies", "shows")  # the search groups it's in, unless the config says otherwise
    mixed_media = True            # search returns both; Show.media_type tells them apart
    settings = (                  # the form in the Providers tab
        Setting("url", "Server address", required=True, help="Such as http://nas:8096"),
        Setting("token", "Token", "secret"),
    )

    def __init__(self, options):   # the [providers.mine] table
        ...

    async def search(self, query: str) -> list[Show]: ...
    async def episodes(self, show: Show) -> list[Episode]: ...
    async def stream(self, episode: Episode) -> list[Stream]: ...
```

`Setting` kinds are `text`, `secret`, `number`, `bool`, `choice` (with `choices`) and
`folders`. Raise `ProviderConfigError` from `__init__` when a setting is missing or
wrong; the menu shows the message. [cue/providers/web.py](cue/providers/web.py) has
helpers for providers that read web pages: `fetch_page` (fresh requests, same-origin
redirects, size limits), `streams_from_payload` (media and subtitle rows from JSON or
HTML), and the challenge and DRM checks the HiAnime and HydraHD plugins use.

**A player** registers a `PlayerBackend`. For a program Cue can start but not control,
subclass `ProcessPlayer` and build its command line:

```python
import shutil
from cue.player.process import ProcessPlayer
from cue.player.registry import PlayerBackend, register_player

class MyPlayer(ProcessPlayer):
    title = "My Player"
    headers = frozenset({"referer", "user-agent"})   # what it can send; None for any

    def arguments(self, url, *, start, headers, title, extra_args):
        return [f"--start={start or 0}", f"--title={title or ''}", url]

register_player(PlayerBackend(
    name="myplayer", title="My Player",
    locate=lambda options: shutil.which("myplayer"),
    create=lambda program, options: MyPlayer(program, options),
    missing="Install My Player from example.com.",
    controls=False,                  # Cue can't follow its position
    platforms=("win32",),            # sys.platform values; empty for all
))
```

A player that Cue can control implements the whole `Player` protocol in
[cue/player/base.py](cue/player/base.py), as [cue/player/mpv.py](cue/player/mpv.py) and
[cue/player/vlc.py](cue/player/vlc.py) do. The
[ffplay](https://github.com/CarsonKopec/cue-plugins/tree/main/players/ffplay) and
[default-app](https://github.com/CarsonKopec/cue-plugins/tree/main/players/default-app)
plugins are complete examples.

**Trying and sharing it:** `cue plugin install path/to/my-plugin` installs the folder.
To list it in the catalog, add it to
[cue-plugins](https://github.com/CarsonKopec/cue-plugins) under `providers/` or
`players/`. Every push there rebuilds the catalog.

Older ways to add a provider still work: a single `*.py` file in
`%APPDATA%\cue\providers\`, or a Python package with a `cue.providers` entry point.
A provider is on when its `[providers.<name>]` table exists, unless `enabled = false`.

A few optional details make a provider fit in better:

- Set `skip_supported = False` on the class for sources that aren't anime, so Cue
  doesn't look up AniSkip timings.
- Return a movie as a `Show` with `media_type="movie"` and a single `Episode`. Cue then
  labels it as a movie, and OpenSubtitles searches it by title and year.
- For a season of a series, set `Show.series_title` to the series name so subtitle
  searches don't include the season label.
- Attach subtitle files as `Stream.subtitle_tracks` (`StreamSubtitle` entries with a
  URL, language and label). Cue downloads the preferred language and lists the rest
  in the Shift+C picker.
- With VLC, a stream can only require `Referer` and `User-Agent` headers. Sources
  that need a login header or cookie must put the credential in the URL or use mpv.

Good candidates for future plugins:

| Idea | Why it fits |
|---|---|
| IINA (macOS), Celluloid (Linux) | Both are built on mpv and accept its options, so they can reuse Cue's mpv control. |
| MPC-HC / MPC-BE (Windows) | Start at a position with `/start`; their optional web interface reports position and accepts seeks. |
| PotPlayer (Windows) | Takes a start position and title on its command line. |
| yt-dlp | One provider for YouTube playlists and channels, Internet Archive, Twitch VODs, Vimeo, PeerTube and other sites yt-dlp supports. Sites that serve audio and video separately need mpv. |
| Plex | Plex accepts its token as an `X-Plex-Token` URL parameter, so VLC can play it directly. |
| Emby | Jellyfin is a fork of Emby, so most of the Jellyfin provider carries over. |
| Internet Archive | Public-domain films and TV through a public JSON API, with plain MP4 links. |
| IPTV / M3U | Channel groups as shows and channels as episodes, from a playlist file or URL. Live channels have no duration, so resume and auto-next don't apply. |

DRM-protected services (Netflix, Crunchyroll, Disney+, Max, Prime Video) can't be
supported: neither VLC nor Cue can play their encrypted streams.

## How skipping works

Skip data comes only from [AniSkip](https://api.aniskip.com), the database ani-skip uses.

1. **MAL ID**: the provider's own MAL ID, else its AniList ID (AniList API) or AniDB/Kitsu
   ID ([ARM](https://arm.haglund.dev) mapping), else a title search on MAL's prefix search
   (as ani-skip does) with Jikan as a fallback. A title match must clear a fuzzy-score
   threshold, because no skip data is better than wrong skip data.
2. **Skip times** from `GET /v2/skip-times/{mal_id}/{episode}?types[]=op&types[]=ed&episodeLength=0`.
3. Both are **cached** in SQLite.
4. During playback Cue watches the player's position and seeks to the end of an
   interval as soon as the position enters it. No player add-on is needed.

No match or no data means normal playback, with "no skip data" on the Now Playing
screen. `s` always jumps 85 seconds. Providers for general movies and TV, such as
HydraHD, turn the lookup off.

## Subtitles

Cue reads the subtitle tracks published by the selected streaming server,
downloads preferred languages, and selects the first in VLC. **No OpenSubtitles
account is needed for server subtitles.** Both SUB and DUB can have subtitles;
availability depends on that version's server. Subtitles are separate from the
audio-version selector.

HydraHD's tracks are listed inside the stream's HLS playlist and marked not to show
by default, so VLC alone would display none. Cue loads the preferred language from
that list and prefers full subtitles over CC/SDH and forced tracks. VLC's Subtitle
menu then lists both the stream's own copies and the one Cue loaded.

`[subtitles] languages = ["en"]` defaults to English. Generic language codes
accept regional variants; a specific code such as `pt-br` prefers that region.
The server's visible language label takes precedence when its language code is
incorrect. Unsupported or missing tracks are reported without stopping video.
SRT, WebVTT, and ASS files are validated and cached under `subs\provider\`. An HLS
subtitle playlist that holds a single complete file is followed to that file;
segmented or live subtitle playlists are reported and remain available in VLC's menu.

Press **Shift+C** in Cue to choose another server track or optional OpenSubtitles
result; press **c** to cycle loaded tracks. The picker remains available when
`auto_download = false`. A manual choice applies to the current episode; the next
episode uses the configured preferred languages. Embedded subtitles remain
available through VLC's Subtitle menu and track cycling. Cue does not create
translations or subtitles where the source provides none.

With an OpenSubtitles API key configured, Cue searches it if no preferred server
track could be loaded. Local files use an exact file-hash match when available;
other searches use title + season/episode. Those files are stored under
`subs\<show>\<episode>.<lang>.srt` and recorded in SQLite. The picker also includes
OpenSubtitles results when the service is available. Episode downloads currently
save the video; server subtitles are cached for Cue playback, not exported as
sidecar files next to downloaded videos.

## Casting to a TV

VLC on an Xbox can play what you pick in Cue, as can VLC on a phone or tablet or
another DLNA player on your home network. VLC on Xbox can't be controlled by
another program, so Cue doesn't push playback to it. Instead, Cue runs a small
DLNA media server: what you cast appears in VLC's **Network** tab, and the TV plays
it through Cue, which adds the headers and subtitles the source needs.

Turn it on in your config and restart Cue:

```toml
[cast]
enabled = true
port = 8770               # the TV connects to this port on your PC
name = "Cue"              # how Cue appears in VLC's Network tab
# interface = "Ethernet"  # the network adapter the TV is on, by name
# host = "192.168.1.20"   # or this PC's address on that network
```

By default Cue listens on every network adapter and tells the TV to use the one
the PC uses for the internet. If the PC also has Wi-Fi, a VPN or virtual adapters
(Hyper-V, WSL, VirtualBox), choose the adapter the TV shares a network with:

- `interface` names it as Windows' network settings do (`Ethernet`, `Wi-Fi`) and
  follows its address if the router changes it. If no connected adapter has that
  name, Cue says so and lists the ones it found.
- `host` names a fixed address instead. Set one or the other, not both.

With either set, Cue listens only on that adapter, so other networks (such as a VPN)
can't reach it.

The first time, Windows asks whether to allow Cue (`cue.exe`, or `python.exe` when
running from source) on networks. Allow it on **private networks**. Your home
network must also be set to Private in Windows, or the TV can't find Cue.

To watch:

1. In Cue, open a show and press `t`. That casts every episode; type a range first
   (such as `3-5`) to cast only those. Already watching on the PC (for example from
   Continue Watching)? Press `t` on the Now Playing screen: Cue stops the PC player,
   saves your place, and casts that episode and the ones after it.
2. On the Xbox, open VLC → **Network** → **Cue** → the show → an episode.

Keep Cue running on the PC while you watch, because the video passes through it.

- **Controls** are on the TV: pause, seek and choose subtitles with the controller.
  Auto-skip doesn't apply while casting.
- **Subtitles:** each episode's subtitle tracks appear in VLC's subtitle menu, named
  by language. VLC switches one on automatically only when its preferred subtitle
  language setting matches; otherwise choose it from the menu.
- **Resume and watched markers:** Cue follows which part of the video the TV fetches,
  saving an approximate resume point (within about 20 seconds) and marking episodes
  watched near the end. VLC starts every episode from the beginning, so the episode
  list shows `(resume 12:34)`; seek there with the controller.
- **No Network tab?** Open `http://<your PC's address>:8770/now` with VLC's
  open-network-stream option. It plays the newest cast; Cue shows the exact link
  when you press `t`. It only works while Cue is open and after you've pressed `t`.
- **Can't connect?** Open `http://<your PC's address>:8770/` in the Xbox's Edge
  browser. If Cue's page loads, the network path works. `cue.log` lists each request
  the TV made (lines starting `cast`).
- The five newest casts stay listed. Sources whose headers VLC can't send, such as
  Jellyfin's, are relayed with them, though this hasn't been tested against a live
  Jellyfin server.
- Anyone on your network can play what you cast, so use it only on a trusted home
  network.

## VLC limitations

- Cue forwards the Referer and User-Agent needed by HiAnime and HydraHD streams.
  Other required HTTP headers, including Authorization and Cookie, are rejected
  with an error. This means the current Jellyfin provider needs `player = "mpv"`.
- Subtitle files requiring unsupported formats or a redirect to another host
  cannot be loaded automatically. The subtitle picker can select another track.
- Auto-next requires VLC to report a known duration and a stop near the end of that
  duration. A stopped stream with unknown duration does not automatically advance.
  Use `n` to move to the next episode.

## Files

Everything lives in `%APPDATA%\cue\` (set `CUE_HOME` to use another folder):

| File | Contents |
|---|---|
| `config.toml` | Settings |
| `cue.db` | History, resume positions, watched markers, MAL IDs, skip times, subtitle cache |
| `subs\` | Downloaded subtitles |
| `plugins\` | Installed plugins, one folder each |
| `providers\` | Single-file provider modules (the older way to add one) |
| `cue.log` | Log file, useful when something goes wrong |
| `install-declined` | Present after answering *never* to the install question |

An installed `cue.exe` lives in `%LOCALAPPDATA%\Programs\Cue`.

`CUE_VLC` points Cue at a specific VLC binary; `CUE_MPV` does the same for the
optional mpv backend. These do not change the selected `player` setting.

## Building

With the `cue` environment active:

```bash
python build/release.py --yt-dlp C:/tools/yt-dlp.exe
```

Without activating it, prefix the command with `conda run -n cue`. This runs PyInstaller with [build/cue.spec](build/cue.spec) (one file, console attached,
Textual's assets included), checks `dist/cue.exe --version`, and writes
`dist/cue-<version>-win64.zip`. Install VLC separately on the target machine.
The optional `--mpv C:/tools/mpv` flag still bundles an mpv folder as `mpv\`, or accepts
a single `mpv.exe`, for users selecting `player = "mpv"`. Both bundle flags are optional.

The logo lives in [assets/](assets/):

| File | What it is |
|---|---|
| `cue-icon.svg`, `cue-icon.png` | The app icon (512 px PNG) |
| `cue-mark.svg` | The mark alone, on a transparent background |
| `cue-wordmark-dark.svg`, `cue-wordmark-light.svg` | The "Cue" wordmark, for dark and light backgrounds |
| `cue.ico` | The icon at 16–256 px, built into `cue.exe` |

`cue.ico` is rendered from `cue-icon.svg`. Re-render it at those sizes if the icon changes.

`cue.exe --smoke-test` starts the TUI headless, renders the Home screen and exits. Run it
on a clean machine to check a build.

The release script runs that smoke test too, with a throwaway Cue folder, so a build
that can't start fails.

### Releasing

The [Release workflow](.github/workflows/release.yml) builds `cue.exe` on GitHub's
Windows runners and publishes it, only when you start it:

1. Raise `__version__` in [cue/__init__.py](cue/__init__.py) and add its entry at the
   top of [CHANGELOG.md](CHANGELOG.md). That entry becomes the release notes.
2. Push to master, then start the workflow: Actions → Release → Run workflow, or

   ```bash
   gh workflow run release.yml
   ```

It runs the tests, builds with `build/release.py` (smoke test included), tags the commit
`v<version>` and attaches `cue.exe`, `cue-<version>-win64.zip` and `SHA256SUMS.txt`. A
version that's already released is refused. The workflow has options for a draft
(publish it yourself after checking), a pre-release, and a dry run that only tests and
builds and keeps the files as a downloadable workflow artifact:

```bash
gh workflow run release.yml -f dry_run=true
```

Plugins are built and published separately, from the
[cue-plugins](https://github.com/CarsonKopec/cue-plugins) repository: each push to its
`main` branch rebuilds the catalog.

## Development

With the `cue` environment active:

```bash
python -m pytest
```

The environment sets `PYTHONNOUSERSITE=1`, so packages in your per-user site-packages
can't leak into tests or builds. The spec also excludes numpy (pulled in by conda-forge's
rapidfuzz) so its MKL libraries stay out of `cue.exe`.

The tests run without external network access, VLC, or mpv. VLC's local control API
and provider HTTP APIs are mocked, and
[tests/fake_mpv.py](tests/fake_mpv.py) stands in for mpv, speaking its JSON IPC on a real
named pipe. Tests cover player selection, VLC arguments and controls, launch, resume,
auto-skip, auto-next, the TUI flow, each provider's parsing and stream resolution
(against synthetic HTTP responses), and subtitle loading.

### Implementation notes

Places where the code fills in details the original design notes left open:

- **VLC control** uses VLC 3's HTTP interface on `127.0.0.1`, with a fresh random
  password and a separate player process for each episode. Cue polls position,
  duration and playback state; it infers completion only from a stop near a known
  end. Resume, seek and subtitle commands share the playback-session interface with
  mpv. VLC's existing preferences are not changed. Skip messages appear in Cue's
  status; the VLC backend does not display mpv-style `show-text` overlays. A small
  status endpoint bundled with Cue uses VLC's supported Lua subtitle API to honor
  explicit selection. It is loaded through `--http-src`; no installed VLC files are
  modified, and the full VLC web interface is not bundled.
- **Optional mpv control** uses a small asyncio JSON-IPC client on the named pipe (one of the two
  options considered) so it shares Textual's event loop. Each episode gets its own mpv
  process with `--keep-open=yes`, so `eof-reached` fires, and `--start` when resuming.
- **Casting** ([cue/cast/](cue/cast/)) runs a threaded HTTP server and SSDP announcer next
  to Textual's event loop. Provider, subtitle and database work is handed to that loop.
  HLS playlists are rewritten so every playlist, segment and subtitle request goes
  through Cue; a segment's start time is how Cue follows the TV's position.
- **Watched markers** need per-episode data, so the database has a `watched` table next to
  `history`, `mal_ids`, `skip_times` and `subtitles`.
- **Data models** gained optional fields: `Show.external_ids` (AniList/AniDB IDs for the
  MAL lookup), `Show.media_type` and `Show.series_title` (movies and per-season shows),
  `Episode.season` (subtitle matching), `Stream.subtitle_tracks` (server subtitles), and
  `SubtitleTrack.id` (the OpenSubtitles file ID or a server track's cache key).
  `SubtitleSource.find` takes an optional `file_path` for hash matching.
- **Open question (hash matching for Jellyfin streams):** for now only local files are
  hashed. Jellyfin streams are matched by title + episode.
- **Config** gained `download_dir`. Jellyfin accepts an optional `user_id`.
- **Plugins** ([cue/plugins/](cue/plugins/)) are imported as `cue_plugins.<name>`
  packages. The registries remember which plugin registered each provider and player,
  so turning a plugin off, updating or removing it drops exactly its own, and its
  modules are re-imported fresh. Providers whose class and settings didn't change keep
  running. An install is unpacked into a staging folder and swapped in, so a failed
  install leaves the previous version working. Zips are checked for paths outside the
  folder, links and size before anything is written.
- **Config edits** from the menu go through tomlkit, so comments and layout survive,
  and are written only if the result is still a valid config.
