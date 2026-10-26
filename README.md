# Cue — Terminal Media Player

A keyboard-driven TUI for browsing and watching shows and movies, with automatic
intro/outro skipping for anime, packaged as a single Windows `.exe`.

Inspired by [ani-cli](https://github.com/pystardust/ani-cli) (flow, player handling,
history) and [ani-skip](https://github.com/synacktraa/ani-skip) (AniSkip integration).
Licensed under the GPL-3.0.

- **One full-screen TUI**: search → pick → play → next episode without leaving the app.
- **Source-agnostic**: media comes from providers (Jellyfin, local folders, HiAnime, and HydraHD).
- **Skips anime openings and endings automatically** using the AniSkip community database.
- **Resumes anywhere**: watch history and playback positions are saved locally.
- **Subtitles** from the streaming server, with optional OpenSubtitles fallback.
- **Plays in VLC** by default, with mpv as an option.

## Install

Install [VLC 3 from VideoLAN](https://www.videolan.org/vlc/download-windows.html).
Cue uses VLC by default and detects its standard Windows installation under
`C:\Program Files\VideoLAN\VLC` (or `Program Files (x86)`). A custom installation
can be selected with the `CUE_VLC` environment variable pointing to `vlc.exe`.

**Release zip:** unzip `cue-<version>-win64.zip` anywhere and run `cue.exe`. VLC is
installed separately. A complete portable VLC folder at `cue\vlc\` is also found,
as is VLC on `PATH`.

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

Then run `cue` (or `python -m cue`). `cue frieren` searches straight away; `cue -p local`
starts on a given provider.

The project folder includes a ready-to-run HiAnime/HydraHD/VLC configuration,
[config-vlc.toml](config-vlc.toml). Run from the folder:

```bash
python -m cue --config config-vlc.toml
```

It starts on HiAnime; press `Tab` to switch to HydraHD, or start there with `-p hydra`.
The exe accepts it too: `cue.exe --config <path to>\config-vlc.toml`.

## Setup

On first run Cue writes `%APPDATA%\cue\config.toml`. Fill in at least one provider:

```toml
default_provider = "local"
auto_skip = true
auto_next = true
quality = "best"          # "best", "worst" or a resolution such as "720"
player = "vlc"           # "vlc" (default) or "mpv"
player_args = []          # extra arguments for the selected player
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
The `anime` (HiAnime) and `hydra` (HydraHD) tables are described under [Providers](#providers).

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
| `Tab` | Switch provider |
| `Esc` | Leave a text box |
| `q` | Back / quit |

**Episode ranges** follow ani-cli: `3`, `1-5`, `1 5`, `-1` (latest), `3--1` (3 to the
latest), and several joined with commas (`1-3,7`). Type one in the Episodes screen's
range box and press Enter to play the batch, or `Esc` then `d` to download it.

## Providers

| Provider | Source | Notes |
|---|---|---|
| `jellyfin` | Jellyfin server | REST API with an API key, direct-play streams. Playback currently requires `player = "mpv"` for its authorization header. Each season appears as its own show (`Show`, `Show Season 2`, ...) because MAL numbers episodes per season. AniList/AniDB/Kitsu IDs from the metadata feed the MAL lookup. |
| `local` | Folders on disk | Parses `Show Name - 01.mkv`, `[Group] Show - 01 (1080p).mkv`, `Show.S01E02.mkv`, `Show Episode 3.mkv`, and bare `01.mkv` inside `Show/Season 2/`. Several files for one episode become quality choices. |
| `anime` | HiAnime catalog | Discovers SUB/DUB server choices and resolves supported public player data to media URLs. See the setup and limitations below. |
| `hydra` | HydraHD catalog | Movies and TV. Each season appears as its own show; a movie is a one-item queue. Plays through the site's VixSrc player with its subtitle tracks. See the setup and limitations below. |

### HiAnime setup

Add this table to your Cue configuration, then run `cue -p anime`:

```toml
[providers.anime]
base_api = "https://hianime.at"
language = "sub"          # initial choice: "sub" (default) or "dub"
timeout = 20
```

Set the top-level `default_provider = "anime"` if desired. Existing `search_api`,
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

Add this table to your Cue configuration, then run `cue -p hydra`:

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

### Adding a provider

Implement the protocol in [cue/providers/base.py](cue/providers/base.py) and register the class:

```python
from cue.providers import register
from cue.providers.base import Episode, Show, Stream

@register
class MyProvider:
    name = "mine"

    def __init__(self, options):   # the [providers.mine] table
        ...

    async def search(self, query: str) -> list[Show]: ...
    async def episodes(self, show: Show) -> list[Episode]: ...
    async def stream(self, episode: Episode) -> list[Stream]: ...
```

Cue finds providers three ways: modules inside `cue/providers/`, the `cue.providers`
entry-point group of installed packages, and `*.py` files dropped into
`%APPDATA%\cue\providers\` (this works with the `.exe` too). A provider is enabled when
its `[providers.<name>]` table exists in the config.

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

Good candidates for future providers:

| Idea | Why it fits |
|---|---|
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
| `providers\` | Drop-in provider plug-ins |
| `cue.log` | Log file, useful when something goes wrong |

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

`cue.exe --smoke-test` starts the TUI headless, renders the Home screen and exits. Run it
on a clean machine to check a build.

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
