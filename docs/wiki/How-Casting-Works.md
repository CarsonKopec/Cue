How Cue gets an episode onto a TV. To set casting up, see [Casting](Casting).

The code is in [cue/cast/](https://github.com/CarsonKopec/Cue/tree/master/cue/cast).

## The cast server

With `[cast] enabled = true`, Cue runs an HTTP server on your network (port 8770) beside
the app. Every TV, whatever kind, plays through it, because sources need headers and
subtitles that TVs can't add themselves.

| Address | What it serves |
|---|---|
| `/` | A page listing what's cast, to check a TV can reach Cue |
| `/now` | A redirect to the newest cast, where it left off |
| `/play/<cast>/<item>` | An episode: resolved when the TV opens it |
| `/r/<token>` | Relayed playlists and pieces |
| `/s/<cast>/<item>/<n>.vtt` | Subtitles, as WebVTT |
| `/dlna/...` | The DLNA media server's description and services |

**Which address the TV is told.** By default, the PC's address on its route to the
internet. Cue asks the system which local address it would use, without sending anything.
With `[cast] interface`, Cue looks the named adapter up instead; with `host`, it uses that
address. With either set, Cue also listens only there.

### Casting a queue

Pressing `t` **publishes** a queue: the show, its episodes, and the resume point from your
history. Nothing is fetched yet. The five newest queues stay available.

When a TV opens an item, the server asks the provider for its stream on Cue's event loop,
waiting up to 90 seconds. It also gathers its subtitles. The answer is kept for 20
minutes, because many sources hand out addresses that expire.

### Rewriting the stream

As with the [local relay](Playback-and-the-Local-Relay#the-local-relay), the server
rewrites every playlist so that each request comes back through Cue. Those requests carry
the source's headers, and credentials only ever go to the source's own site.

Subtitles are handled differently from the local relay:

- Cue loads the episode's subtitles in your languages, from the server or a subtitle
  source.
- It puts them into the playlist as WebVTT tracks named by language, replacing the
  source's own.
- A stream with a single media playlist (no variants) is wrapped in a small master
  playlist, so it can carry them too.

### Following the TV

The server can't ask the TV where it is. It watches which pieces of the video the TV
requests instead:

- Every piece's start time is known from the playlist.
- TVs fetch ahead of what's on screen, so the position is taken as about 20 seconds before
  the piece requested.
- That position is saved every 5 seconds as the episode's resume point.
- Once the TV asks for pieces past 90% of the episode, it's marked watched.

## VLC on the Xbox: DLNA

VLC on the Xbox can't be controlled by another program. So Cue is a **DLNA media server**,
and VLC browses it like a shared folder:

1. Cue announces itself on the network with **SSDP** (multicast UDP on port 1900), as a
   UPnP media server, and answers searches for one.
2. VLC fetches Cue's description and browses its **ContentDirectory**: Cue → shows →
   episodes.
3. Each episode is listed as a video item (`video/mp2t`), which is the only kind VLC's
   DLNA client plays. VLC then sees from the response that it's HLS.

VLC on a phone or tablet, or any other DLNA player, works the same way.

## AirPlay and Chromecast

These TVs are told what to play. The code is in
[cue/cast/devices.py](https://github.com/CarsonKopec/Cue/blob/master/cue/cast/devices.py).

### Finding TVs

When you press `t`, Cue spends about 4 seconds listening for TVs announcing themselves on
the network (mDNS):

- **AirPlay**, with [pyatv](https://github.com/postlund/pyatv). Speakers such as a HomePod
  are left out: an AirPlay device's `features` flags say whether it plays video.
- **Chromecast**, with [PyChromecast](https://github.com/home-assistant-libs/pychromecast).
  Speakers and speaker groups are left out.

### Pairing with AirPlay TVs

Many AirPlay TVs (most that aren't an Apple TV) want a code the first time. Cue starts
pyatv's pairing, the TV shows a code, you type it into Cue, and pyatv saves the credentials
in `airplay.json` in Cue's folder. Later connections use them.

### Playing a queue

For each episode, Cue:

1. Opens the episode's address on the cast server itself. That resolves the stream, and
   tells Cue its type: an HLS playlist, or a video file. Chromecast needs the type up
   front.
2. Sends the TV the address, the title and where to start: the resume point.
   - **AirPlay** (pyatv's `play_url`) keeps the request open while the video plays and
     returns when it stops.
   - **Chromecast** opens its Default Media Receiver. Cue listens to its media status,
     following only reports about this video, until it goes idle after playing. A
     receiver error ends the queue with a message.
3. If the cast server saw the episode watched (past 90%), plays the next one. Otherwise it
   was stopped on the TV, and the queue ends.

Chromecast's receiver is a web page running on the TV, so it may only read media from
servers that allow it. The cast server sends CORS headers
(`Access-Control-Allow-Origin: *`) on its media addresses (`/play`, `/r`, `/s`), and answers
the receiver's pre-flight requests. Its own pages and the DLNA addresses don't get those
headers.

## Why Cue has to stay open

The TV fetches every playlist and piece from Cue, which fetches it from the source. When
Cue closes, the cast server stops and AirPlay playback ends.

## Security

The cast server is reachable from your network, without a password; anyone there can play
what you've cast. `interface` or `host` keeps it off other networks, such as a VPN. Cast
only on a home network you trust.
