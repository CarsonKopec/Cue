How Cue's programs are built, checked and published.

## Building

With the `cue` environment active (see [Installing](Installing#from-source)):

```bash
python build/release.py
```

Without activating it, prefix the command with `conda run -n cue`.

[build/release.py](https://github.com/CarsonKopec/Cue/blob/master/build/release.py):

1. Runs [PyInstaller](https://pyinstaller.org) with
   [build/cue.spec](https://github.com/CarsonKopec/Cue/blob/master/build/cue.spec): one file,
   with a console, Textual's assets, and the libraries for AirPlay, Chromecast and
   decryption. That makes `dist/cue.exe` on Windows, or `dist/cue` on Linux and macOS.
   Build on each system for that system.
2. Starts the build with `--version` and `--smoke-test` (headless, with a throwaway
   `CUE_HOME`), so a build that can't start fails here.
3. Puts this system's release files in `dist/release/`:
   - the program under its release name: `cue.exe`, `cue-linux-x64` or `cue-macos-arm64`;
   - `cue-<version>-<platform>.zip` (Windows) or `.tar.gz`, with the program in a `cue`
     folder, the README and the licence.

Options:

| Option | What it does |
|---|---|
| `--mpv C:/tools/mpv` | Bundles an mpv folder (as `mpv\`) or a single `mpv.exe` in the archive |
| `--yt-dlp C:/tools/yt-dlp.exe` | Bundles yt-dlp in the archive |
| `--skip-build` | Packages an existing build in `dist/` |

### The smoke test

`cue --smoke-test` starts the app headless, renders the Home screen, checks fuzzy search,
checks the AirPlay, Chromecast and decryption libraries made it into the build, and exits.
Run it on a clean machine to check a build.

### What the spec takes care of

- **Plugins' imports.** Plugins can import only what's bundled, so the spec includes all of
  `cue` and a list of standard-library modules plugins commonly need.
- **numpy stays out.** conda-forge's rapidfuzz pulls in numpy, and with it hundreds of MB of
  MKL. Cue doesn't need it.
- **Certificates.** conda-forge's `certifi` has no `cacert.pem` of its own, so the spec
  bundles the environment's.
- **The icon.** `cue.exe` carries `assets/cue.ico`; Linux and macOS programs have no icon of
  their own.
- **The right DLLs.** PyInstaller finds DLLs on `PATH`. Run without the environment activated,
  it could bundle another environment's copy, such as a `libexpat.dll` the build's Python
  can't load. The build script puts its own environment's `Library\bin` first.

## CI

Every push and pull request runs [CI](https://github.com/CarsonKopec/Cue/blob/master/.github/workflows/ci.yml),
which uses the shared [build workflow](https://github.com/CarsonKopec/Cue/blob/master/.github/workflows/build.yml):

| System | Runner |
|---|---|
| Windows x64 | `windows-latest` |
| Linux x64 | `ubuntu-22.04`: an older glibc, so the program runs on more Linux systems |
| macOS Apple silicon | `macos-latest` |

On each: install, run the tests, then `build/release.py` (smoke test included). Each run keeps
the three builds as artifacts for a while, so you can try one before releasing.

## Releasing

The [Release workflow](https://github.com/CarsonKopec/Cue/blob/master/.github/workflows/release.yml)
builds on all three systems and publishes them together, only when you start it:

1. Raise `__version__` in
   [cue/__init__.py](https://github.com/CarsonKopec/Cue/blob/master/cue/__init__.py), and put
   its entry at the top of [CHANGELOG.md](https://github.com/CarsonKopec/Cue/blob/master/CHANGELOG.md)
   (`# Cue 0.4.1: what changed — date`). That entry becomes the release notes.
2. Push to master, then start the workflow: Actions → Release → Run workflow, or

   ```bash
   gh workflow run release.yml
   ```

It refuses a version that's already released, builds and tests on all three systems, tags
the commit `v<version>`, and attaches every system's program and archive with one
`SHA256SUMS.txt` covering them all. Running copies of Cue update to it within a day
([Updates](Updates)).

Options: a **draft** (publish it yourself after checking), a **pre-release**, and a **dry run**
that builds everything but publishes nothing, keeping the files as a workflow artifact:

```bash
gh workflow run release.yml -f dry_run=true
```

## The wiki

These pages are in [`docs/wiki/`](https://github.com/CarsonKopec/Cue/tree/master/docs/wiki).
The [Wiki workflow](https://github.com/CarsonKopec/Cue/blob/master/.github/workflows/wiki.yml)
copies them to the GitHub wiki whenever they change on master. Page names come from file
names (`How-Cue-Works.md` is "How Cue Works"); `_Sidebar.md` and `_Footer.md` are on every
page. Edits made on GitHub's wiki pages directly are replaced by the next copy, so change
the files instead.

## Plugins

Plugins are built and published separately, from
[cue-plugins](https://github.com/CarsonKopec/cue-plugins): every push to its `main` branch
rebuilds the catalog.

## The logo

The logo is in [assets/](https://github.com/CarsonKopec/Cue/tree/master/assets):

| File | What it is |
|---|---|
| `cue-icon.svg`, `cue-icon.png` | The app icon (512 px PNG) |
| `cue-mark.svg` | The mark alone, on a transparent background |
| `cue-wordmark-dark.svg`, `cue-wordmark-light.svg` | The "Cue" wordmark, for dark and light backgrounds |
| `cue.ico` | The icon at 16–256 px, built into `cue.exe` |

`cue.ico` is rendered from `cue-icon.svg`. Render it again at those sizes if the icon changes.
