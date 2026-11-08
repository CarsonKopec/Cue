A plugin adds providers (places to watch from), players, subtitle sources, or any mix of
them. It's plain Python, and works the same in `cue.exe` and from source.
[How Plugins and Updates Work](How-Plugins-and-Updates-Work) explains how Cue installs and
loads one.

## The folder

```
my-plugin/
  plugin.toml
  __init__.py      # imported as cue_plugins.my_plugin
  helpers.py       # optional: import it as `from .helpers import x`
  lib/             # optional: pure-Python packages the plugin needs
```

```toml
[plugin]
name = "my-plugin"            # lowercase letters, digits, - and _, starting with a letter
version = "1.0.0"
title = "My Plugin"
description = "One line for the Plugins menu."
author = "you"
homepage = "https://github.com/you/my-plugin"
requires = "0.2.0"            # the oldest Cue it works with
providers = ["mine"]          # what it registers, shown before it's installed
players = []
subtitles = []                # subtitle sources; needs requires = "0.5.0" or later
```

### What a plugin can import

- Anything in `cue` (some modules, such as `cue.player.process`, exist for plugins).
- What Cue itself uses: `httpx`, `rapidfuzz`, `textual` and `tomlkit`.
- The standard library modules `cue.exe` bundles: the common ones (`asyncio`, `json`,
  `re`, `html`, `urllib`, `xml`, `hashlib`, `base64`, `subprocess`, `zipfile` and more).
  The full list is in [build/cue.spec](https://github.com/CarsonKopec/Cue/blob/master/build/cue.spec).
- Anything else must be pure Python, in the plugin's `lib/` folder.

## A provider

A provider implements the protocol in
[cue/providers/base.py](https://github.com/CarsonKopec/Cue/blob/master/cue/providers/base.py)
and registers its class:

```python
from cue.providers import register
from cue.providers.base import Episode, ProviderConfigError, Setting, Show, Stream

@register
class MyProvider:
    name = "mine"                 # its [providers.mine] table
    title = "My Source"
    groups = ("movies", "shows")  # its search groups, unless the config says otherwise
    mixed_media = True            # search returns both; Show.media_type tells them apart
    settings = (                  # the form in the Providers tab
        Setting("url", "Server address", required=True, help="Such as http://nas:8096"),
        Setting("token", "Token", "secret"),
    )

    def __init__(self, options):  # the [providers.mine] table
        if not options.get("url"):
            raise ProviderConfigError("Set the server address")
        ...

    async def search(self, query: str) -> list[Show]: ...
    async def episodes(self, show: Show) -> list[Episode]: ...
    async def stream(self, episode: Episode) -> list[Stream]: ...
```

### The data

| Type | Fields that matter |
|---|---|
| `Show` | `id`, `title`, `year`; `media_type` (`"episode"` or `"movie"`); `series_title` for a season of a series; `mal_id` or `external_ids` (`{"AniList": "21"}`) for skipping |
| `Episode` | `show_id`, `number` (a float, so 12.5 works), `title`, `season` |
| `Stream` | `url`, `headers`, `quality`, `subtitle_tracks` |
| `StreamSubtitle` | `url`, `language`, `label`, `headers` |

### Settings

`Setting(key, label, kind, default, choices, help, required)`. The kinds are `text`,
`secret` (hidden while typing), `number`, `bool`, `choice` (one of `choices`) and
`folders`. Raise `ProviderConfigError` from `__init__` when a setting is missing or wrong;
the Providers tab shows the message.

### Reading web pages

[cue/providers/web.py](https://github.com/CarsonKopec/Cue/blob/master/cue/providers/web.py)
has helpers for providers that read websites:

- `fetch_page`: fresh requests, redirects kept to the same site, size limits.
- `streams_from_payload`: media and subtitle rows from JSON or HTML.
- The challenge and DRM checks the HiAnime and HydraHD plugins use, so a provider reports
  them instead of trying to get around them.

### Fitting in

- Set `skip_supported = False` for sources that aren't anime, so Cue doesn't look up
  AniSkip timings.
- Return a movie as a `Show` with `media_type="movie"` and a single `Episode`. Cue labels it
  as a movie, and subtitle sources search it by title and year.
- For a season of a series, set `Show.series_title` to the series' name, so subtitle
  searches don't include the season label.
- Attach subtitle files as `Stream.subtitle_tracks`. Cue downloads your preferred
  language and lists the rest in the `Shift+C` picker.
- Put the headers a stream needs in `Stream.headers`. Cue sends them itself, or relays the
  stream for players that can't ([Playback and the Local Relay](Playback-and-the-Local-Relay)).

## A player

A player registers a `PlayerBackend`. For a program Cue can start but not control,
subclass `ProcessPlayer` and build its command line:

```python
import shutil
from cue.player.process import ProcessPlayer
from cue.player.registry import PlayerBackend, register_player

class MyPlayer(ProcessPlayer):
    title = "My Player"

    def arguments(self, url, *, start, headers, title, extra_args):
        return [f"--start={start or 0}", f"--title={title or ''}", url]

register_player(PlayerBackend(
    name="myplayer", title="My Player",
    locate=lambda options: shutil.which("myplayer"),
    create=lambda program, options: MyPlayer(program, options),
    missing="Install My Player from example.com.",
    controls=False,                  # Cue can't follow its position
    platforms=("win32",),            # sys.platform values it runs on; empty for all
    headers=frozenset({"referer"}),  # the headers it can send; None for any
    hls=True,                        # False: Cue gives it one continuous stream
    links=True,                      # False: Cue downloads streamed episodes for it first
))
```

`headers`, `hls` and `links` tell Cue where to step in; see
[Playback and the Local Relay](Playback-and-the-Local-Relay#what-each-player-can-do).
`settings` works as for providers, stored in `[players.<name>]`.

A player Cue can control implements the whole `Player` protocol in
[cue/player/base.py](https://github.com/CarsonKopec/Cue/blob/master/cue/player/base.py), as
[mpv.py](https://github.com/CarsonKopec/Cue/blob/master/cue/player/mpv.py) and
[vlc.py](https://github.com/CarsonKopec/Cue/blob/master/cue/player/vlc.py) do. The
[ffplay](https://github.com/CarsonKopec/cue-plugins/tree/main/players/ffplay) and
[default-app](https://github.com/CarsonKopec/cue-plugins/tree/main/players/default-app)
plugins are complete examples.

## A subtitle source

A subtitle source finds subtitle files for an episode and downloads them. It registers a
`SubtitleBackend` from
[cue/subtitles/registry.py](https://github.com/CarsonKopec/Cue/blob/master/cue/subtitles/registry.py)
(Cue 0.5.0 and later):

```python
from cue.providers.base import Setting
from cue.subtitles.base import SubtitleError, SubtitleTrack
from cue.subtitles.registry import SubtitleBackend, register_subtitles

class MySubtitles:
    name = "mysubs"                # the same name as its backend

    def __init__(self, api_key, http, folder):
        self._key, self._http, self._folder = api_key, http, folder / "mysubs"

    async def find(self, show, episode, languages, *, file_path=None):
        ...                        # best match first; [] when there's nothing
        return [SubtitleTrack("en", "Release name", url="https://...", id="123")]

    async def fetch(self, track):
        ...                        # download it, and return its path on disk

register_subtitles(SubtitleBackend(
    "mysubs", "My Subtitles",      # its [subtitles.mysubs] table, and its title
    create=lambda options, http, folder: MySubtitles(options["api_key"], http, folder),
    description="One line for the Subtitles tab.",
    settings=(Setting("api_key", "API key", "secret", required=True),),
    homepage="https://example.com/api-keys",  # where to get a key
))
```

- **`create`** gets the source's `[subtitles.<name>]` table, Cue's shared `httpx.AsyncClient`
  and the folder for subtitle files. Cue calls it only once every `required` setting is
  filled in; until then the Subtitles tab says what's missing. `Setting` works as for
  providers, and with `enabled = false` in the table Cue doesn't create the source at all.
- **`find`** gets the `Show`, the `Episode` and your languages: two-letter codes, in order of
  preference. `file_path` is the episode's file when it's on the PC, for a source that can
  match a file exactly. Leave each track's `source` empty (Cue fills in your `name`).
- **`fetch`** downloads one track and returns its path. Keep files in a folder of your own
  under the one Cue gives you.
- **Raise `SubtitleError`** with a message people can act on, such as "SubDL refused the API
  key". From `find` it doesn't stop the other sources; from `fetch`, Cue tries the next
  match.
- For the series' name use `show.series_title or show.title`; `show.media_type == "movie"`
  tells movies apart, and `episode.season or 1` is the season.

The [gestdown](https://github.com/CarsonKopec/cue-plugins/tree/main/subtitles/gestdown)
plugin is a short, complete example.
[subdl](https://github.com/CarsonKopec/cue-plugins/tree/main/subtitles/subdl) shows an API
key and subtitles that come in zips.

## Trying it

```bash
cue plugin install path/to/my-plugin
```

installs the folder. Change the code and install again to reload it. Errors show next to
the plugin in the Plugins tab, and in `cue.log`.

## Sharing it

To put it in the catalog, add it to
[cue-plugins](https://github.com/CarsonKopec/cue-plugins) under `providers/`, `players/` or
`subtitles/`, with tests. Every push to its `main` branch rebuilds the catalog, and Cue offers it from the
next refresh.

## Older ways

A single `*.py` file in the `providers\` folder in Cue's folder, or a Python package with a
`cue.providers` entry point, still work. Such a provider is on when its
`[providers.<name>]` table exists, unless `enabled = false`.

## Ideas

| Idea | Why it fits |
|---|---|
| IINA (macOS), Celluloid (Linux) | Built on mpv and accept its options, so they can reuse Cue's mpv control. |
| MPC-HC / MPC-BE (Windows) | Start at a position with `/start`; their optional web interface reports position and accepts seeks. |
| PotPlayer (Windows) | Takes a start position and title on its command line. |
| yt-dlp | One provider for YouTube playlists and channels, Internet Archive, Twitch VODs, Vimeo, PeerTube and the other sites yt-dlp supports. |
| Plex | Plex accepts its token as an `X-Plex-Token` URL parameter. |
| Emby | Jellyfin is a fork of Emby, so most of the Jellyfin provider carries over. |
| Internet Archive | Public-domain films and TV through a public JSON API, with plain MP4 links. |
| IPTV / M3U | Channel groups as shows and channels as episodes. Live channels have no length, so resume and auto-next don't apply. |

Services with DRM (Netflix, Crunchyroll, Disney+, Max, Prime Video) can't be supported:
neither VLC nor Cue can play their encrypted streams.
