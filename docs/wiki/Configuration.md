Cue's settings live in `config.toml`, in Cue's folder (see [Files](#files)). Cue writes a
starter file the first time it runs.

You rarely need to edit it by hand. On the Home screen (after `Esc` if you're in the
search box):

- **Settings** (`s`) changes your subtitle languages, whether they load by themselves,
  and whether they're burned in for the Default app.
- **The Plugins menu** (`p`) changes providers, groups, players, subtitle sources and
  plugins.

Both keep your comments and layout. Anything else is set here. Cue checks the file when
it starts and says what's wrong if a setting can't be used.

## A complete example

```toml
default_group = "shows"   # where searches start: movies, shows, anime, all, or your own
auto_skip = true          # skip anime openings and endings
auto_next = true          # play the next episode when one ends
quality = "best"          # "best", "worst" or a height such as "720"
player = "vlc"            # vlc, mpv, other, or a player from a plugin
player_args = []          # extra arguments for VLC or mpv
download_dir = ""         # where `d` saves episodes; default: your Videos folder\Cue

[providers.jellyfin]
url = "https://jellyfin.example.home"
api_key = "..."           # Jellyfin Dashboard -> API Keys

[providers.local]
path = "D:/Media/Shows"   # or a list of folders

[players.mpv]
path = "C:/Tools/mpv/mpv.exe"

[subtitles]
languages = ["en"]
auto_download = true
burn_in = true            # for the Default app: draw them onto the picture

[subtitles.opensubtitles]
api_key = "..."           # free key from opensubtitles.com/consumers

[subtitles.subdl]          # from the subdl plugin
api_key = "..."

[playback]
keep_hours = 0
max_mb = 4096

[cast]
enabled = true

[plugins]
disabled = []

[updates]
check = true
install = true
```

Top-level settings (`player`, `quality` and the others without a `[table]`) must come
before the first `[table]`, as in any TOML file.

## Top-level settings

| Setting | Default | What it does |
|---|---|---|
| `default_group` | the first group | The [search group](Search-Groups) Cue starts in. |
| `auto_skip` | `true` | Skip anime openings and endings ([Skipping](Skipping)). `a` toggles it while watching. |
| `auto_next` | `true` | Play the next episode when one ends. |
| `quality` | `"best"` | `"best"`, `"worst"` or a height such as `"720"` or `"1080"`. Where a source offers only one playlist with every quality inside, the player picks. |
| `player` | `"vlc"` | Which [player](Players) plays episodes. |
| `player_args` | `[]` | Extra arguments for VLC or mpv. VLC starts fullscreen; `["--no-fullscreen"]` gives a window. |
| `download_dir` | Videos\Cue | Where episodes you download with `d` are saved. |
| `default_provider` | | From older versions; Cue starts in that provider's first group. Use `default_group` instead. |

## `[providers.<name>]`

One table per provider, holding its own settings; [Providers](Providers) lists each one's.
A provider is on when its table exists and has what it needs. Two settings work for every
provider:

| Setting | What it does |
|---|---|
| `enabled = false` | Turns the provider off and keeps its settings. |
| `groups = ["shows", "kids"]` | The search groups it's in, instead of its own choice. |

## `[groups.<name>]`

Your own [search groups](Search-Groups), or new titles for the built-in ones:

```toml
[groups.kids]
title = "Kids"
media = "series"     # optional: "movie" or "series", to show only those
```

## `[players.<name>]`

Each [player](Players)'s settings. Every player that runs a program takes `path`, to use a
particular copy:

```toml
[players.vlc]
path = "D:/Portable/VLC/vlc.exe"

[players.other]
path = "C:/Program Files/MPC-HC/mpc-hc64.exe"
arguments = '"{url}" /start {start_ms} /fullscreen'
hls = true
```

## `[subtitles]`

| Setting | Default | What it does |
|---|---|---|
| `languages` | `["en"]` | The languages Cue loads, in order of preference. `pt-br` prefers that region; `pt` takes any. |
| `auto_download` | `true` | Load subtitles by themselves. Off, the subtitle picker (`Shift+C`) still works. |
| `burn_in` | `true` | For players that can't show subtitles by themselves (the Default app, so Windows' Media Player): draw your first language onto the downloaded episode's picture. `false` puts every language inside the file instead, for the app's subtitles button. See [Players](Players#default-app). |

These three are also under Settings (`s` on the Home screen) → Subtitles.

### `[subtitles.<name>]`

Each subtitle source has its own table: `[subtitles.opensubtitles]` for the built-in one,
and `[subtitles.gestdown]` or `[subtitles.subdl]` for those from plugins. The Subtitles tab
in the Plugins menu edits them for you.

| Setting | What it does |
|---|---|
| `api_key` | The key OpenSubtitles and SubDL need. A source without one isn't searched. |
| `enabled` | `false` turns a source off and keeps its other settings. |

A plugin's source can have settings of its own; its form in the Subtitles tab lists them.
See [Subtitles](Subtitles#setting-up-a-source).

## `[playback]`

For players that only open files (such as Windows' Media Player), Cue downloads each
streamed episode first. These settings control those temporary files; see
[Playback and the Local Relay](Playback-and-the-Local-Relay#downloads-for-players-that-only-open-files).

| Setting | Default | What it does |
|---|---|---|
| `folder` | the system's temp folder\cue-playback | Where they go: another drive, say. |
| `quality` | the main `quality` | A smaller version just for these, such as `"720"`. |
| `keep_hours` | `0` | `0` deletes an episode when you stop watching or move on; `48` keeps them two days, for replays. |
| `max_mb` | `4096` | The most they may take together; the oldest go first. `0`: no limit. |

## `[cast]`

Casting to a TV; see [Casting](Casting).

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `false` | Runs Cue's cast server, which casting needs. Restart Cue after changing it. |
| `port` | `8770` | The port TVs connect to. |
| `name` | `"Cue"` | How Cue appears in VLC's Network tab. |
| `interface` | | The network adapter the TV is on, by name (`"Ethernet"`, `"Wi-Fi"`). |
| `host` | | Or a fixed address of this PC on the TV's network. Set `interface` or `host`, not both. |

## `[plugins]`

| Setting | Default | What it does |
|---|---|---|
| `catalog` | Cue's plugin catalog | Where the list of plugins comes from: an https address or a local `index.json`. Empty turns the catalog off. |
| `disabled` | `[]` | Plugins that are installed but turned off. |

## `[updates]`

| Setting | Default | What it does |
|---|---|---|
| `check` | `true` | Look for a new Cue and new plugins once a day. |
| `install` | `true` | Install them; `false` only says what's available. |

See [Updates](Updates).

## Environment variables

| Variable | What it does |
|---|---|
| `CUE_HOME` | Use another folder for everything in [Files](#files): a portable setup, or tests. |
| `CUE_VLC` | VLC's program, when Cue shouldn't look for it. |
| `CUE_MPV` | mpv's program. |
| `CUE_FFMPEG` | FFmpeg's program. |

These point Cue at programs; they don't change which `player` is used.

## Files

Cue keeps everything in one folder: `%APPDATA%\cue\` on Windows and `~/.config/cue/` on
Linux and macOS (or `$XDG_CONFIG_HOME/cue`). `CUE_HOME` moves it.

| File | What's in it |
|---|---|
| `config.toml` | Settings |
| `cue.db` | History, resume points, watched episodes, MAL IDs, skip times, which subtitle each episode uses |
| `subs\` | Downloaded subtitles (`subs\provider\` holds the streaming servers' own) |
| `plugins\` | Installed plugins, one folder each |
| `providers\` | Single-file providers (the older way to add one) |
| `cue.log` | The log: what to look at when something goes wrong |
| `airplay.json` | Pairings with AirPlay TVs that asked for a code |
| `update-check.json` | When Cue last looked for updates |
| `install-declined` | Present after answering *never* to the install question |

Elsewhere:

- The installed `cue.exe` (and the FFmpeg it downloaded) is in `%LOCALAPPDATA%\Programs\Cue`.
- Episodes downloaded with `d` go to `download_dir`.
- Temporary episodes for players that only open files go to `[playback] folder`.
