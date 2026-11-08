Cue plays episodes in a separate video player. Pick one in the Plugins menu's Players tab
(`d`), or set `player` in the config.

| Player | From | What Cue can do with it |
|---|---|---|
| VLC | built in (default) | Everything: resume, auto-skip, auto-next, subtitle choice. |
| mpv | built in | Everything, and any header a source needs. |
| Another program | built in | Any player: MPC-HC, PotPlayer, KMPlayer, SMPlayer, ... Cue starts it but can't follow it. |
| ffplay | `ffplay` plugin | Starts at your resume point and sends any header. Cue can't follow it while it plays. Needs FFmpeg. |
| Default app | `default-app` plugin | Your system's default video app: Windows' Media Player (or whatever Default apps says), macOS `open`, Linux `xdg-open`. |

**Follow** means Cue reads the player's position while you watch, which auto-skip, resume
points and auto-next need. With a player Cue can't follow, the Now Playing screen says
so; `n` still moves to the next episode, and closing the player returns to Cue.

## Choosing a program

Cue finds each player by itself: in its usual install folder, next to Cue, or on `PATH`.
To use a particular copy, set its path in the Players tab (`Enter`) or the config:

```toml
[players.vlc]
path = "D:/Portable/VLC/vlc.exe"
```

`CUE_VLC` and `CUE_MPV` do the same from the environment.

## VLC

VLC starts fullscreen; `player_args = ["--no-fullscreen"]` gives a window. Cue controls
it through VLC's local web interface on `127.0.0.1`, with a fresh random password for each
episode, and never changes VLC's own preferences. See
[How Cue Works](How-Cue-Works#players) for details.

VLC can send a Referer and User-Agent, which HiAnime and HydraHD need. Sources needing
other headers, such as Jellyfin's login, play through Cue's local relay instead.

**Limits:**

- A subtitle file in a format VLC can't read, or behind a redirect to another site, can't
  be loaded by itself; pick another in the subtitle picker.
- Auto-next needs VLC to report the episode's length and stop near its end. A stream
  with an unknown length doesn't move on by itself; press `n`.

## mpv

Install [mpv](https://mpv.io/installation/) and set `player = "mpv"`. Cue finds `mpv.exe`
next to Cue, on `PATH`, or through `CUE_MPV`, and controls it over mpv's JSON IPC.

## Another program

Runs any player with arguments you choose:

```toml
player = "other"

[players.other]
path = "C:/Program Files/MPC-HC/mpc-hc64.exe"
arguments = '"{url}" /start {start_ms} /fullscreen'
hls = true          # false if it can't play HLS (.m3u8) links
```

| Placeholder | Filled in with |
|---|---|
| `{url}` | The link to play |
| `{title}` | The episode's title |
| `{start}` | The resume point, in seconds |
| `{start_ms}` | The resume point, in milliseconds |

## Default app

Opens episodes in the app your system uses for videos. Those apps can't open a stream's
link (Windows would hand it to a web browser), so Cue downloads a streamed episode first,
with progress on the Now Playing screen, then opens the file. Files already on your PC
open straight away. Press `q` in Cue when you're done.

This needs FFmpeg (or yt-dlp); on Windows `cue install --ffmpeg` gets it. The downloads
are quick and leave little behind; see the [`[playback]` settings](Configuration#playback).

## Every player can play every stream

Streams often need headers (a Referer, or a login) and come as HLS playlists with audio
in separate tracks. Most players can't handle all of that, so Cue steps in only where
a player needs it:

| The player | What Cue gives it |
|---|---|
| Sends the headers and plays HLS (mpv, ffplay) | The source's own link |
| Plays HLS but can't send the headers (VLC for Jellyfin, Another program) | A link to Cue's local relay, which adds them |
| Can't play HLS (Another program with `hls = false`) | One continuous stream, joined by FFmpeg |
| Only opens files (Default app) | The episode, downloaded first |

[Playback and the Local Relay](Playback-and-the-Local-Relay) explains how each works.

## Adding a player

A plugin can add any player; see [Writing a Plugin](Writing-a-Plugin#a-player).
