# Cue 0.3.2: a logo — 2026-11-07

- **Cue has a logo**: a rounded C around a play button, in Cue's orange on dark grey.
  `cue.exe` now carries it as its icon, so Explorer, the taskbar and Settings → Apps
  show it. The README opens with the "Cue" wordmark, with versions for GitHub's light
  and dark themes.
- [assets/](assets/) holds the app icon (SVG and 512 px PNG), the mark on its own, the
  two wordmarks, and `cue.ico` with 16–256 px sizes.

No code changes. The exe was rebuilt with the icon; it still passes the smoke test,
and Windows extracts the new icon from it.

# Cue 0.3.1: install message fix — 2026-11-07

- `cue install` and Cue's other commands no longer crash when the console can't show
  a character such as the "→" in "Settings → Apps". This happened with piped output
  and older consoles on Windows. Such characters now print as `?`. In 0.3.0 the
  install still finished, but the command ended with an error.

Validation: **314 Cue tests passed**, including a new test that runs `cue install`
with its output going to a cp1252 stream; without the fix it fails with the original
error.

# Cue 0.3.0: search groups — 2026-11-07

- **Search by group instead of by provider.** Movies, Shows and Anime are built in,
  plus All; `Tab` switches groups and the status bar names the current one and its
  providers. A search asks every provider in the group at once. Results appear as
  each one answers, ranked against the query and tagged with their provider; a
  provider that fails is listed without hiding the others' results.
- **Groups come from providers.** Each provider names its groups (`groups`, and the
  config's `[providers.<name>] groups = [...]` overrides it), so one provider can be in
  several. Jellyfin and Local are in Shows and Anime; the HiAnime plugin is in Anime;
  HydraHD is in Movies and Shows.
- **Movies vs Shows.** A provider that returns both (`mixed_media = True`, like
  HydraHD) shows only its movies under Movies and only its series under Shows.
- **Groups tab** in the Plugins menu: each group with its providers; tick which
  providers are in a group, rename it, start searches in it (`d`), and make (`n`) or
  remove (`x`) your own. `[groups.<name>]` holds a group's title and optional media;
  `default_group` says where searches start.
- `cue -g <group>` starts in a group; `cue -p <provider>` now searches just that
  provider. An older config's `default_provider` starts in that provider's first group.
- cue-plugins: `anime` and `hydra` 1.1.0 declare their groups.

Validation: **313 Cue tests and 317 cue-plugins tests passed**. New tests cover:
- group membership from classes and the config, including several groups and All
- movie/series splitting for mixed providers
- custom groups, their titles and media
- config checks and edits; removing a group takes it out of every provider
- a merged search across four providers, one of which fails, with filtering and
  opening a result from the right provider
- starting groups, `-g` and `-p`
- the Groups tab: membership, new and removed groups, the starting group

The Groups tab and its dialog were checked in rendered screenshots.

# Cue 0.2.0: plugins, players and self-install — 2026-11-07

- **Plugins.** A plugin is a folder of Python code (`plugin.toml` + `__init__.py`)
  that adds providers, players, or both. Cue installs plugins from a catalog (an
  `index.json` with checksums), from a link to a `.zip`, or from a `.zip` file or
  folder. It shows the source and asks first. Plugins can be turned off, updated and
  removed without restarting Cue.
- **Plugins have their own repository**,
  [cue-plugins](https://github.com/CarsonKopec/cue-plugins), sorted into `providers/`
  and `players/`. Each push there rebuilds the catalog (the `catalog` release), which is
  Cue's default.
- **HiAnime and HydraHD are now plugins** (`anime`, `hydra`), so `cue.exe` ships
  with Jellyfin and Local only. Their shared page-fetching and stream-parsing code is
  now a public toolkit for plugin authors, `cue.providers.web`.
- **Plugins menu** (`p` on the Home screen, or the command palette) with three tabs:
  - Providers: status, turn on/off, choose the default, and a settings form each
    provider describes, so Jellyfin and Local can be set up without editing TOML.
  - Players: choose the player and set where its program is.
  - Plugins: install, update, turn on/off, remove, add from file or link.

  Edits keep the config file's comments and layout.
- **Players from plugins.** `player = "<name>"` accepts any registered player;
  `[players.<name>]` holds its options (`path` for every player). New plugins:
  `ffplay` (FFmpeg's player, with resume position and any header) and `default-app`
  (opens local files in the system's default video app on Windows, macOS or Linux).
  `ProcessPlayer` makes wrapping other programs a few lines.
- **`cue install` / `cue uninstall`.** `cue.exe` copies itself to
  `%LOCALAPPDATA%\Programs\Cue`, adds that folder to the user's PATH and appears in
  Settings → Apps, with no administrator rights. A downloaded `cue.exe` offers this
  (or an update) on first run; *never* stops the question.
- **`cue plugin`** list, catalog, install, update, remove, enable and disable.
- `[providers.<name>] enabled = false` turns a provider off and keeps its settings.
  A config table for a provider that isn't installed is reported, not silently
  ignored.
- tomli-w was replaced by tomlkit (which preserves comments). `cue.exe` now bundles
  all of Cue and the standard-library modules plugins commonly need.

Validation: **305 Cue tests and 316 cue-plugins tests passed**. Installed ffplay played a local file and
an HTTP stream: it started at the resume point and sent the stream's Referer and
User-Agent, and Cue reported a 404 as an error. The menu was checked in rendered
screenshots. See [VALIDATION.md](VALIDATION.md).

Remaining limits: plugins run with the user's own access (there's no sandbox), so
Cue asks before installing and checks catalog checksums. The default catalog has
not been published yet. Players Cue can't control (ffplay, the default app) have no
auto-skip and save no resume point. Self-install is Windows-only; pip installs
already put `cue` on PATH.

# Cue cast-to-TV update — 2026-10-31

- **Cast to a TV:** with `[cast] enabled = true`, Cue runs a DLNA media server on
  the home network. Press **t** on the Episodes screen to cast the episodes (or a
  typed range). VLC on an Xbox, or another DLNA player, lists them under Network →
  Cue and plays them through Cue's relay.
- The relay resolves each stream when the TV opens it and adds the source's
  headers. It rewrites HLS playlists so every request goes through Cue. A
  source's credentials go only to its own host.
- Each episode's subtitles are added to the stream as named, language-tagged
  tracks in VLC's subtitle menu.
- Cue follows the parts of the video the TV fetches to save approximate resume
  points and watched markers. The TV's episode list shows watched (✓) and
  `(resume m:ss)`.
- `http://<PC>:8770/now` opens the newest cast without the Network tab. Local
  files are served with seeking (HTTP ranges).
- **t** on Now Playing moves playback to the TV: the PC player stops, the
  position is saved, and the current and following episodes are cast. On the
  Episodes screen **t** and **d** now also work while the range box has focus.
- `[cast] interface = "Ethernet"` (or `host = "<address>"`) chooses the network
  adapter the TV uses. With either set, Cue listens only there; a missing adapter
  is reported with the list of connected ones. `cue.log` records what the TV
  requested.

Validation: **565 tests passed**. On this PC, Cue answered a DLNA search on its
network address. Real VLC 3.0.23 opened a cast through VLC's own DLNA client and
played Breaking Bad S1E1 in 1080p. Seeking it to 15:00 saved a 14:48 resume point.
See [VALIDATION.md](VALIDATION.md).

Remaining limits: VLC 3 ignores playlist hints that turn subtitles on or start
playback at a resume point. Subtitles come on automatically only when VLC's
preferred subtitle language matches, and resuming means seeking on the TV.
Auto-skip doesn't apply while casting. Not yet tried on a real Xbox.

`dist/cue.exe` and `dist/cue-0.1.0-win64.zip` were rebuilt with casting (PyInstaller
6.22.3). The exe's headless startup passed with the user configuration, and its
cast server listened on the configured Ethernet adapter.

# Cue HydraHD update — 2026-10-25

- Added a `hydra` provider for HydraHD movies and TV. Each series season is its
  own show, so history and resume positions never collide between seasons; a
  movie is a one-item queue. Saved titles restore movie/season details on resume.
- Plays through the site's advertised VixSrc player, using its published public
  page and API requests to reach the HLS playlist. Referer/User-Agent go to VLC.
  Challenge pages, access refusals, unsupported servers and off-origin redirects
  stop with a clear error. No JavaScript runs and no challenge is bypassed.
- **Subtitles:** VixSrc lists its subtitle tracks in the HLS playlist and marks
  them all off, so VLC alone showed none. Cue now loads the preferred language
  automatically, prefers full subtitles over CC and forced tracks, and lists
  every track in the **Shift+C** picker. Single-file HLS subtitle playlists are
  followed to their WebVTT file.
- AniSkip lookups are disabled for HydraHD so movies and TV are never skipped on
  anime timings. Movies are labelled as movies, not "Episode 1". The optional
  OpenSubtitles fallback searches movies by title/year and series by series title.
- `config-vlc.toml` now also enables HydraHD; press **Tab** to switch to it.

Validation: **535 tests passed**; headless startup passed with both providers.
Real VLC 3.0.23 played Breaking Bad S1E1 and The Matrix from HydraHD with English
subtitles loaded and selected automatically, and switched each to another
language through Cue. See [VALIDATION.md](VALIDATION.md).

Remaining limits: only the VixSrc server is supported; site markup, domains, or
server menus may change. VLC's Subtitle menu shows both the stream's own tracks
and the one Cue loaded. Subtitle links are signed per playback, so the small file
is downloaded again each time. The audio language follows the playlist default
(English on the samples); VLC's Audio menu can change it.

`dist/cue.exe` and `dist/cue-0.1.0-win64.zip` were rebuilt with HydraHD (PyInstaller
6.22.3); the exe's headless startup passed with the user configuration.

## Earlier subtitles and SUB/DUB update — 2026-10-21

- Added a visible SUB/DUB selector and **b** shortcut on the episode screen.
  Queues retain their selected version; unavailable variants produce a clear
  error. Download names include the version. Existing discovery fixes remain.
- Loads the server's preferred subtitles in VLC without an OpenSubtitles API key.
  Reads Zoko and explicit JSON/HTML tracks; supports SRT, WebVTT, and ASS.
- Corrects inconsistent server language tags using visible labels, preserves
  regional preferences, and caches validated subtitle files.
- **Shift+C** chooses server or optional OpenSubtitles tracks; **c** cycles loaded
  tracks. Manual selections take priority over background loading, and stale
  choices cannot affect the next episode.
- OpenSubtitles remains an optional fallback. The supplied configuration selects
  VLC, starts in SUB mode, and automatically loads English subtitles.

Validation: **348 tests passed**; package build and packaged startup passed.
Real VLC 3.0.23 played the public sample in both SUB and DUB, selected newly
downloaded English subtitles, and switched the SUB stream to French.

Remaining limits: audio/subtitle availability depends on the server; watch
history is shared between SUB and DUB; subtitle sidecars are not exported with
video downloads. Access restrictions are respected. MegaPlay's sampled removed
file and unsupported server formats remain unresolved. This delivery contains
updated source, not a rebuilt Windows executable.

## Earlier VLC update

- VLC is now the default player. Detects the installed Windows VLC, `CUE_VLC`,
  portable app folders, and PATH. Optional mpv remains selectable.
- Added VLC playback, resume, seek/replay, intro/outro skipping, progress saving,
  automatic next episode, and subtitle loading, selection, and cycling.
- Uses an isolated VLC process with an authenticated control endpoint on
  localhost. It does not control a separately opened VLC instance.
- Preserves HiAnime discovery/resolution and forwards its Referer/User-Agent.
  Legacy `--fs` settings are translated for VLC.
- Includes `config-vlc.toml` for launching the HiAnime provider with VLC.

Validation: **217 tests passed**. Installed VLC 3.0.23 passed real headless checks
for local playback, subtitle selection, resume, seek, natural completion,
automatic next episode, and outro skipping. A live resolved HiAnime stream
decoded video, resumed at 30 seconds, and sought to 45 seconds. The bundled
control endpoint is included in Python packages and the Windows build spec.

Limitations: VLC cannot forward arbitrary HTTP headers such as Jellyfin's
Authorization header; unsupported headers produce an error. Unknown-duration
streams do not automatically advance on stop. Other unchanged HiAnime limits
are described below. This is source, not a rebuilt Windows executable.

## Earlier HiAnime update (historical)

- Preserved `Cue-fixed.zip` server discovery, including base64 server URLs,
  language labels, and stable deduplication; malformed URLs now fail safely.
- Added a ZokoAnime resolver for its public v23 player configuration. Returns
  actual media URLs with playback headers through Cue's existing Stream model.
- Added explicit HTML/JSON source parsing for MegaPlay, with a clear error for
  its observed missing-file page. No live MegaPlay playback claim is made.
- Added SUB/DUB selection (`language = "sub"` by default), server failure
  isolation, and correct episode-ID discovery when the cache is empty.
- Rejects challenges, access denials, unsupported protected sources, oversized
  responses, and redirects outside the server's origin. Catalog credentials
  are isolated from third-party servers; downloaded JavaScript is never run.
- Restored the README, license, environment file, design notes, and build
  scripts omitted from `Cue-fixed.zip`.

Validation: **169 tests passed**, package build passed, and headless Cue startup
passed. A live ZokoAnime sample resolved successfully, returned valid HLS master
and variant playlists, and decoded a video frame in the original archive's mpv.
See [VALIDATION.md](VALIDATION.md) for the scope of those checks.

Remaining limits: the sampled MegaPlay file is unavailable; unsupported server
formats require another adapter; provider subtitles were not yet loaded; a master-only
HLS source has no Cue resolution choices; playback does not switch servers after
mpv starts. This delivery is updated source, without a rebuilt Windows executable.
