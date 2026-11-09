First, look in `cue.log`, in Cue's folder (`%APPDATA%\cue\` on Windows, `~/.config/cue/`
elsewhere). It records what Cue tried and what went wrong.

## Starting Cue

**"No providers configured" / nothing to search.** Set up at least one
[provider](Providers): on the Home screen press `Esc`, then `p`. The Home screen says
what a provider is missing.

**Cue stops at start with a config error.** The message names the setting and what's
wrong with it, such as `cast.port must be a port number between 1 and 65535`. Fix it in
`config.toml` ([Configuration](Configuration)).

**macOS says the program can't be opened.** It isn't signed. Run
`xattr -d com.apple.quarantine cue-macos-arm64` once ([Installing](Installing#macos)).

**`cue` isn't found after installing.** Open a new terminal: PATH changes reach only
terminals opened afterwards.

## Playing

**VLC isn't found.** Install it from videolan.org, or set its path in the Players tab
(`Enter`) or with `CUE_VLC`. The Players tab says whether each player's program was
found.

**An episode won't start.** The provider's message says why. Sites change, so a provider
plugin may need an update: `cue plugin update`, or `cue update`. HiAnime tries other
servers with the same language; HydraHD tries up to six.

**No auto-skip.** The show has no AniSkip data, the provider isn't anime (HydraHD), or the
player is one Cue can't follow. Now Playing says which; see [Skipping](Skipping).

**Auto-next doesn't move on.** VLC has to report the episode's length and stop near its
end. Press `n`.

**"Cue needs FFmpeg" with the Default app or Another program.** On Windows, run
`cue install --ffmpeg`. Elsewhere, install FFmpeg with your package manager.

**"Not enough free space".** Before downloading for a player that only opens files, Cue
checks the folder has room. Choose a smaller version or another folder:

```toml
[playback]
quality = "720"
folder = "D:/Temp/cue"
```

## Subtitles

**None appear.** The server may have none in your language. Set up a subtitle source for
a fallback: OpenSubtitles or SubDL with a free key, or Gestdown for TV shows with none
([Subtitles](Subtitles#setting-up-a-source)). Then open the picker (`Shift+C`) to see
every track. The Subtitles tab in the Plugins menu (`Esc`, `p`) says whether each source
is ready.

**"allowed 5 subtitles" or another quota message.** OpenSubtitles' free tier allows a few
downloads a day. Cue keeps every file it gets, so episodes you've watched before don't
count again, and with another source set up, Cue uses its match instead.

**"SubDL refused the API key".** Copy the key again from your subdl.com profile, and set
it with `Enter` on SubDL in the Subtitles tab.

**"Gestdown is still collecting this episode's subtitles".** Gestdown fetches new
episodes from Addic7ed on request; try again in a minute.

## Casting

**The TV doesn't list Cue, or can't connect.**

1. Is `[cast] enabled = true`, and did you restart Cue?
2. Is your network set to **Private** in Windows, and did you allow Cue on private
   networks? (Settings → Privacy & security → Windows Security → Firewall → Allow an
   app.)
3. With several network adapters (Wi-Fi and Ethernet, a VPN, Hyper-V or WSL), set
   `[cast] interface` to the one the TV is on.
4. Open `http://<this PC's address>:8770/` in the TV's browser. If Cue's page loads, the
   network works, and `cue.log` lists the TV's requests (lines starting `cast`).

**No TVs in the list.** Turn the TV on (not standby), check it's on the same network, and
press `r` in the list. Samsung and LG TVs are listed as DLNA, not AirPlay
([why](Casting#airplay)).

**A DLNA TV plays without subtitles.** Turn subtitles on in the TV's own settings while it
plays. Cue sends your first language along with every episode that has one.

**An AirPlay TV keeps asking for a code.** Delete its entry in `airplay.json` (or the whole
file) and pair again.

More in [Casting](Casting#when-it-doesnt-work).

## Updates and plugins

**Cue didn't update.** It checks once a day, in the background. `cue update --check` says
what's available, and `cue update` installs it now. A download that doesn't match its
checksum is thrown away and nothing changes. A source checkout never updates itself.

**A plugin shows an error.** The Plugins tab shows it next to the plugin. Try
`cue plugin update`, or remove it (`x`) and install it again. If it needs a newer Cue, its
`requires` says which.

## Reporting a problem

Open an issue on [GitHub](https://github.com/CarsonKopec/Cue/issues) with:

- what you did and what you expected,
- `cue --version`,
- the end of `cue.log`.

Remove any API keys or addresses first.
