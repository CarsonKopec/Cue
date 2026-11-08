<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/CarsonKopec/Cue/master/assets/cue-wordmark-dark.svg">
    <img src="https://raw.githubusercontent.com/CarsonKopec/Cue/master/assets/cue-wordmark-light.svg" alt="Cue" width="320">
  </picture>
</p>

**Cue** is a keyboard-driven terminal app for finding and watching shows and movies. You
search, pick an episode and it plays; when it ends, the next one starts. Anime openings
and endings are skipped for you, your place is remembered, and what you pick can play
on a TV.

Cue runs on Windows, Linux and macOS as a single program. It's licensed under the GPL-3.0.

## Where to start

| I want to… | Read |
|---|---|
| Install Cue | [Installing](Installing) |
| Set up where shows come from | [Providers](Providers) and [Plugins](Plugins) |
| Learn the keys | [Keys](Keys) |
| Change a setting | [Configuration](Configuration) |
| Watch on a TV | [Casting](Casting) |
| Use a different video player | [Players](Players) |
| Fix something that isn't working | [Troubleshooting](Troubleshooting) |

## What Cue does

- **One full-screen app**: search, pick, play and move on to the next episode without
  leaving it.
- **Any source**: shows come from *providers*. Jellyfin and folders on your PC are
  built in; HiAnime, HydraHD and others are [plugins](Plugins) you install from Cue's
  Plugins menu.
- **Searches by group**: Movies, Shows, Anime or groups of your own. One search asks
  every provider in the group at once. See [Search Groups](Search-Groups).
- **Skips anime openings and endings** using the AniSkip community database. See
  [Skipping](Skipping).
- **Resumes anywhere**: history and playback positions are kept on your PC.
- **Subtitles** from the streaming server, with OpenSubtitles, Addic7ed and SubDL as
  fallbacks, all searched at once. See [Subtitles](Subtitles).
- **Plays in VLC** by default, or mpv, or any other video player, including Windows'
  Media Player. Cue does whatever a player can't: it adds headers, joins streams, or
  downloads the episode first. See [Players](Players).
- **Casts to a TV**: VLC on an Xbox, AirPlay TVs and Chromecast. See [Casting](Casting).
- **Keeps itself up to date**, plugins included. See [Updates](Updates).

## How it works

These pages explain what happens under the hood, for the curious and for anyone
changing Cue:

- [How Cue Works](How-Cue-Works): the big picture, from a search to the player.
- [Playback and the Local Relay](Playback-and-the-Local-Relay): how Cue gets any stream
  into any player, and how its downloads stay fast and small.
- [How Casting Works](How-Casting-Works): the DLNA media server, AirPlay and Chromecast.
- [How Plugins and Updates Work](How-Plugins-and-Updates-Work): installing, checking and
  swapping code safely.

## For developers

- [Writing a Plugin](Writing-a-Plugin): add a provider or a player.
- [Building and Releasing](Building-and-Releasing): make the programs and publish a release.
- [Development](Development): the code layout, tests and design notes.

---

These pages live in the repository under
[`docs/wiki/`](https://github.com/CarsonKopec/Cue/tree/master/docs/wiki) and are copied to
this wiki on every push, so they change together with the code. Edit them there.
