"""HydraHD's public movie and television catalog.

Television seasons have separate, stable Show IDs because Cue's history and
download queue key episodes by show ID and episode number.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from cue.providers import register
from cue.providers.base import Episode, ProviderConfigError, ProviderError, Show, Stream


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HYDRA_HOSTS = {"hydrahd.com", "www.hydrahd.com", "hydrahd.gd", "www.hydrahd.gd"}
_MOVIE_PATH = re.compile(r"/movie/[1-9]\d*-watch-[A-Za-z0-9_%.-]+")
_SERIES_PATH = re.compile(r"/watchseries/([A-Za-z0-9_-]+)-online-free")
_MAX_PAGE_BYTES = 2 * 1024 * 1024
_MAX_SEARCH_CARDS = 60
_SERIES_CONCURRENCY = 4
_LOG = logging.getLogger(__name__)


def _url_origin(url: str) -> tuple[str, str, int] | None:
    try:
        if any(ord(character) < 33 or character == "\\" for character in url):
            return None
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None:
            return None
        return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None


def _catalog_path(value: str, base_url: str) -> str | None:
    """Accept catalog links, never person pages, advertisements or off-site links."""
    # Some live cards include a trailing space in href, which browsers trim.
    absolute = urljoin(base_url, value.strip())
    origin = _url_origin(absolute)
    if origin is None or (
        origin != _url_origin(base_url)
        and not (origin[0] == "https" and origin[1] in _HYDRA_HOSTS and origin[2] == 443)
    ):
        return None
    parts = urlsplit(absolute)
    if parts.query or parts.fragment:
        return None
    path = parts.path.rstrip("/")
    if _MOVIE_PATH.fullmatch(path) or _SERIES_PATH.fullmatch(path):
        return path
    return None


def _show_identity(show_id: str) -> tuple[str, int | None]:
    if _MOVIE_PATH.fullmatch(show_id):
        return show_id, None
    path, separator, suffix = show_id.partition("#season=")
    if separator and _SERIES_PATH.fullmatch(path) and re.fullmatch(r"\d{1,3}", suffix):
        return path, int(suffix)
    raise ProviderError("HydraHD saved title is invalid; search for the title again.")


@dataclass
class _Card:
    path: str
    title: str
    year: int | None


class _SearchParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.cards: list[_Card] = []
        self._card: dict[str, Any] | None = None
        self._seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "figure" and "figured" in classes:
            self._card = {"path": None, "title": "", "fallback": "", "year": None}
        elif self._card is not None:
            if tag == "a" and "hthis" in classes:
                path = _catalog_path(values.get("href") or "", self.base_url)
                if path:
                    self._card["path"] = path
                    self._card["fallback"] = re.sub(r"\s+online free$", "", values.get("title") or "", flags=re.I)
            elif tag == "img" and self._card["path"] and values.get("alt"):
                self._card["title"] = " ".join(values["alt"].split())

    def handle_data(self, data: str) -> None:
        if self._card is not None and self._card["year"] is None and re.fullmatch(r"\s*(?:18|19|20)\d{2}\s*", data):
            self._card["year"] = int(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "figure" or self._card is None:
            return
        card, self._card = self._card, None
        title = " ".join((card["title"] or card["fallback"]).split())
        if card["path"] and title and card["path"] not in self._seen:
            self._seen.add(card["path"])
            self.cards.append(_Card(card["path"], title, card["year"]))


class _EpisodeParser(HTMLParser):
    def __init__(self, series_path: str) -> None:
        super().__init__(convert_charrefs=True)
        match = _SERIES_PATH.fullmatch(series_path)
        self.slug = match.group(1) if match else ""
        self.title = ""
        self.episodes: dict[int, dict[int, str | None]] = {}
        self._h1 = False
        self._current: tuple[int, int] | None = None
        self._episode_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "h1" and not self.title:
            self._h1 = True
        if tag != "a":
            return
        self._current = None
        self._episode_text = []
        if "dynamic-ep-link" not in (values.get("class") or "").split() or values.get("data-slug") != self.slug:
            return
        season, episode = values.get("data-season") or "", values.get("data-episode") or ""
        if not re.fullmatch(r"\d{1,3}", season) or not re.fullmatch(r"\d{1,5}", episode):
            return
        self._current = int(season), int(episode)

    def handle_data(self, data: str) -> None:
        if self._h1:
            self.title += data
        if self._current is not None:
            self._episode_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self._h1 = False
            self.title = " ".join(self.title.split())
        elif tag == "a" and self._current is not None:
            season, number = self._current
            title = " ".join(" ".join(self._episode_text).split())
            title = re.sub(r"^Episode\s+\d+\s*", "", title, flags=re.I)
            title = re.sub(r"^\d{4}-\d{2}-\d{2}\s*-\s*", "", title)
            self.episodes.setdefault(season, {}).setdefault(number, title or None)
            self._current = None


def _is_challenge(html: str) -> bool:
    # A passive /cdn-cgi/challenge-platform/.../jsd script is also present on
    # usable pages. Only challenge page titles/forms are blocking evidence.
    title = re.search(r"<title\b[^>]*>(.*?)</title\s*>", html, re.I | re.S)
    if title and re.search(r"just a moment|attention required|access denied|verify (?:you|your)", title.group(1), re.I):
        return True
    return bool(re.search(r"<form\b[^>]*(?:id\s*=\s*['\"]challenge-form['\"]|action\s*=\s*['\"][^'\"]*/cdn-cgi/)", html, re.I))


@register
class HydraProvider:
    name = "hydra"
    skip_supported = False

    def __init__(self, options: Mapping[str, Any], *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        base_url = options.get("base_url", "https://hydrahd.com")
        if not isinstance(base_url, str) or _url_origin(base_url) is None:
            raise ProviderConfigError("HydraHD base_url must be an HTTP(S) website address without credentials.")
        parts = urlsplit(base_url)
        if parts.query or parts.fragment or parts.path not in {"", "/"}:
            raise ProviderConfigError("HydraHD base_url must be the website root, such as https://hydrahd.com.")
        try:
            timeout = float(options.get("timeout", 20))
        except (TypeError, ValueError):
            raise ProviderConfigError("HydraHD timeout must be a positive number.") from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise ProviderConfigError("HydraHD timeout must be a positive number.")
        self.base_url = base_url.rstrip("/")
        self._origin = _url_origin(self.base_url)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            timeout=timeout, follow_redirects=False, transport=transport,
        )
        # Embedded servers never inherit cookies from the catalog client.
        self._resolver_client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=timeout,
            follow_redirects=False, transport=transport,
        )
        self._series: dict[str, _EpisodeParser] = {}

    def _allowed_url(self, url: str) -> bool:
        origin = _url_origin(url)
        return bool(origin and (
            origin == self._origin
            or (origin[0] == "https" and origin[1] in _HYDRA_HOSTS and origin[2] == 443)
        ))

    async def _get(self, url: str) -> tuple[str, str]:
        """Read a bounded public HTML page with explicit catalog redirect rules."""
        if not self._allowed_url(url):
            raise ProviderError("HydraHD returned an unsupported catalog address.")
        try:
            for hop in range(4):
                async with self._client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = urljoin(url, response.headers.get("location", ""))
                        if not response.headers.get("location") or not self._allowed_url(target):
                            raise ProviderError("HydraHD redirected outside its supported catalog domains.")
                        if urlsplit(url).scheme == "https" and urlsplit(target).scheme != "https":
                            raise ProviderError("HydraHD redirected to an insecure catalog address.")
                        if hop == 3:
                            raise ProviderError("HydraHD redirected too many times.")
                        url = target
                        continue
                    if response.status_code in {401, 403, 429} or response.headers.get("cf-mitigated", "").lower() == "challenge":
                        raise ProviderError("HydraHD requires access verification or is rate limiting requests; Cue cannot bypass it.")
                    if response.status_code >= 400:
                        raise ProviderError(f"HydraHD request failed (HTTP {response.status_code}).")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if content_type and content_type not in {"text/html", "application/xhtml+xml"}:
                        raise ProviderError("HydraHD returned an unsupported catalog response.")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_PAGE_BYTES:
                            raise ProviderError("HydraHD catalog page is too large.")
                    html = bytes(body).decode(response.encoding or "utf-8", errors="replace")
                    if _is_challenge(html):
                        raise ProviderError("HydraHD requires access verification; Cue cannot bypass it.")
                    return str(response.url), html
        except httpx.HTTPError as exc:
            raise ProviderError(f"Cannot reach HydraHD ({type(exc).__name__}).") from exc
        raise ProviderError("HydraHD did not return a catalog page.")

    async def _series_detail(self, path: str) -> _EpisodeParser:
        if path not in self._series:
            _, html = await self._get(urljoin(self.base_url, path))
            parsed = _EpisodeParser(path)
            parsed.feed(html)
            if not parsed.episodes:
                raise ProviderError("HydraHD did not list any supported episodes for this series.")
            self._series[path] = parsed
        return self._series[path]

    async def search(self, query: str) -> list[Show]:
        if not query.strip():
            return []
        page_url, html = await self._get(f"{self.base_url}/index.php?{urlencode({'menu': 'search', 'query': query.strip()})}")
        parser = _SearchParser(page_url)
        parser.feed(html)
        if not parser.cards:
            if not re.search(r"Movie and TV Results For|\bbrowse-grid\b", html, re.I):
                raise ProviderError("HydraHD search page was not recognized.")
            return []
        semaphore = asyncio.Semaphore(_SERIES_CONCURRENCY)

        async def expand(card: _Card) -> list[Show]:
            if _MOVIE_PATH.fullmatch(card.path):
                return [Show(card.path, card.title, year=card.year, episode_count=1, media_type="movie")]
            async with semaphore:
                detail = await self._series_detail(card.path)
            title = detail.title or card.title
            return [
                Show(
                    f"{card.path}#season={season}", f"{title} — Season {season}",
                    year=card.year, episode_count=len(detail.episodes[season]), series_title=title,
                )
                for season in sorted(detail.episodes)
            ]

        results = await asyncio.gather(*(expand(card) for card in parser.cards[:_MAX_SEARCH_CARDS]), return_exceptions=True)
        shows: list[Show] = []
        errors: list[ProviderError] = []
        for result in results:
            if isinstance(result, ProviderError):
                errors.append(result)
                _LOG.warning("Could not expand a HydraHD series: %s", result)
            elif isinstance(result, BaseException):
                raise result
            else:
                shows.extend(result)
        if not shows and errors:
            raise errors[0]
        return shows

    async def episodes(self, show: Show) -> list[Episode]:
        path, season = _show_identity(show.id)
        if season is None:
            show.media_type = "movie"
            show.episode_count = 1
            show.series_title = None
            # The observed movie route ends with the release year. History only
            # stores the ID/title, so restore this matching hint on resume.
            year = re.search(r"-((?:18|19|20)\d{2})-online$", path)
            if show.year is None and year:
                show.year = int(year.group(1))
            return [Episode(show.id, 1, title=show.title)]
        detail = await self._series_detail(path)
        if season not in detail.episodes:
            raise ProviderError(f"HydraHD no longer lists season {season} for this series.")
        show.media_type = "episode"
        show.series_title = detail.title or show.series_title
        show.episode_count = len(detail.episodes[season])
        return [
            Episode(show.id, number, title=title, season=season)
            for number, title in sorted(detail.episodes[season].items())
        ]

    async def stream(self, episode: Episode) -> list[Stream]:
        path, season = _show_identity(episode.show_id)
        if not math.isfinite(episode.number) or not float(episode.number).is_integer():
            raise ProviderError("HydraHD episode number is invalid.")
        if season is None:
            if episode.number != 1:
                raise ProviderError("HydraHD movies have a single playback item.")
        else:
            if episode.season is not None and episode.season != season:
                raise ProviderError("HydraHD episode season does not match its saved title.")
            detail = await self._series_detail(path)
            if int(episode.number) not in detail.episodes.get(season, {}):
                raise ProviderError("HydraHD no longer lists this episode.")
            path = f"{path}/season/{season}/episode/{int(episode.number)}"
        page_url, html = await self._get(urljoin(self.base_url, path))
        from cue.providers.hydra_resolvers import resolve_page

        return await resolve_page(self._resolver_client, page_url, html, user_agent=USER_AGENT)

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._resolver_client.aclose()
