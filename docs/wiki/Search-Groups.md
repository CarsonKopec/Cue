You search a **group**, not a single provider. The status bar shows which group you're in,
and `Tab` switches between them.

| Group | Has by default | Notes |
|---|---|---|
| Movies | HydraHD | Providers that list both movies and series show only their movies here. |
| Shows | HydraHD, Jellyfin, Local | ...and only their series here. |
| Anime | HiAnime, Jellyfin, Local | |
| All | every provider that's ready | Shown when it adds something. |

## How a search works

A search asks every provider in the group at the same time. Results appear as each one
answers, ranked by how well they match what you typed, and each is tagged with its
provider (`[hydra]`). A provider that fails is listed under the results, and the others
still show. `Tab` skips a group with no provider ready.

## Choosing groups

Each provider says which groups it belongs to, and one provider can be in several. You can
change that, or make your own groups, in the Plugins menu's **Groups** tab:

| Key | What it does |
|---|---|
| `Enter` | Choose the group's providers, and rename it |
| `d` | Start searches in this group |
| `n` | A new group |
| `x` | Remove a group of your own |

Or in the config:

```toml
default_group = "kids"

[providers.local]
path = "D:/Media"
groups = ["shows", "kids"]     # replaces the provider's own choice

[groups.kids]
title = "Kids"
media = "series"               # optional: "movie" or "series", like Movies and Shows
```

`media` keeps a group to movies or to series, for providers that list both.

## From the command line

```bash
cue -g anime
```

starts in a group, and

```bash
cue -p hydra
```

searches just one provider. `default_provider` in an older config still works: Cue
starts in that provider's first group.
