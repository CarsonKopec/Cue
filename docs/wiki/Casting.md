What you pick in Cue can play on a TV:

| TV | How it plays |
|---|---|
| **Smart TVs** over DLNA: Samsung, LG, Sony and most others | Cue tells the TV's built-in player what to play |
| **VLC** on an Xbox, a phone, a tablet, or any DLNA player | It browses Cue's media server and you pick an episode on the TV |
| **Chromecast**: Chromecast, Google TV, TVs with Chromecast built in | Cue tells the TV what to play |
| **AirPlay**: Apple TV | Cue tells the TV what to play (see [AirPlay](#airplay)) |

Either way the video passes through Cue, which adds the headers and subtitles the source
needs, so **keep Cue running while you watch**.
[How Casting Works](How-Casting-Works) explains the details.

## Turn it on

```toml
[cast]
enabled = true
port = 8770               # the TV connects to this port on your PC
name = "Cue"              # how Cue appears in VLC's Network tab
# interface = "Ethernet"  # the network adapter the TV is on, by name
# host = "192.168.1.20"   # or this PC's address on that network
```

Restart Cue after changing it.

**Windows Firewall.** The first time, Windows asks whether to let Cue (`cue.exe`, or
`python.exe` from source) use networks. Allow **private networks**, and make sure your
home network is set to Private in Windows, or the TV can't reach Cue.

**More than one network adapter?** By default Cue listens on every adapter and tells the
TV the address the PC uses for the internet. With Wi-Fi as well as Ethernet, a VPN, or
virtual adapters (Hyper-V, WSL, VirtualBox), name the one the TV shares:

- `interface` names it as Windows' network settings do (`Ethernet`, `Wi-Fi`), and follows
  its address if the router changes it. If no connected adapter has that name, Cue says
  so and lists the ones it found.
- `host` names a fixed address instead.

With either set, Cue listens only there, so other networks (a VPN, say) can't reach it.

## Casting

In Cue, open a show and press `t`. That casts every episode; type a range first (such as
`3-5`) to cast only those. Already watching on the PC? Press `t` on Now Playing: Cue stops
the PC's player, saves your place, and casts that episode and the ones after it.

Cue lists where it can cast: **VLC on the TV** first, then the TVs it finds within a few
seconds, each marked DLNA, AirPlay or Chromecast. `r` looks again, and the TV you cast to
last is highlighted.

### Smart TVs (DLNA)

Choose the TV, such as **Samsung 7 Series (50) · DLNA**. It plays the episode, and when an
episode ends, the next one starts. Stopping on the TV ends the queue. Pause with the TV's
remote. There's nothing to pair or install: this is the player built into the TV, the
one phone apps cast to.

- **Resume:** the TV starts where you left off. Some TVs (Samsung's among them) can't jump
  within a stream, so Cue's cast server starts the stream there instead. Seeking with the
  remote may not work on those TVs for the same reason.
- **Subtitles:** your first language is sent along as an SRT file, the way Samsung, LG and
  most other TVs take subtitles, and shifted to match a resumed episode. If they don't
  show, turn on subtitles in the TV's own settings while it plays.
- Speakers (audio-only DLNA devices) aren't listed.

### VLC (Xbox and DLNA)

Choose VLC on the TV, then on the TV open **VLC → Network → Cue →** the show → an episode.

- **Controls** are on the TV: pause, seek and choose subtitles with the controller.
- **Subtitles** appear in VLC's subtitle menu, named by language. VLC turns one on by
  itself only when its preferred subtitle language matches; otherwise pick it.
- **Resume:** VLC starts every episode from the beginning, so the episode list on the TV
  shows `(resume 12:34)`; seek there with the controller.
- The five newest casts stay listed.

### Chromecast

Choose the TV. It starts the episode where you left off, and when an episode ends, the
next one starts. Stopping on the TV ends the queue. Pause and seek with the TV's remote.
Speakers and speaker groups aren't listed.

### AirPlay

AirPlay works the same way as Chromecast, with one extra step: an AirPlay device that asks
for a code shows one the first time, and you type it into Cue. Cue remembers the pairing
in `airplay.json`, so you're asked once. AirPlay stops when Cue closes.

Cue sends AirPlay video with [pyatv](https://github.com/postlund/pyatv), and pyatv 0.18
(the newest) can't play video on TVs that have AirPlay built in, such as Samsung's and LG's:
they refuse its request. So those TVs aren't listed under AirPlay. Cast to them over DLNA
instead, which works on the same TVs. Only devices that say they play video the Apple TV
way are listed. pyatv's own reports say 0.18 also fails on Apple TVs with tvOS 26 or later.
Cue hasn't been tested on an Apple TV.

TVs must be on, not in standby, to be found.

## What Cue keeps track of

Cue follows which part of the video the TV fetches. It saves an approximate resume point
(within about 20 seconds) and marks an episode watched near its end, just as when you
watch on the PC. Auto-skip doesn't apply on a TV.

## When it doesn't work

- **No Network tab in VLC?** Open `http://<this PC's address>:8770/now` with VLC's
  open-network-stream option. It plays the newest cast; Cue shows the exact link when
  you press `t`. It works only while Cue is open and after you've pressed `t`.
- **The TV can't connect?** Open `http://<this PC's address>:8770/` in the TV's browser
  (Edge on the Xbox). If Cue's page loads, the network path works. `cue.log` lists each
  request the TV made (lines starting `cast`).
- **No TV listed?** Turn the TV on, make sure it's on the same network as the PC, and press
  `r`. A Samsung or LG TV shows up as DLNA, not AirPlay ([why](#airplay)).
- **A DLNA TV says it can't play it?** Make sure Windows lets Cue use private networks (the
  TV fetches the episode from Cue), then cast again. `cue.log` shows whether the TV asked
  for the episode (`cast <TV's address> - "GET /play/…"`).
- See also [Troubleshooting](Troubleshooting).

## Privacy

Anyone on your network can play what you cast, so cast only on a trusted home network.
