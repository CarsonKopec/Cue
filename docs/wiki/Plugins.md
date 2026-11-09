Plugins add providers (places to watch from), players and subtitle sources to Cue.
HiAnime, HydraHD, ffplay, the Default app, Gestdown and SubDL are plugins, kept in the
[cue-plugins](https://github.com/CarsonKopec/cue-plugins) repository.

## The Plugins menu

On the Home screen press `Esc`, then `p`. It's also in Settings (`s`, or "Settings" in the
command palette, `Ctrl+P`) → **Plugins, providers and players**.
The menu has five tabs; `←` and `→` switch between them.

| Tab | What it shows | Keys |
|---|---|---|
| **Providers** | Every provider Cue knows: built in, from a plugin, or named in your config but not installed, with its groups. The status says ready, off, or what setup is missing. | `Enter` settings · `Space` on/off |
| **Groups** | Every [search group](Search-Groups) and its providers. | `Enter` choose providers · `d` start here · `n` new · `x` remove |
| **Players** | VLC, mpv, Another program and players from plugins, and whether each program was found. | `Enter` settings · `d` play with this player |
| **Subtitles** | Every [subtitle source](Subtitles#setting-up-a-source): OpenSubtitles and those from plugins. The status says ready, off, or what setup is missing. | `Enter` settings · `Space` on/off |
| **Plugins** | Installed plugins, then catalog plugins you don't have yet. | `Enter` install or update · `Space` on/off · `a` add from a file or link · `x` remove · `r` refresh |

The settings form comes from each provider, player or subtitle source itself: Jellyfin
asks for its address and API key, Local for its folders, SubDL for its key. Saving writes
its table in `config.toml` (your comments stay) and takes effect at once, so there's no
need to restart Cue.

## Installing a plugin

Cue installs a plugin from:

- **the catalog**: the list of plugins, with checksums, at the address in
  `[plugins] catalog` (by default the `catalog` release of cue-plugins). A download whose
  checksum doesn't match is refused.
- **a link** to a plugin `.zip` (https only),
- **a `.zip` file or a folder** on your PC.

Before installing, Cue shows where the plugin comes from and asks. A plugin runs with
your account's access, like any program, so only install plugins you trust. Installing
turns its providers on, and its subtitle sources are searched as soon as they're set up
(Gestdown at once, SubDL once it has a key). Removing it turns them off and keeps their
settings.

## From a terminal

| Command | What it does |
|---|---|
| `cue plugin catalog` | The catalog's plugins |
| `cue plugin list` | What's installed |
| `cue plugin install anime` | Install from the catalog, a link, a `.zip` or a folder |
| `cue plugin update` | Update everything the catalog has newer (or name some) |
| `cue plugin remove anime` | Remove one |
| `cue plugin disable anime` / `enable` | Turn one off or on |

Add `--yes` to skip the question.

## Settings

```toml
[plugins]
catalog = "https://github.com/CarsonKopec/cue-plugins/releases/download/catalog/index.json"
disabled = []             # installed but turned off
```

`catalog` can also be a local `index.json`; empty turns the catalog off.

Plugins update by themselves once a day with Cue ([Updates](Updates)).
[How Plugins and Updates Work](How-Plugins-and-Updates-Work) explains what happens when
one is installed, and [Writing a Plugin](Writing-a-Plugin) shows how to make one.

## Plugins in the catalog

| Plugin | Adds |
|---|---|
| `anime` | The HiAnime provider ([setup](Providers#hianime)) |
| `hydra` | The HydraHD provider ([setup](Providers#hydrahd)) |
| `ffplay` | The ffplay player |
| `default-app` | Your system's default video app as a player (Windows' Media Player, say) |
| `gestdown` | Subtitles for TV shows from Addic7ed, with no key ([setup](Subtitles#addic7ed-gestdown)) |
| `subdl` | Subtitles for movies and TV from SubDL ([setup](Subtitles#subdl)) |

`gestdown` and `subdl` need Cue 0.5.0 or newer.
