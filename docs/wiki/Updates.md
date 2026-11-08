The downloaded Cue keeps itself and its plugins up to date, on Windows, Linux and macOS.

## When it checks

Once a day: the first time you open Cue after 24 hours have passed. The check runs in
the background, so start-up never waits, and being offline is simply ignored (that
launch still counts as the day's check). Commands such as `cue plugin list` don't check;
only opening the app does.

A copy of Cue running from source never checks by itself. Update it with `git pull` (or
`pip install -U`); `cue update` still updates its plugins.

## What happens

- **A newer Cue** is downloaded from the latest GitHub release: the program for your
  system (`cue.exe`, `cue-linux-x64` or `cue-macos-arm64`). It's checked against the
  release's `SHA256SUMS.txt` and thrown away if it doesn't match. Cue then puts it in
  place of the running copy. It's used from the next time you open Cue, and a
  notification says so. For the installed copy on Windows, Settings → Apps shows the new
  version too.
- **Newer plugins** from the catalog are installed straight away, checked against the
  catalog's checksums. Plugins you've turned off are left alone, and your providers'
  settings and on/off choices are kept.

[How Plugins and Updates Work](How-Plugins-and-Updates-Work) explains how a running
program gets replaced safely.

## Settings

```toml
[updates]
check = true      # false: never check by itself
install = true    # false: only tell you what's available
```

## Updating now

```bash
cue update
```

checks and installs straight away, and

```bash
cue update --check
```

only reports what's available. Cue remembers when it last checked in
`update-check.json` in its folder (see [Configuration](Configuration#files)). Delete
that file to make the next launch check again.
