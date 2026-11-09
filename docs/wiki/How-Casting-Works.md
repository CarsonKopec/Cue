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

## TVs Cue tells what to play: DLNA, Chromecast and AirPlay

These TVs are told what to play. The code is in
[cue/cast/devices.py](https://github.com/CarsonKopec/Cue/blob/master/cue/cast/devices.py)
and, for DLNA,
[cue/cast/renderers.py](https://github.com/CarsonKopec/Cue/blob/master/cue/cast/renderers.py).

### Finding TVs

When you press `t`, Cue spends about 4 seconds looking for TVs, all three ways at once:

- **DLNA**: an SSDP search (`M-SEARCH` for `MediaRenderer:1`) sent from every network
  adapter, since the TV may be on any of them. Each answer points to the TV's description,
  which gives its name and its **AVTransport** control address. **ConnectionManager**'s
  `GetProtocolInfo` lists what it plays, so audio-only devices (speakers) are left out.
  Answers come from the local network, so they're checked: only plain http addresses on the
  device that answered, descriptions of limited size, and no XML DTDs.
- **AirPlay**, with [pyatv](https://github.com/postlund/pyatv), over mDNS. Only devices
  whose `features` flags include bit 0 (video, as Apple TVs say) are listed. Samsung and LG
  TVs set bit 4 (HLS video) instead, but answer pyatv's `POST /play` with 404 Not Found, so
  they're cast to over DLNA.
- **Chromecast**, with [PyChromecast](https://github.com/home-assistant-libs/pychromecast),
  over mDNS. Speakers and speaker groups are left out.

### Pairing with AirPlay TVs

Many AirPlay devices want a code the first time. Cue starts pyatv's pairing, the TV shows a
code, you type it into Cue, and pyatv saves the credentials in `airplay.json` in Cue's
folder. Later connections use them.

Pairing proves the code with SRP, through the srptools library. srptools 2.0 made an
argument that pyatv 0.18 passes keyword-only, which broke every code ("SRPClientSession
takes 2 positional arguments"), so Cue requires srptools below 2. Cue's self-test
(`cue --smoke-test`) runs the whole exchange, with srptools standing in for the TV, so a
build with a broken pairing fails before it's released.

### Playing a queue

For each episode, Cue:

1. Opens the episode's address on the cast server itself. That resolves the stream, and
   tells Cue its type: an HLS playlist, or a video file. Chromecast needs the type up
   front.
2. Sends the TV the address, the title and where to start: the resume point.
   - **DLNA**: SOAP commands to the TV's AVTransport: `Stop`, `SetAVTransportURI` with the
     address and DIDL-Lite metadata, then `Play`. Cue then asks `GetTransportInfo` every two
     seconds. The episode has ended when the TV goes back to `STOPPED`, and stopping
     casting in Cue sends `Stop`.
   - **AirPlay** (pyatv's `play_url`) keeps the request open while the video plays and
     returns when it stops.
   - **Chromecast** opens its Default Media Receiver. Cue listens to its media status,
     following only reports about this video, until it goes idle after playing. A
     receiver error ends the queue with a message.
3. If the cast server saw the episode watched (past 90%), plays the next one. Otherwise it
   was stopped on the TV, and the queue ends.

### DLNA TVs that can't seek

While a Samsung TV plays HLS, `GetCurrentTransportActions` offers only `Pause` and `Stop`:
`Seek` is refused ("Transition not available", error 701), and so is HLS's
`#EXT-X-START` tag. So for a resumed episode, Cue gives the TV `/play/<cast>/<episode>?start=<seconds>`,
and the cast server trims the episode's media playlists to begin at the segment holding
that time:

- earlier segments are left out, and `#EXT-X-MEDIA-SEQUENCE` and
  `#EXT-X-DISCONTINUITY-SEQUENCE` move on past them;
- the latest `#EXT-X-KEY` and `#EXT-X-MAP` before the cut are kept, since the first
  remaining segment needs them;
- every remaining segment keeps its time in the episode, so resume points and "watched"
  stay right.

A video file (not HLS) is resumed with `Seek` instead, retried for a few seconds while the
TV starts.

### Subtitles on DLNA TVs

DLNA TVs don't load the subtitle renditions in Cue's HLS playlist, so the preferred
subtitle goes along as SRT (`/s/<cast>/<episode>/<n>.srt`, converted from Cue's WebVTT):

- in the `SetAVTransportURI` metadata, as Samsung's `sec:CaptionInfoEx` and `sec:CaptionInfo`,
  and as `pv:subtitleFileUri` on the video, which LG and others read;
- in the answer to the TV's own request: when it fetches the episode with
  `getcaptionInfo.sec: 1`, the cast server answers with a `CaptionInfo.sec` header.

For a trimmed episode, the SRT gets the same `?start=` and its times move back to match,
since the TV's clock starts at 0 where the stream does. Cues that are already over are
left out.

### Chromecast and CORS

Chromecast's receiver is a web page running on the TV, so it may only read media from
servers that allow it. The cast server sends CORS headers
(`Access-Control-Allow-Origin: *`) on its media addresses (`/play`, `/r`, `/s`), and answers
the receiver's pre-flight requests. Its own pages and the DLNA addresses don't get those
headers.

## Why Cue has to stay open

The TV fetches every playlist and piece from Cue, which fetches it from the source. When
Cue closes, the cast server stops, and the TV runs out of video.

## Security

The cast server is reachable from your network, without a password; anyone there can play
what you've cast. `interface` or `host` keeps it off other networks, such as a VPN. Cast
only on a home network you trust.
