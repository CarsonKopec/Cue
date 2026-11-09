`cue serve` runs Cue without the terminal app, on a home server or in Docker, and you use
it from a web browser: a phone, a tablet or a PC on the same network. It searches, plays
episodes in the browser or casts them to a TV, and remembers your place, the same as the
terminal app. The terminal app (`cue`) still works as before.

| On the web | |
|---|---|
| **Search** by group, with results from every provider as each one answers | |
| **Episodes** with watched marks and where you stopped, and **Continue Watching** | |
| **Watch in the browser**, with subtitles, auto-skip of anime openings and endings, and the next episode starting by itself | |
| **Cast to a TV**: DLNA smart TVs, Chromecast, AirPlay, or VLC on an Xbox, one episode after another | |

Not on the web yet: settings, providers, plugins, the subtitle picker and downloads. Set
those up in the config file or in the terminal app, then restart `cue serve` (see
[Setting it up](#setting-it-up)).

## Running it with Docker

You need a Linux machine that's always on, such as a NAS, a Raspberry Pi or a home server,
with Docker and Docker Compose. In a copy of Cue's repository:

```bash
mkdir config
docker compose up -d --build
```

Then open `http://<the machine's address>:8771` in a browser on your network. Cue's
address is also in its log: `docker compose logs cue`.

- `./config` holds everything Cue keeps: `config.toml`, the database, plugins and
  subtitles. Back it up to keep your history.
- `docker compose up -d --build` again after `git pull` updates Cue.
- The image is built for `amd64` (most PCs and servers) and `arm64` (Raspberry Pi 4 and
  5, most recent NAS boxes).

**The folder must be Cue's.** Cue runs as user and group 1000, the first user on most
Linux systems, so make `./config` yourself (as above) before the first start. If your
user has another ID (`id -u`, `id -g`; Synology users start at 1026), set it in a `.env`
file beside `docker-compose.yml`:

```bash
CUE_UID=1026
CUE_GID=100
```

**Local files.** For the local provider, mount the folder in `docker-compose.yml`
(`- /srv/media:/media:ro`) and set `path = "/media"` under `[providers.local]`.

## Running it without Docker

`cue serve` works wherever Cue does, as `cue.exe` or from source:

```bash
cue serve
```

It prints the address to open. `--port` changes the web interface's port and `--host`
the address it listens on; both can be set in [`[web]`](#settings) instead. Ctrl+C
stops it. On Windows, allow Cue through the firewall on **private networks** when asked.

## Networking

These steps are for the setup above: Docker on a Linux machine (a NAS, a Raspberry Pi
or a home server) with **host networking**, as `network_mode: host` in
`docker-compose.yml` sets up. The container shares the machine's own network instead of
getting a Docker network of its own, because:

- Cue finds TVs with SSDP (DLNA) and mDNS (Chromecast, AirPlay) broadcasts, which don't
  cross into a Docker network.
- Phones and TVs fetch videos from Cue's cast server, so it must be reachable at the
  machine's own address.

On a NAS that sets up containers in its own app, choose the host network there: in
Synology's Container Manager it's "Use the same network as Docker Host", and in
Portainer the network called `host`.

Docker Desktop on Windows or macOS isn't a substitute: it runs containers in a virtual
machine, where TVs can't be found. On a Windows PC, run `cue serve` itself (see
[Running it without Docker](#running-it-without-docker)).

### Ports

| Port | What uses it | Change it with |
|---|---|---|
| TCP 8771 | The web interface | `[web] port` |
| TCP 8770 | The cast server: phones and TVs play videos from it | `[cast] port` |
| UDP 1900 | SSDP: VLC on an Xbox finds Cue, and Cue finds DLNA TVs | |
| UDP 5353 | mDNS: Cue finds Chromecast and AirPlay TVs | |

A phone plays videos from the same address it opened the page with, on port 8770, so it
needs nothing more. TVs are given the machine's address on its main network.

### Firewall

Raspberry Pi OS and most home servers have no firewall switched on. If yours has one, let
your home network in. With ufw, for a network of `192.168.1.x`:

```bash
sudo ufw allow proto tcp from 192.168.1.0/24 to any port 8770,8771
sudo ufw allow proto udp from 192.168.1.0/24
```

The second rule is for finding TVs: they answer Cue's search from their own address,
which a firewall otherwise drops as unasked-for. A NAS has the same settings in its own
firewall page.

### More than one network

The machine may have several networks: Wi-Fi and Ethernet, Docker's own bridges
(`docker0`), or a VPN. Cue looks for TVs on all of them, and tells TVs the address of the
one it uses for the internet. When that's the wrong one (a VPN is the usual culprit),
name the network the TVs are on in `config/config.toml`:

```toml
[cast]
interface = "eth0"        # see `ip -brief address`: eth0, end0, wlan0, enp3s0…
# host = "192.168.1.20"   # or the machine's address on that network
```

With either set, the cast server listens only there. Restart Cue afterwards.

**A fixed address helps.** Give the machine a reserved address in your router's DHCP
settings, so the page you bookmarked on your phone keeps working.

## Watching on a phone

1. Search. With more than one group, the buttons under the search box choose Movies,
   Shows, Anime or your own.
2. Open a show. ✓ marks watched episodes and ▶ the one you stopped in. The big button
   resumes, or plays the next episode. SUB and DUB appear for providers that have both.
3. Tap an episode to play it here.

While it plays:

- **Subtitles** come from the streaming server, or else from your
  [subtitle sources](Subtitles) (OpenSubtitles, Addic7ed, SubDL), in your first language
  from `[subtitles] languages`. Choose another, or none, from the menu under the video.
- **Auto-skip** jumps over anime openings and endings ([Skipping](Skipping)). Turned off,
  a "Skip opening" button appears instead.
- **Auto-next** starts the next episode five seconds after one ends, with a button to
  cancel.
- Your place is saved as you watch, so you can carry on in the terminal app, on a TV or
  on another phone.

iPhones and iPads play the stream themselves, with AirPlay and subtitles in full screen.
Other browsers play it with [hls.js](https://github.com/video-dev/hls.js), which is
bundled with Cue.

**Local files** play only in formats the browser knows: MP4 with H.264 video plays
everywhere, while MKV and others play in some browsers, depending on what's inside. Cast
the rest to a TV, or watch them in the terminal app.

## Casting from the web

The button at the top right says where episodes play: **This device**, or a TV. Tap it
to look for TVs (a few seconds) and choose one. From then on, tapping an episode sends it
to that TV, then the episodes after it, one by one.

- On the player, the TV button sends what's playing to a TV; it carries on from where you
  were.
- While a TV plays, a bar at the bottom shows what's on, with **Next** and **Stop**.
- An AirPlay TV that shows a code the first time asks for it in that bar. Cue remembers
  the pairing.
- **VLC on the TV** (an Xbox, say) works as in the terminal app: open VLC → Network →
  Cue and pick the episode.

What each kind of TV can do is in [Casting](Casting).

## Setting it up

The web interface can't change settings yet. Use either:

- **The config file**, `config/config.toml` (see [Configuration](Configuration)). Restart
  Cue after changing it: `docker compose restart`.
- **The terminal app**, inside the container:

  ```bash
  docker compose exec cue cue
  ```

  Press `Esc`, then `s` for Settings or `p` for Plugins, set things up and quit with
  `q`. Then `docker compose restart`. The terminal app can't play anything inside the
  container; it's only for settings.

Plugins install the same way:

```bash
docker compose exec cue cue plugin catalog
docker compose exec cue cue plugin install <name>
docker compose restart
```

## Settings

`cue serve` uses the same config file as the terminal app, plus a `[web]` table:

| Setting | Default | What it does |
|---|---|---|
| `port` | `8771` | The web interface's port. |
| `host` | | The address to listen on; empty listens on every network adapter. |
| `hosts` | `[]` | Extra names the web interface answers to, such as `"cue.example.com"` from your own DNS. |

The cast server always runs under `cue serve`, whatever `[cast] enabled` says, because
browsers play videos from it. Its `port`, `name`, `interface` and `host` apply as in
[Casting](Casting).

## Who can use it

There's no login yet, so anyone on your network can use it, and only them:

- Cue answers only devices with a home network's addresses (`192.168.x.x`, `10.x.x.x`,
  `172.16-31.x.x`, and the IPv6 equivalents). Don't forward its ports on your router.
- It answers only to IP addresses and home network names (`nas`, `nas.local`,
  `server.lan`, `box.home.arpa`). Another name gets "Cue doesn't answer to …"; add it to
  `[web] hosts`. This stops other websites from reaching Cue through your browser.
- It offers nothing that changes settings or installs plugins.

## Troubleshooting

| Problem | What to do |
|---|---|
| The page doesn't open | Is it running (`docker compose ps`)? Use the address from `docker compose logs cue`. Check the machine's firewall allows port 8771 ([Firewall](#firewall)). |
| "Cue doesn't answer to …" | Open it by its IP address, or add the name to `[web] hosts`. |
| "Playback is unavailable" | The cast server couldn't start, usually because port 8770 is taken. Change `[cast] port`. |
| Videos don't start | The browser plays from port 8770: the firewall must allow it too. |
| No TVs found | Use host networking (as `docker-compose.yml` does), let UDP from your network through the firewall ([Firewall](#firewall)), and check the TV is switched on and on the same network. |
| A TV is found but won't play | It was given the wrong address: name its network with `[cast] interface` ([More than one network](#more-than-one-network)). |
| `Permission denied: '/config/config.toml'` | `./config` isn't owned by Cue's user: `sudo chown -R 1000:1000 config`, or set `CUE_UID` and `CUE_GID`. |
| "This browser can't play this video's format" | A local file the browser doesn't know (see [Watching on a phone](#watching-on-a-phone)). Cast it to a TV. |
