What you pick in Cue can play on a TV:

| TV | How it plays |
|---|---|
| **VLC** on an Xbox, a phone, a tablet, or any DLNA player | It browses Cue's media server and you pick an episode on the TV |
| **AirPlay** TVs: Apple TV, and TVs with AirPlay built in (recent Samsung, LG, Sony) | Cue tells the TV what to play |
| **Chromecast**: Chromecast, Google TV, TVs with Chromecast built in | Cue tells the TV what to play |

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

Cue lists where it can cast: **VLC on the TV** first, then the AirPlay and Chromecast TVs it
finds within a few seconds. `r` looks again, and the TV you cast to last is highlighted.

### VLC (Xbox and DLNA)

Choose VLC on the TV, then on the TV open **VLC → Network → Cue →** the show → an episode.

- **Controls** are on the TV: pause, seek and choose subtitles with the controller.
- **Subtitles** appear in VLC's subtitle menu, named by language. VLC turns one on by
  itself only when its preferred subtitle language matches; otherwise pick it.
- **Resume:** VLC starts every episode from the beginning, so the episode list on the TV
  shows `(resume 12:34)`; seek there with the controller.
- The five newest casts stay listed.

### AirPlay and Chromecast

Choose the TV. It starts the episode where you left off, and when an episode ends, the
next one starts. Stopping on the TV ends the queue. Pause and seek with the TV's remote.

An AirPlay TV that asks for a code (most TVs other than an Apple TV) shows one the first
time; type it into Cue. Cue remembers the pairing in `airplay.json`, so you're asked once.

- Only devices that play video are listed, not speakers or speaker groups.
- TVs must be on, not in standby, to be found.
- AirPlay stops when Cue closes.

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
- **No AirPlay or Chromecast TVs listed?** Turn the TV on, make sure it's on the same
  network as the PC, and press `r`.
- See also [Troubleshooting](Troubleshooting).

## Privacy

Anyone on your network can play what you cast, so cast only on a trusted home network.
