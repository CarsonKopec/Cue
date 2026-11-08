"""Keeping Cue and its plugins up to date: GitHub releases for Cue itself, the catalog for plugins.

A frozen Cue checks once a day when it starts (``[updates] check``). A newer release's
program for this platform (``cue.exe``, ``cue-linux-x64``, ``cue-macos-arm64``, ...) is
downloaded over https, checked against the release's ``SHA256SUMS.txt``, and swapped in
for the running one: Windows won't overwrite a running program but will rename it, so
the old file is moved aside and the new one takes its place. The new version runs from
the next start. Plugins update from the catalog the same day.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from cue import __version__
from cue.config import app_dir
from cue.install import EXE, REG_SZ, UNINSTALL_KEY, InstallError, Registry, UserRegistry, installed
from cue.install import _clear_leftovers, _replace, _same_file
from cue.plugins import CatalogEntry, InstalledPlugin, PluginError, PluginManager, fetch_archive, fetch_catalog
from cue.plugins.actions import updates
from cue.plugins.manifest import is_newer, version_key

log = logging.getLogger(__name__)

RELEASES = "https://api.github.com/repos/CarsonKopec/Cue/releases/latest"
SUMS = "SHA256SUMS.txt"
CHECK_EVERY = timedelta(days=1)
STATE_FILE = "update-check.json"  # in the app folder: when Cue last looked
MAX_EXE_BYTES = 200 * 1024 * 1024
MAX_SMALL_BYTES = 1024 * 1024
MAX_REDIRECTS = 5  # GitHub release downloads redirect to its file host
TIMEOUT = httpx.Timeout(60, connect=10)


def platform_tag(system: str = sys.platform, machine: str | None = None) -> str:
    """``windows-x64``, ``linux-x64``, ``macos-arm64``, ...: what a release's files are named for."""
    machine = (machine if machine is not None else platform.machine()).lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine or "unknown")
    name = "windows" if system == "win32" else "macos" if system == "darwin" else "linux"
    return f"{name}-{arch}"


def asset_name(system: str = sys.platform, machine: str | None = None) -> str:
    """This platform's program in a release: ``cue.exe`` on Windows, ``cue-<platform>`` elsewhere."""
    return EXE if system == "win32" else f"cue-{platform_tag(system, machine)}"


ASSET = asset_name()
# How each platform's programs start: Windows (MZ), macOS (Mach-O, or a universal binary), Linux (ELF).
MAGIC = {"win32": (b"MZ",), "darwin": (bytes.fromhex("cffaedfe"), bytes.fromhex("cafebabe"))}.get(sys.platform, (bytes.fromhex("7f454c46"),))


class UpdateError(Exception):
    """Cue couldn't check for, download or install an update."""


@dataclass(frozen=True)
class Release:
    version: str
    page: str  # the release on GitHub
    exe: str  # the download address of this platform's program (ASSET)
    sums: str  # SHA256SUMS.txt's download address

    @property
    def newer(self) -> bool:
        return is_newer(self.version, __version__)


# -- fetching ---------------------------------------------------------------------------


async def _fetch(http: httpx.AsyncClient, url: str, limit: int, accept: str | None = None) -> bytes:
    """GET ``url`` over https only, following redirects one at a time and capping the size."""
    requested = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if urlsplit(url).scheme != "https":
                raise UpdateError(f"updates are only downloaded over https, not {url}")
            headers = {"Accept": accept} if accept else None
            async with http.stream("GET", url, headers=headers, follow_redirects=False, timeout=TIMEOUT) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                if response.status_code != 200:
                    raise UpdateError(f"{requested} answered HTTP {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise UpdateError(f"{requested} is larger than expected")
                return bytes(body)
    except httpx.HTTPError as exc:
        raise UpdateError(f"couldn't reach {urlsplit(requested).hostname}: {type(exc).__name__}") from exc
    raise UpdateError(f"{requested} redirects too many times")


async def latest_release(http: httpx.AsyncClient) -> Release:
    """Cue's newest published release on GitHub (drafts and pre-releases aren't offered)."""
    try:
        data = json.loads(await _fetch(http, RELEASES, MAX_SMALL_BYTES, "application/vnd.github+json"))
    except ValueError:
        raise UpdateError("GitHub's answer about releases wasn't JSON") from None
    if not isinstance(data, dict):
        raise UpdateError("GitHub's answer about releases was unexpected")
    tag = str(data.get("tag_name") or "")
    version = tag.removeprefix("v")
    try:
        version_key(version)
    except PluginError:
        raise UpdateError(f"the latest release's tag {tag!r} isn't a version number") from None
    assets = {
        asset.get("name"): asset.get("browser_download_url")
        for asset in data.get("assets") or [] if isinstance(asset, dict)
    }
    missing = [name for name in (ASSET, SUMS) if not isinstance(assets.get(name), str)]
    if missing:
        raise UpdateError(f"Cue {version}'s release has no {' or '.join(missing)}")
    return Release(version, str(data.get("html_url") or ""), assets[ASSET], assets[SUMS])


def _expected_checksum(sums: str) -> str | None:
    """The program's SHA-256 from a ``sha256sum`` listing (``<hash>  cue.exe`` or ``<hash> *cue.exe``)."""
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == ASSET and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


async def download(http: httpx.AsyncClient, release: Release) -> bytes:
    """The release's program for this platform, checked against its SHA256SUMS.txt."""
    sums = (await _fetch(http, release.sums, MAX_SMALL_BYTES)).decode("utf-8", "replace")
    expected = _expected_checksum(sums)
    if expected is None:
        raise UpdateError(f"Cue {release.version}'s {SUMS} doesn't list {ASSET}")
    data = await _fetch(http, release.exe, MAX_EXE_BYTES)
    if hashlib.sha256(data).hexdigest() != expected:
        raise UpdateError(f"the download doesn't match Cue {release.version}'s checksum; nothing was changed")
    if not data.startswith(MAGIC):
        raise UpdateError("the download isn't a program for this computer; nothing was changed")
    return data


# -- installing -------------------------------------------------------------------------------


def apply(data: bytes, version: str, running: Path | None = None, registry: Registry | None = None) -> Path:
    """Put the new program in place of the running one; it takes effect at the next start.

    For the installed copy, Settings > Apps is told the new version too.
    """
    target = running or Path(sys.executable)
    staged = target.with_name(f"cue-{version}.download")
    try:
        staged.write_bytes(data)
        if os.name != "nt":
            staged.chmod(0o755)  # a program, like the one it replaces
        _replace(staged, target)  # renames the running exe aside, then puts the new one in place
    except OSError as exc:
        raise UpdateError(f"couldn't replace {target}: {exc.strerror or exc}") from exc
    finally:
        staged.unlink(missing_ok=True)
    _clear_leftovers(target.parent)  # older copies set aside by earlier updates
    if registry is not None:
        found = installed(registry)
        if found is not None and _same_file(found.folder / EXE, target):
            registry.set(UNINSTALL_KEY, {"DisplayVersion": (version, REG_SZ)})
    return target


def user_registry() -> Registry | None:
    try:
        return UserRegistry()
    except InstallError:
        return None  # not Windows


# -- the daily check ---------------------------------------------------------------------------


def _state_file() -> Path:
    return app_dir() / STATE_FILE


def due(now: datetime | None = None) -> bool:
    """Whether a day has passed since the last check (or there wasn't one)."""
    try:
        checked = datetime.fromisoformat(json.loads(_state_file().read_text(encoding="utf-8"))["checked"])
    except (OSError, ValueError, KeyError, TypeError):
        return True
    now = now or datetime.now(UTC)
    return checked > now or now - checked >= CHECK_EVERY  # also when the clock went back


def mark_checked(now: datetime | None = None) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"checked": (now or datetime.now(UTC)).isoformat()}), encoding="utf-8")


@dataclass
class Outcome:
    """What an update run found and did."""

    release: Release | None = None  # Cue's latest release, when GitHub answered
    app_installed: bool = False  # it was newer and is now in place
    app_error: str = ""
    plugins_installed: list[InstalledPlugin] = field(default_factory=list)
    plugins_available: list[CatalogEntry] = field(default_factory=list)  # newer, not installed
    plugin_errors: list[str] = field(default_factory=list)

    def notices(self) -> list[tuple[str, str]]:
        """(title, message) for the good news only; failures stay in the log."""
        result = []
        if self.release is not None and self.app_installed:
            result.append(("Cue updated", f"Cue {self.release.version} is installed. "
                                          "It starts the next time you open Cue."))
        elif self.release is not None and self.release.newer:
            result.append(("Update available", f"Cue {self.release.version} is available: run cue update."))
        if self.plugins_installed:
            names = ", ".join(f"{p.manifest.title} {p.manifest.version}" for p in self.plugins_installed)
            result.append(("Plugins updated", f"Updated {names}."))
        elif self.plugins_available:
            names = ", ".join(f"{e.manifest.title} {e.manifest.version}" for e in self.plugins_available)
            result.append(("Plugin updates", f"New versions of {names}: run cue update."))
        return result


async def update_plugins(http: httpx.AsyncClient, manager: PluginManager, catalog: str, outcome: Outcome,
                         *, install: bool) -> None:
    """Install (or, without ``install``, list) newer versions of the plugins that are turned on.

    The plugins' providers keep their settings and on/off state: an update only
    replaces code.
    """
    try:
        entries = await fetch_catalog(http, catalog)
    except PluginError as exc:
        outcome.plugin_errors.append(f"the plugin catalog: {exc}")
        return
    newer = updates([plugin for plugin in manager.scan() if plugin.enabled], entries)
    if not install:
        outcome.plugins_available = list(newer.values())
        return
    for name, entry in newer.items():
        try:
            plugin = manager.install(await fetch_archive(http, entry.url, entry.sha256), entry.url)
        except (PluginError, OSError) as exc:
            outcome.plugin_errors.append(f"{name}: {exc}")
            continue
        if plugin.error:
            outcome.plugin_errors.append(f"{name} {entry.manifest.version} installed but didn't load: {plugin.error}")
        else:
            outcome.plugins_installed.append(plugin)


async def run(http: httpx.AsyncClient, manager: PluginManager, catalog: str, *, install: bool, app: bool = True,
              running: Path | None = None, registry: Registry | None = None) -> Outcome:
    """Check (and with ``install``, apply) updates for Cue itself (``app``) and its plugins."""
    outcome = Outcome()
    if app:
        try:
            outcome.release = await latest_release(http)
            if install and outcome.release.newer:
                data = await download(http, outcome.release)
                await asyncio.to_thread(apply, data, outcome.release.version, running, registry)
                outcome.app_installed = True
        except UpdateError as exc:
            outcome.app_error = str(exc)
            log.info("Cue update: %s", exc)
    if catalog:
        await update_plugins(http, manager, catalog, outcome, install=install)
        for problem in outcome.plugin_errors:
            log.info("Plugin update: %s", problem)
    return outcome
