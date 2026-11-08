A provider is somewhere Cue finds shows and movies. Each one has a table in the config,
`[providers.<name>]`, which the Plugins menu's Providers tab fills in for you (`Enter`).

| Provider | Where from | In groups |
|---|---|---|
| `jellyfin` | Your Jellyfin server (built in) | Shows, Anime |
| `local` | Folders on your PC (built in) | Shows, Anime |
| `anime` | HiAnime (the `anime` plugin) | Anime |
| `hydra` | HydraHD, movies and TV (the `hydra` plugin) | Movies, Shows |

A provider with empty settings is left out, and the Home screen says what's missing.
`enabled = false` in its table turns it off and keeps its settings; `groups = [...]`
changes its [search groups](Search-Groups).

## Jellyfin

```toml
[providers.jellyfin]
url = "https://jellyfin.example.home"
api_key = "..."           # Jellyfin Dashboard -> API Keys
# user_id = "..."         # only for servers older than 10.9
```

- Cue uses Jellyfin's REST API with your API key and plays its direct-play streams.
- Each season appears as its own show (`Show`, `Show Season 2`, ...), because
  MyAnimeList numbers anime episodes per season.
- AniList, AniDB and Kitsu IDs in Jellyfin's metadata help Cue find the show's
  MyAnimeList ID for [skipping](Skipping).
- Jellyfin needs its login sent with every request, which VLC can't do. Cue's
  [local relay](Playback-and-the-Local-Relay) adds it, so VLC plays Jellyfin; mpv sends
  it itself.

## Local folders

```toml
[providers.local]
path = "D:/Media/Shows"          # or a list: ["D:/Anime", "E:/TV"]
```

Cue reads file names. It understands:

| Pattern | Example |
|---|---|
| Name and number | `Show Name - 01.mkv` |
| Release group and quality | `[Group] Show - 01 (1080p).mkv` |
| Season and episode | `Show.S01E02.mkv` |
| Episode word | `Show Episode 3.mkv` |
| A bare number in a show's folder | `Show/Season 2/01.mkv` |

Several files for the same episode become quality choices. Local files get exact
subtitle matches from OpenSubtitles (by file hash); see [Subtitles](Subtitles).

## HiAnime

Install the `anime` plugin: the Plugins menu's Plugins tab, or

```bash
cue plugin install anime
```

That adds a `[providers.anime]` table using these defaults; change them in the Providers
tab or the config:

```toml
[providers.anime]
base_api = "https://hianime.at"
language = "sub"          # the first choice: "sub" or "dub"
timeout = 20
```

**SUB and DUB.** On the episodes screen, use the SUB / DUB selector or press `b` before
playing. SUB is the original audio, DUB the dubbed version. Cue keeps the choice for the
whole queue and says when that version isn't available; it never switches by itself.
The choice lasts until Cue closes; `language` sets the next run's. History and resume
points are shared between the versions, and downloads include `[SUB]` or `[DUB]` in
their names so they don't overwrite each other.

**How streams are found.** The plugin reads the episode's server list, then each
server's public player page:

- ZokoAnime's player configuration (`window.__P`), decoded the way the site's own public
  player module does it;
- explicit media links on other supported server pages.

It doesn't run downloaded JavaScript or get around logins, access denials, DRM or
challenges. If one server fails, the next one with the same language is tried. Streams
carry their Referer and User-Agent to the player and to downloads.

**Limits.**

- The site's markup or encoding may change.
- Files the site has removed can't be played.
- If a server offers only one playlist with every quality inside, the player picks the
  quality, not Cue.
- Once an episode is playing, Cue doesn't switch servers by itself.
- `search_api`, `episodes_api`, `servers_api`, `agent` and catalog `headers` still work
  in the table. Catalog credentials are never sent to the player servers.

## HydraHD

Install the `hydra` plugin the same way. Its defaults:

```toml
[providers.hydra]
base_url = "https://hydrahd.com"   # the site redirects to its current address
timeout = 20
```

- A search finds movies and series. A series is listed once per season
  (`Breaking Bad — Season 1`, `— Season 2`, ...), so history and resume points stay
  separate per season. A movie opens as a single item.
- Cue reads the episode page's server menu and plays the first one it supports. Today
  that's **VixSrc**, read through its public page and API to an HLS playlist. Its
  Referer and User-Agent go to the player. Up to six supported servers are tried before
  Cue reports an error. Another server shows a browser challenge, which Cue doesn't try
  to pass.
- The playlist lists the stream's subtitles (often English, CC and others); Cue loads
  your language from it. See [Subtitles](Subtitles).
- The audio is the language the playlist marks as default (English on the titles
  tried). Change it in VLC's Audio menu.
- AniSkip is turned off for HydraHD, so movies and TV are never skipped on anime timings.
  `s` still jumps 85 seconds.
- HydraHD streams are encrypted (AES-128); when Cue downloads one, it decrypts the pieces
  itself (see [Playback and the Local Relay](Playback-and-the-Local-Relay)).

**Limits:** HydraHD's pages, addresses or servers may change. There's no SUB/DUB choice,
because the site doesn't offer one.

## More providers

Anyone can add one; see [Writing a Plugin](Writing-a-Plugin), which also lists good
candidates (Plex, Emby, yt-dlp, Internet Archive, IPTV playlists). Services with DRM
(Netflix, Crunchyroll, Disney+, Max, Prime Video) can't be supported: neither VLC nor Cue
can play their encrypted streams.
