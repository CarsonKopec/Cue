"""The plugin catalog: an ``index.json`` listing plugins, where to download them, and their checksums.

```json
{"plugins": [{"name": "anime", "version": "1.0.0", "title": "HiAnime",
              "providers": ["anime"], "url": "anime-1.0.0.zip", "sha256": "…"}]}
```

Relative ``url`` values are resolved against the catalog's own address, so the
index and the zips can be published side by side. A catalog may also be a local
``index.json`` file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from cue.plugins.archive import MAX_ARCHIVE_BYTES
from cue.plugins.manifest import Manifest, PluginError, parse_manifest

log = logging.getLogger(__name__)

MAX_INDEX_BYTES = 1024 * 1024
MAX_REDIRECTS = 5  # GitHub release downloads redirect once, to its file host
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class CatalogEntry:
    manifest: Manifest
    url: str  # an https:// address or a local file path
    sha256: str


def is_web_address(location: str) -> bool:
    return urlsplit(location).scheme in ("http", "https")


def check_address(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOOPBACK):
        return
    raise PluginError(f"plugins are only downloaded over https: {url}")


async def _download(http: httpx.AsyncClient, url: str, limit: int) -> bytes:
    requested = url
    try:
        # Redirects are followed here, one at a time, so every hop is checked before it's requested.
        for _ in range(MAX_REDIRECTS + 1):
            check_address(url)
            async with http.stream("GET", url, follow_redirects=False) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                if response.status_code != 200:
                    raise PluginError(f"{requested} answered HTTP {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise PluginError(f"{requested} is larger than expected")
                return bytes(body)
    except httpx.HTTPError as exc:
        raise PluginError(f"could not download {requested}: {type(exc).__name__}") from exc
    raise PluginError(f"{requested} redirects too many times")


def parse_catalog(document: Any, base: str) -> list[CatalogEntry]:
    """Read an index document; invalid entries are skipped and logged."""
    rows = document.get("plugins") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise PluginError("the catalog has no plugins list")
    entries: dict[str, CatalogEntry] = {}
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise PluginError("entry is not an object")
            manifest = parse_manifest(row, "catalog entry")
            sha256 = str(row.get("sha256", "")).lower()
            if not _SHA256.fullmatch(sha256):
                raise PluginError(f"{manifest.name} has no valid sha256")
            raw = row.get("url")
            if not isinstance(raw, str) or not raw:
                raise PluginError(f"{manifest.name} has no url")
            if is_web_address(base) or is_web_address(raw):
                url = urljoin(base, raw)
                check_address(url)
            else:
                url = str(Path(base).parent / raw)
        except PluginError as exc:
            log.warning("Skipping catalog entry: %s", exc)
            continue
        entries[manifest.name] = CatalogEntry(manifest, url, sha256)
    return sorted(entries.values(), key=lambda entry: entry.manifest.name)


async def fetch_catalog(http: httpx.AsyncClient, location: str) -> list[CatalogEntry]:
    """Load the catalog from an https:// address or a local file."""
    if is_web_address(location):
        data = await _download(http, location, MAX_INDEX_BYTES)
    else:
        try:
            data = Path(location).read_bytes()
        except OSError as exc:
            raise PluginError(f"could not read the catalog {location}: {exc.strerror}") from exc
    try:
        document = json.loads(data)
    except ValueError:
        raise PluginError("the catalog is not valid JSON") from None
    return parse_catalog(document, location)


async def fetch_archive(http: httpx.AsyncClient, url: str, sha256: str | None = None) -> bytes:
    """Download a plugin zip (or read a local one) and check it against ``sha256`` when given."""
    if is_web_address(url):
        data = await _download(http, url, MAX_ARCHIVE_BYTES)
    else:
        try:
            data = Path(url).read_bytes()
        except OSError as exc:
            raise PluginError(f"could not read {url}: {exc.strerror}") from exc
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise PluginError("the download doesn't match the catalog's checksum; nothing was installed")
    return data
