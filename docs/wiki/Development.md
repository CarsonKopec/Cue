Working on Cue itself. To make a plugin instead, see [Writing a Plugin](Writing-a-Plugin).

## Setting up

```bash
conda env create -f environment.yml
```

```bash
conda activate cue
```

That's Python 3.14 from conda-forge with Cue installed in editable mode, plus pytest and
PyInstaller. The environment sets `PYTHONNOUSERSITE=1`, so packages in your per-user
site-packages can't leak into tests or builds. `pip install -e ".[dev]"` in any Python 3.11+
environment works too; that's what CI uses, with Python 3.13.

## Tests

```bash
python -m pytest
```

The tests need no network, VLC or mpv:

- Provider websites and APIs (Jellyfin, HiAnime, AniSkip, OpenSubtitles, GitHub, gyan.dev, ...)
  are answered by `httpx.MockTransport` stand-ins.
- VLC's local control API is mocked, and
  [tests/fake_mpv.py](https://github.com/CarsonKopec/Cue/blob/master/tests/fake_mpv.py)
  stands in for mpv, speaking its JSON IPC on a real named pipe (or Unix socket).
- The app's screens are driven with Textual's test pilot.
- The local relay and cast server run on real local sockets.
- Tests never touch the real PATH, registry, Cue folder (`CUE_HOME` points at a temporary
  one) or network: searching for TVs is switched off.
- FFmpeg tests run when FFmpeg is installed, and are skipped otherwise.

CI runs them on Windows, Linux and macOS ([Building and Releasing](Building-and-Releasing#ci)).
[VALIDATION.md](https://github.com/CarsonKopec/Cue/blob/master/VALIDATION.md) records test
runs and live playback checks.
A test that relies on one system's behaviour, such as Windows' PATH rules or its refusal to
delete files in use, checks `sys.platform`.

## Where things are

| Path | What's there |
|---|---|
| `cue/__main__.py` | Command-line entry: arguments, logging, the first-run install offer, the smoke test |
| `cue/app.py` | The Textual app: shared services (providers, groups, players, subtitles, relay, cast server) and screen routing |
| `cue/screens/` | Home, Results, Episodes, Now Playing, Plugins, and the cast picker |
| `cue/widgets.py` | Shared widgets: vim-key lists and the status bar |
| `cue/providers/` | The provider protocol (`base.py`), the registry, Jellyfin, Local, and helpers for web providers (`web.py`) |
| `cue/groups.py` | Search groups |
| `cue/selection.py` | Episode ranges and quality choice, from ani-cli |
| `cue/player/` | The `Player` protocol, VLC, mpv, programs Cue starts (`process.py`, `command.py`), the player registry, the playback session, and the local relay (`relay.py`) |
| `cue/skip/` | MyAnimeList matching and AniSkip |
| `cue/subtitles/` | Server subtitles, OpenSubtitles, the subtitle source registry (`registry.py`), and the service that searches every source |
| `cue/cast/` | The cast server, SSDP, HLS rewriting, network adapters, AirPlay and Chromecast |
| `cue/plugins/` | Plugin manifests, archives, the catalog and the plugin manager |
| `cue/install.py`, `cue/update.py`, `cue/ffmpeg_setup.py` | Installing, self-updates and getting FFmpeg |
| `cue/config.py` | Reading `config.toml`, and editing it without losing comments |
| `cue/store.py` | The SQLite database |
| `build/` | The PyInstaller spec and release script |
| `docs/wiki/` | These pages |

## Design notes

- **One event loop.** Searches, provider calls, players, skips and the database run on
  Textual's asyncio loop. The local relay and the cast server serve HTTP on their own
  threads, handing provider, subtitle and database work to the loop.
- **VLC** is controlled through VLC 3's HTTP interface on `127.0.0.1`, with a fresh random
  password and a separate VLC process for each episode. Cue polls position, length and
  state, and treats an episode as finished only when it stops near a known end. A small
  status page bundled with Cue uses VLC's Lua subtitle API so a chosen subtitle can be
  selected; it's loaded with `--http-src`, so no installed VLC files change.
- **mpv** is controlled by a small asyncio JSON-IPC client on a named pipe (a Unix socket
  elsewhere). Each episode gets its own mpv with `--keep-open=yes`, so `eof-reached` fires,
  and `--start` when resuming.
- **Casting** rewrites HLS playlists so every request goes through Cue; a piece's start time
  is how Cue follows the TV's position ([How Casting Works](How-Casting-Works)).
- **Players declare what they can do** (`headers`, `hls`, `links`), and the relay steps in
  only where needed ([Playback and the Local Relay](Playback-and-the-Local-Relay)).
- **Registries remember origins.** Providers, players and subtitle sources remember which
  plugin registered them, so a plugin can be turned off, updated or removed cleanly
  ([How Plugins and Updates Work](How-Plugins-and-Updates-Work)).
- **Config edits** go through tomlkit, keep comments and layout, and are written only if the
  result is still a valid config. A file's own line endings are kept.
- **Optional data** on the models: `Show.external_ids` (AniList/AniDB IDs for the MAL lookup),
  `Show.media_type` and `Show.series_title` (movies and per-season shows), `Episode.season`
  (subtitle matching), `Stream.subtitle_tracks` (server subtitles), `SubtitleTrack.id` (a
  source's file ID or a server track's cache key).
- **Open question:** subtitle hash matching works only for local files; Jellyfin streams are
  matched by title and episode.

## Changing these docs

Edit the pages in `docs/wiki/` in the same change as the code they describe. They're
published to the wiki when they reach master ([Building and Releasing](Building-and-Releasing#the-wiki)).
