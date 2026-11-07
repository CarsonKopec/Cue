# Plugins, players and self-install validation — 2026-11-07 (America/New_York)

Both suites passed: **305 tests in Cue** and **316 in cue-plugins**. The plugins and
their tests moved to the separate cue-plugins repository. All earlier HiAnime and
HydraHD tests pass unchanged there against the plugin versions, which are loaded the
way Cue loads installed plugins. cue-plugins also checks that every plugin is valid
for Cue, that its catalog build is reproducible and readable by Cue, and that a
changed plugin can't be published without a new version. New tests cover:

- manifests, version checks and plugins that need a newer Cue
- zip safety: paths outside the folder, absolute and drive paths, backslashes,
  links, size limits, and GitHub-style single top folders
- reproducible plugin zips
- install, update (the new code is re-imported) and a failed update that keeps the
  old version; turning plugins off and on; broken plugins; `lib/`; removal and
  clean-up
- the catalog: relative links, bad entries skipped, checksum mismatch, https-only
  downloads including redirects (each hop is checked before it's requested), and
  local catalogs
- providers turned off, missing and reused; comment-preserving config edits; invalid
  edits refused with the file left unchanged
- the CLI: install from a zip and from a catalog, confirm prompt, update, list,
  disable/enable, remove
- the menu, driven through Textual's pilot:
  - Providers: on/off, the settings form (required field, save) and default provider
  - Players: choosing a player and its path
  - Plugins: install from the catalog, on/off, remove, add from a zip file, and an
    unavailable catalog
- players: the registry and program lookup; `ProcessPlayer`'s arguments, failure
  reports, a program that floods stderr (this once hung), quit and header refusals;
  the ffplay and default-app plugins; playing through a plugin player with a note;
  missing and other-OS players
- install: PATH editing (case, trailing `\`, quotes, `%VARS%`), copying with the
  files beside the exe, registry entries, re-install, uninstall that deletes only
  Cue's files, the real Windows registry (a scratch key), and the first-run question

## Live checks

| Check | Result |
| --- | --- |
| ffplay plugin, 12 s local file | Played to the end (12.8 s) |
| Same file, resume at 8 s | Ended after 4.9 s: started at the resume point |
| Local HTTP stream with Referer and User-Agent | The server received both headers; played in full |
| HTTP stream, resume at 8 s | 4.9 s |
| HTTP 404 | ffplay exited in 0.6 s; Cue reported "Server returned 404 Not Found" |
| Plugin zips built twice | Identical checksums |
| Plugins menu, rendered at 110×30 | Tabs, tables, footer keys, settings form and install confirmation laid out correctly |
| `cue.exe` (frozen), scratch Cue folder | Read a local catalog; installed all four plugins by name and from a zip; listed them; headless start loaded `anime` and `hydra` from plugins; turning `hydra` off removed it |
| `cue.exe` HTTPS | Reached github.com (the unpublished catalog answered 404, not a TLS error) |

Two problems were found and fixed along the way:
- The first 0.2.0 build couldn't start. Its HTTPS client failed because conda-forge's
  newer certifi reads the CA bundle from `Library\ssl\cacert.pem`, which PyInstaller
  didn't bundle. The spec now bundles it, and `build/release.py` runs
  `cue.exe --smoke-test` so a build like that fails.
- Config edits changed a file's line endings on Windows. They now keep LF or CRLF
  as the file had them.

ffplay ran with SDL's dummy video and audio drivers, so nothing appeared on screen.
The default-app plugin was tested with a stand-in opener, not by opening Media Player.
`cue install` was tested against a stand-in registry and a scratch registry key; it
hasn't been run against this PC's real PATH.

# Cast-to-TV validation — 2026-10-31 (America/New_York)

The full suite passed: **565 tests** (30 new for casting). The casting tests run the
relay and DLNA server on a real local socket, with mocked upstream servers and an
HTTP client standing in for the TV. They cover:

- playlist rewriting and segment timing
- header forwarding (credentials only to the source's own host)
- subtitle renditions, including SRT → WebVTT
- media-only streams and local files with ranges
- resume and watched tracking
- error replies and expired casts
- DLNA description and Browse (paging, faults)
- SSDP replies
- `[cast]` config, the `t` key (also from the range box and Now Playing)
- adapter selection: matching by name, the address listened on, missing
  adapters, and the real adapter list on Windows

## Live checks

These ran on this PC with installed **VLC 3.0.23** standing in for the TV, using
HydraHD's Breaking Bad — Season 1.

| Check | Result |
| --- | --- |
| Cue app started with `[cast] enabled = true` | Server on the PC's network address (192.168.40.72) |
| DLNA search (SSDP M-SEARCH for MediaServer) | Cue answered; its description named the server |
| `t` on the Episodes screen | 7 episodes cast; notification showed the VLC path and direct link |
| VLC's own DLNA client (`upnp://…/dlna/cds/control?ObjectID=c1`) | Browsed the folder and played episode 1 in 1080p through the relay |
| Subtitle tracks in VLC | All 8 languages listed by name |
| VLC preferred subtitle language `en` | English selected automatically |
| No preferred language set | Subtitles off; VLC 3 ignores `DEFAULT=YES` on HLS subtitle tracks |
| Resume via `EXT-X-START` | Ignored by VLC 3; removed |
| TV seek to 10:00 / 15:00 | Resume point saved at 9:56 / 14:48 |
| TV seek past 90% | Episode marked watched, resume point cleared |
| Cue exit | Server and announcements stopped |

VLC's DLNA client code was checked for how it reads items. It plays only
`protocolInfo` values starting with `http-get:*:video/`, and Cue's items follow
that. This was not tested on a real Xbox: discovery across the network, the Xbox
VLC app's menus, and on-screen subtitle rendering still need a try on the console.

# HydraHD validation — 2026-10-25 (America/New_York)

The full suite passed: **535 tests in 22.6 seconds** (Windows, Python 3.14.8,
Textual 8.2.8, httpx 0.28.1). `python -m cue --config config-vlc.toml --smoke-test`
started headless with the `anime` and `hydra` providers, both as the default and
with `-p hydra`.

## Automated coverage

- Catalog: search cards, season expansion with stable per-season IDs, movie
  detection, saved-title restoration, catalog redirects limited to HydraHD's own
  domains, page size limits, and challenge and rate-limit pages.
- Server menu: the published play-button request for movies and TV, premium and
  unsupported servers skipped, bounded fallback, same-origin redirects only, and
  no credentials or cookies sent to player hosts.
- VixSrc: published page → API → embed → literal playlist data; computed or
  protected player data refused; origin and path checks at every stage.
- Subtitles: master-playlist renditions become Cue tracks (full subtitles first,
  CC and forced last, invalid URIs dropped); playback continues when the playlist
  cannot be read; single-file subtitle playlists followed; segmented, live, empty
  or invalid ones reported.
- General media: skip lookups disabled by provider, movie titles, and
  OpenSubtitles movie and series searches.

## Live VLC check

The checks ran Cue's real `HydraProvider` and `PlaybackSession` with installed **VLC 3.0.23**,
dummy audio/video output, no OpenSubtitles key, and subtitle file auto-detection off.

| Check | Breaking Bad — Season 1, episode 1 | The Matrix (movie) |
| --- | --- | --- |
| Search and queue | 5 seasons listed; 7-episode queue | 1-item queue |
| Stream | VixSrc HLS, decoded video (1080p H.264 offered) | VixSrc HLS, decoded video |
| Subtitle tracks in playlist | 8 (English, English [CC], German, Greek, French, Italian, Italian [Forced], Romanian) | 3 (English [CC], Italian, Italian [Forced]) |
| VLC selection before Cue | none (`spu-es` −1) | none (`spu-es` −1) |
| Automatic English | English downloaded, loaded and selected (track 3) | English [CC] loaded and selected (track 3) |
| Manual choice via Cue | German selected (track 13) | Italian selected (track 8) |
| Cleanup | Cue-owned VLC process exited | Cue-owned VLC process exited |

This checked VLC's selected track and the subtitle files. It did not inspect what
appeared on screen. It is a brief sample, not a guarantee for other titles, full
playback, or future site changes.

# Earlier subtitles and SUB/DUB validation — 2026-10-21 (America/New_York)

The final full suite passed: **348 tests in 25.93 seconds**. The Python wheel
built successfully; its provider, resolver, subtitle service, session, screens,
and bundled VLC control endpoint match the delivered source. Headless startup
from the packaged wheel passed with the supplied `config-vlc.toml`.

Environment: Windows, Python 3.12.14, pytest 9.1.1, pytest-asyncio 1.4.0,
httpx 0.28.1, Textual 8.2.8. Tests ran in an isolated environment. The sandbox's
temporary-folder restrictions required running checks outside that sandbox.

## Automated coverage

- Existing HiAnime server discovery, episode IDs, public source resolution,
  source selection, request headers, and player tests remain passing.
- SUB/DUB defaults, selector and keyboard use, unavailable variants, immutable
  queue selections, concurrent modes, and distinct download filenames.
- Zoko, JSON, and HTML subtitle metadata, relative URLs, stream inheritance,
  source deduplication, and exclusion of thumbnails or media masquerading as text.
- UTF-8/UTF-16 SRT, WebVTT and ASS downloads, validation, bounded responses,
  atomic caching, duplicate requests, regional languages and misleading labels.
- Header isolation, malformed URLs/headers, error pages, access refusals,
  timeouts and redirect restrictions; no access-control bypass.
- Subtitle loading without an API key, preferred languages, optional fallback,
  automatic loading disabled, queue advancement, and graceful player failures.
- Actual Textual subtitle-picker interaction, SUB/DUB display, stale selections,
  and in-flight automatic/manual selection ordering.

These tests use synthetic HTTP responses, a fake player or mocked VLC controls;
they do not require live streaming sites, VLC, or mpv.

## Live VLC check

Used installed **VLC 3.0.23** and the public HiAnime sample episode ID `15911`
(Black Clover episode 85), through the application's real PlaybackSession.
VLC used dummy audio/video outputs, with subtitle auto-discovery disabled.
No OpenSubtitles API key was configured.

| Check | Result |
| --- | --- |
| SUB playback | Resolved through Zoko; decoded 10 video frames |
| DUB playback | Resolved through Zoko; decoded 28 video frames |
| SUB subtitle discovery | Nine server tracks, with language labels normalized |
| DUB subtitle discovery | One English server track |
| Automatic English subtitles | Downloaded, cached and selected in both modes |
| External-track confirmation | No initial subtitle tracks; new selected track ID 0 appeared after Cue loaded the file |
| Manual French selection on SUB | New track ID 3 selected through PlaybackSession |
| Cleanup | Both Cue-owned VLC processes exited |

Track ID 0 is valid in VLC; -1 means disabled. The test compared the track list
before loading subtitles with the selected track afterward. It verified VLC's
track-selection state and subtitle text files; visual rendering and audible
language were not inspected because the checks used dummy outputs. The source
mode was selected through the advertised SUB/DUB server groups.

This is a brief sample check, not a guarantee for other episodes, full playback,
future site changes, or other VLC versions. Audio and subtitle availability
remain controlled by the selected server. The sampled MegaPlay/HD-1 page
continues to report removed/missing content; successful live MegaPlay playback
has not been verified. Its explicit-source parser is tested offline.

## Retained validation and implementation boundaries

Earlier delivered versions passed 169 HiAnime tests and then 217 tests with VLC.
Real checks verified local playback, seeking, resume, subtitle cycling, natural
completion, automatic next episode and outro skipping. The current suite retains
those automated regressions.

The Zoko resolver follows the public [player](https://zokoanime.video/player.js?v=23),
[encoding module](https://zokoanime.video/core/obfuscate.js?v=23), and
[player core](https://zokoanime.video/core/index.js?v=23). Only normal public data
is parsed; no JavaScript is executed, tokens forged, DRM decrypted, challenge
solved, or access denial bypassed. Only Zoko and MegaPlay embed hosts are supported.

VLC's local endpoint uses its supported subtitle URI API and delegates other
controls to [VideoLAN's control implementation](https://github.com/videolan/vlc/blob/3.0.x/share/lua/intf/modules/httprequests.lua).
It binds to localhost with a new password per process. Installed VLC files and
preferences are not edited. Referer/User-Agent are supported; arbitrary required
media headers, including Jellyfin Authorization, still need optional mpv.

Unknown-duration streams cannot automatically advance on stop. HLS master-only
sources do not expose resolution choices to Cue. SUB/DUB watch history remains
shared, selections persist for the app run, and video downloads do not export
subtitle sidecars. The source archive includes no media, signed source URLs,
downloaded subtitle cache, virtual environment, stale binaries or new Windows exe.
