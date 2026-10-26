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
