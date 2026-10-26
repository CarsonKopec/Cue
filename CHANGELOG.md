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
