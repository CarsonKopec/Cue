With `auto_skip = true`, Cue jumps over anime openings and endings as they start. `a`
turns it on and off while you watch, and `s` jumps ahead 85 seconds by hand at any time.

Skip times come from [AniSkip](https://api.aniskip.com), the community database
[ani-skip](https://github.com/synacktraa/ani-skip) uses. No player add-on is needed.

## How it works

1. **Find the show's MyAnimeList ID.** AniSkip is keyed by MAL ID. Cue tries, in order:
   - the provider's own MAL ID, if it has one;
   - its AniList ID, through the AniList API;
   - its AniDB or Kitsu ID, through the [ARM](https://arm.haglund.dev) mapping;
   - a title search: MyAnimeList's prefix search (as ani-skip does), with Jikan as a
     fallback.

   A title match has to score at least 75 out of 100 on a fuzzy comparison, which also
   weighs the season and year. No skip data is better than the wrong show's skip data.
2. **Get the episode's skip times:**
   `GET /v2/skip-times/{mal_id}/{episode}?types[]=op&types[]=ed&episodeLength=0`.
3. **Cache both** in `cue.db`, so each is looked up once.
4. **Watch the position.** While the episode plays, Cue reads the player's position several
   times a second. As soon as it enters an opening or ending, Cue seeks to the end of it
   and says so on the Now Playing screen.

No match or no data means normal playback, with "no skip data" on Now Playing.

## When it doesn't apply

- Providers for general movies and TV, such as HydraHD, turn the lookup off, so a film is
  never skipped on anime timings.
- Players Cue can't follow (ffplay, the Default app, Another program) can't be skipped
  in; see [Players](Players).
- It doesn't apply while [casting](Casting); the TV plays on its own.
