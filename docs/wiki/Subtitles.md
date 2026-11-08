Cue loads subtitles in your languages by itself, picks the first in the player, and lets you
choose others while you watch.

```toml
[subtitles]
languages = ["en"]        # in order of preference
auto_download = true      # false: only when you pick them
```

## Where they come from

1. **The streaming server.** Most sources publish subtitle tracks with the video. Cue
   downloads your preferred languages from those and selects the first. No account is
   needed for these.
2. **Subtitle sources**, for any language the server didn't have. Cue searches every
   source that's set up at once:

   | Source | Covers | Needs | Comes with |
   |---|---|---|---|
   | [OpenSubtitles](#opensubtitles) | Movies and TV in many languages, with exact matches for episodes on your PC | A free API key | Cue |
   | [Addic7ed (Gestdown)](#addic7ed-gestdown) | TV shows only | Nothing | The `gestdown` plugin |
   | [SubDL](#subdl) | Movies and TV in many languages | A free API key | The `subdl` plugin |

With no source set up, you get the server's subtitles only. Gestdown needs no key, so
installing it is the quickest way to get subtitles for TV shows.

Embedded subtitles (inside an `.mkv`, say) stay available in the player's own subtitle
menu.

## Setting up a source

On the Home screen press `Esc`, then `p`, and open the **Subtitles** tab. It lists every
source with its status: ready, off, or the setting it still needs.

| Key | What it does |
|---|---|
| `Enter` | Its settings, such as an API key. Saving turns it on. |
| `Space` | Turn it on or off |

Gestdown and SubDL appear there once their plugins are installed, from the **Plugins**
tab or with `cue plugin install gestdown` ([Plugins](Plugins)).

Each source keeps its settings in its own `[subtitles.<name>]` table in `config.toml`, so
you can set them there too:

```toml
[subtitles.opensubtitles]
api_key = "..."

[subtitles.subdl]
api_key = "..."

[subtitles.gestdown]
enabled = false        # installed, but not searched
```

### OpenSubtitles

Sign up at opensubtitles.com, then Profile → API consumers → New consumer, and use that
key.

For episodes on your PC, Cue first looks for an exact match of the file (by its hash).
Otherwise it searches by title, season and episode, and movies by title and year. The free
tier allows a few downloads a day, so Cue keeps every file it gets and never downloads one
twice.

### Addic7ed (Gestdown)

[Gestdown](https://www.gestdown.info) serves Addic7ed's subtitles through an API that
needs no account. Addic7ed has TV shows only, so movies get nothing from it.

Cue finds the show by its title, then the episode by season and number. Finished
translations come first, then those without hearing-impaired notes, then the most
downloaded. Gestdown sometimes needs a minute to collect a new episode's subtitles; it
says so, and asking again later works.

### SubDL

[SubDL](https://subdl.com) has movies and TV in many languages. Sign in at subdl.com, open
your profile, and copy the key from its API section.

Cue searches by title, plus the year for movies and the season and episode for shows.
Single-episode files come before whole-season packs, and from a season pack Cue takes only
the episode you're watching. `pt-br` asks SubDL for Brazilian Portuguese.

## How the search works

- Every ready source is asked at once. One that fails doesn't stop the others; Cue reports
  an error only when they all failed.
- Matches are tried in order: OpenSubtitles first, then sources from plugins by name.
  Within a source, its best match comes first.
- When a download fails (OpenSubtitles' daily limit, say), Cue tries the next match, from
  the same source or another.
- Cue keeps every file it gets and records which one each episode uses, so none is
  downloaded twice.

## Choosing while you watch

| Key | What it does |
|---|---|
| `c` | Cycle through the loaded subtitles |
| `Shift+C` | The picker: every server track and every source's results for this episode |

Each result in the picker says where it came from: `server`, `opensubtitles`, `gestdown`
or `subdl`. A choice from the picker applies to this episode; the next one uses your
languages again. The picker works with `auto_download = false` too.

## Language codes

Use two-letter codes (`en`, `fr`, `ja`). A general code takes any region; a specific one
(`pt-br`) prefers that region. When a server labels a track with the wrong code but the
right name ("English"), the name wins.

## Details

- Server tracks in SRT, WebVTT and ASS are checked and cached in `subs\provider\`.
- An HLS subtitle playlist holding one complete file is followed to that file. Segmented
  or live subtitle playlists are reported, and stay available in VLC's menu.
- HydraHD lists its subtitles inside the video playlist, marked not to show by default,
  so VLC alone would show none. Cue loads your language from that list, preferring full
  subtitles over CC/SDH and forced tracks.
- SUB and DUB can each have subtitles; it depends on that version's server. Subtitles are
  separate from the audio choice.
- OpenSubtitles files are saved as `subs\<show>\<episode>.<language>.srt`. Gestdown's and
  SubDL's go in `subs\gestdown\` and `subs\subdl\`, a folder for each show.
- Subtitles aren't saved next to episodes downloaded with `d`.
- Missing or unreadable tracks are reported without stopping the video.
- Cue doesn't make or translate subtitles where a source has none.

When [casting](Casting), each episode's subtitles are offered to the TV as tracks named
by language.

Plugins can add more sources: see [Writing a Plugin](Writing-a-Plugin#a-subtitle-source).
