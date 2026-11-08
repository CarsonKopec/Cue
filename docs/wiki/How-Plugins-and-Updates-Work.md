How Cue adds and replaces code safely: plugins, its own updates, installing itself and
FFmpeg. To use these, see [Plugins](Plugins), [Updates](Updates) and
[Installing](Installing).

## Plugins

### What a plugin is

A folder with a `plugin.toml` (its name, version, the oldest Cue it needs, and what it
adds) and an `__init__.py`. Cue imports it as the Python package `cue_plugins.<name>`, and
it registers its providers, players or subtitle sources as it loads.
[Writing a Plugin](Writing-a-Plugin) shows one.

Plugins live in the `plugins\` folder in Cue's folder, one folder each.

### Knowing what each plugin added

While a plugin loads, Cue notes that everything registered came from it. That's how:

- turning a plugin off, updating it or removing it drops exactly its own providers,
  players and subtitle sources, and nobody else's;
- the Plugins menu says where each provider, player and subtitle source came from;
- an update re-imports the plugin's modules fresh, rather than reusing old ones.

Providers whose code and settings didn't change keep running through all of that.

### The catalog

The catalog is a JSON list of plugins: each with its manifest, the address of its `.zip`
and the zip's SHA-256. It's built by the
[cue-plugins](https://github.com/CarsonKopec/cue-plugins) repository's CI on every push to
`main`, and published as the `index.json` of that repository's `catalog` release.

### Installing one

1. **Download** (from the catalog or a link): over https only. Cue follows redirects one at
   a time, refusing any that leave https.
2. **Check** the zip's SHA-256 against the catalog's. A mismatch stops everything.
3. **Check the archive before unpacking anything:**
   - at most 20 MB packed, 64 MB unpacked and 2,000 files;
   - no path that would land outside the plugin's folder (`..`, absolute paths);
   - no links;
   - unpacked sizes are counted as they're written, rather than trusting what the zip
     claims.
4. **Unpack into a staging folder** next to the others, and read its `plugin.toml` there.
5. **Swap it in**: the old version is renamed aside and the new folder takes its place. A
   failed install leaves the previous version working.
6. **Load it**, and turn its providers on (settings you already had are kept).

## Cue updating itself

### Finding the release

Once a day (see [Updates](Updates)), Cue asks GitHub's API for the latest release.
Drafts and pre-releases aren't offered. The release's tag must be a version newer than the
running one.

Each release carries one program per system, and Cue picks its own:

| System | File |
|---|---|
| Windows | `cue.exe` |
| Linux x64 | `cue-linux-x64` |
| macOS Apple silicon | `cue-macos-arm64` |

### Checking it

1. Download `SHA256SUMS.txt` and find this program's line.
2. Download the program: https only, at most 200 MB.
3. Compare its SHA-256. A mismatch throws it away, and nothing changes.
4. Check that it starts like a program for this system: `MZ` on Windows, ELF on Linux,
   Mach-O on macOS.

### Replacing a running program

Windows won't overwrite a program that's running, but it will **rename** it. So Cue:

1. writes the new program beside the old one,
2. renames the running `cue.exe` aside to `cue.old-<random>.exe`,
3. moves the new one into place as `cue.exe`,
4. deletes the old one if it can. Otherwise it's still running, and it's cleared on a
   later start.

On Linux and macOS a running program's file can simply be replaced; Cue sets it as
executable first. The new version runs from the next start. For the installed copy on
Windows, the version shown in Settings → Apps is updated too.

## Installing Cue (Windows)

`cue install` needs no administrator rights, because it only touches your own account:

1. Copies `cue.exe` (and `mpv`, `vlc` or `yt-dlp.exe` from beside it) to
   `%LOCALAPPDATA%\Programs\Cue`.
2. Adds that folder to **your** PATH, in `HKEY_CURRENT_USER\Environment`. PATH entries keep
   their `%VARIABLE%` form (`REG_EXPAND_SZ`). Windows is told the environment changed, so
   new terminals see it.
3. Writes an entry in
   `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Uninstall\Cue`, so Cue
   appears in Settings → Apps with its version, icon and an Uninstall command
   (`cue uninstall`).

`cue uninstall` undoes all three. A running program can't delete itself, so if you
uninstall the copy that's running, its file is deleted a moment after it exits. Only Cue's
own files are removed, never anything else in the folder, and your settings stay.

## FFmpeg

`cue install` can fetch FFmpeg ([Installing](Installing#ffmpeg)):

1. Download gyan.dev's published SHA-256 for its "essentials" build, then the build itself:
   https only, at most 600 MB, hashed as it's written.
2. Compare the hashes. On a mismatch, nothing is installed.
3. Take only `bin/ffmpeg.exe` from the zip, check it's a Windows program, and delete the
   zip.
4. Move it in next to `cue.exe`, renaming any copy in use aside first, like `cue.exe` itself.

## Trust

A plugin is Python code running as you, like any program you install. Checksums prove a
download is the file the catalog or release lists. They can't make a plugin's code safe,
so Cue always shows where a plugin comes from and asks before installing it.
