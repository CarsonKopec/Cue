Streams are awkward for video players. A source may need HTTP headers (a Referer, or
Jellyfin's login), and many come as HLS playlists with the audio in separate tracks.
Most players can't do all of that, and some can only open files. Cue makes any stream
play in any player by stepping in only where that player needs help.

The code is in [cue/player/relay.py](https://github.com/CarsonKopec/Cue/blob/master/cue/player/relay.py).

## What each player can do

Every player declares three things:

| Declared | Meaning |
|---|---|
| `headers` | The HTTP headers it can send with a stream: any (mpv), only Referer and User-Agent (VLC), or none (Another program) |
| `hls` | Whether it plays HLS playlists (`.m3u8`) |
| `links` | Whether it opens links at all, or only files (the Default app) |

For each episode, Cue compares that with what the stream needs and picks the lightest way
to play it:

| Route | When | What the player gets |
|---|---|---|
| **Direct** | The player can send every header the stream needs | The source's own link |
| **Relayed HLS** | It plays HLS, but can't send the headers | A link to the local relay |
| **Continuous** | It can't play HLS | One MPEG-TS stream, joined by FFmpeg as it plays |
| **File** | It only opens files | The episode, downloaded to a temporary MP4 first |

Files already on your PC always go straight to the player.

## The local relay

The relay is a small HTTP server on `127.0.0.1`, on a random port, started the first time
a player needs it. Only programs on this PC can reach it.

**Links.** Each upstream address gets a random 32-character token, and the player is given
`http://127.0.0.1:<port>/h/<token>.<ext>`. The relay remembers which stream each token
belongs to and what that stream needs.

**Playlists.** When the player asks for a playlist, the relay fetches it from the source,
replaces every address in it (variants, audio tracks, pieces, keys, init sections) with
relay links, and returns it. So every request the player makes for that stream comes back
through the relay.

**Headers, and where they go.** Every request the relay passes on carries the stream's
headers. Credentials go only to the stream's own site, though. A piece on a CDN gets the
Referer and User-Agent, but never an `Authorization` header or cookie meant for Jellyfin.

**Extensions.** FFmpeg refuses HLS pieces whose names don't end in a media extension.
HiAnime's host disguises its pieces as images (`seg_00000.ts.jpg`), so relay links carry a
media extension (`.ts` when the original has none). The upstream address is untouched.

**Continuous streams.** At `/ts/<id>`, the relay runs FFmpeg on the stream's relayed
playlist and pipes one MPEG-TS stream to the player, copying video and audio without
re-encoding. FFmpeg gets a cut-down playlist: the variant your `quality` setting allows,
and the audio track the source marks as default (so it doesn't fetch every language). A
plain video file is passed through as it is. Without FFmpeg, the player is sent the
playlist instead.

**Connections.** The relay speaks HTTP/1.1 and keeps connections open between requests,
because players fetch hundreds of pieces in a row. It sends each reply at once
(`TCP_NODELAY`) rather than waiting on the player's acknowledgement. Brief upstream
failures are retried three times; a `401`, `403`, `404` or `410` isn't, because asking
again won't help.

## Downloads for players that only open files

Windows' Media Player can't open a stream's link: Windows would hand it to a web browser.
So for the Default app, Cue downloads the episode as an MP4 first, showing progress on Now
Playing, then opens it.

Such a player can't take subtitles once it's playing, so Cue looks for them while the
episode downloads. Before opening the file, it saves them beside it under the same name:
`<episode>.srt` for your first language, `<episode>.<language>.srt` for the others. WebVTT
is converted to SRT by Cue, and ASS by FFmpeg. If the search is still running when the
download finishes, the episode waits up to 20 seconds for it, then opens without them.
The subtitle files are deleted along with the episode.

The download is built to be quick and to leave little behind on a small disk:

### Only the finished file is written

1. Cue reads the playlists itself: the cut-down top playlist, then each media playlist.
2. It lines up every piece in the order FFmpeg will ask for them. Video and audio are
   interleaved by time, because FFmpeg reads them side by side.
3. **12 download threads** fetch pieces into memory, at most **24 pieces** ahead of FFmpeg.
4. FFmpeg reads the stream through the relay. Each piece it asks for is handed over from
   memory and forgotten.
5. FFmpeg writes the finished MP4, first as `<name>.part.mp4`, renamed once complete.

No piece is ever written to disk, so an episode needs about its own size free, not
twice that. On two real episodes, the most disk used during the download was exactly the
finished file's size.

If FFmpeg asks for a piece the threads haven't reached, it's fetched right away on
FFmpeg's request.

### Room is checked first

Before starting, Cue estimates the episode's size from the playlist's bandwidth
(`AVERAGE-BANDWIDTH`, else `BANDWIDTH`) and its length. If the folder has less than that
plus 5% free, it stops and suggests a lower quality, another folder, or freeing space.

### Encrypted streams are decrypted by Cue

HydraHD's pieces are encrypted with AES-128. FFmpeg can decrypt them, but it opens a new
connection for every encrypted piece and decrypts each in turn. That made FFmpeg, not the
network, the slow side: an hour-long episode used over 400 connections every 15 seconds.

So Cue takes the decryption over:

1. It reads each media playlist's `#EXT-X-KEY` lines and fetches each key once.
2. The download threads decrypt every piece as it arrives (AES-128-CBC). The IV is the one
   the playlist gives, or else the piece's sequence number, as the HLS spec says.
3. The playlists FFmpeg gets have their keys removed, so it reads plain pieces over a few
   kept-open connections.

Cue leaves to FFmpeg what it can't do completely itself: SAMPLE-AES or DRM, byte-range
pieces, and init sections under a key. Those playlists stay as they were.

### How fast

Measured on this design:

| Episode | Size | Time |
|---|---|---|
| HiAnime, 24 minutes at 1080p | 258 MB | about 5 s |
| HydraHD, 58 minutes at 720p | 656 MB | about 50 s (it was 83 s before Cue decrypted the pieces) |

More than 12 threads didn't help: HydraHD's server gave about 30 MB/s at 12 or 16 at once,
and throttled at 24.

### Cleaning up

| Setting | What it does |
|---|---|
| `keep_hours = 0` | Each episode is deleted when you stop watching or move on. More keeps them for replays. |
| `max_mb = 4096` | The folder's limit; the oldest episodes go first. `0`: no limit. |

Cue tidies the folder before each download and when it closes. Unfinished `.part.mp4`
files always go. On Windows, a file a player still has open can't be deleted, so it stays
until the next tidy.

`[playback] quality` downloads a smaller version for these players only, and `folder`
puts them on another drive. See [Configuration](Configuration#playback).

### Without FFmpeg

If FFmpeg isn't available, Cue downloads with [yt-dlp](https://github.com/yt-dlp/yt-dlp)
instead, when that's installed. On Windows, `cue install --ffmpeg` gets FFmpeg
([Installing](Installing#ffmpeg)).
