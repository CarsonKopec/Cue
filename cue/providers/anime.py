"""HiAnime catalog provider for Cue.

Implements catalog discovery and resolves supported server pages to media URLs.
The resolver reads the same publicly delivered player data as the browser;
authentication challenges, DRM, and unsupported player formats are not bypassed.

Cue conventions follow cue.providers.local:
    @register
    name = "anime"
    __init__(options: Mapping[str, Any])
    async search(query), episodes(show), stream(episode), aclose()

Dependency: httpx
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from cue.providers import register
from cue.providers.base import Episode, ProviderConfigError, ProviderError, Show, Stream
from cue.providers.anime_resolvers import resolve_server, streams_from_payload


BASE_API = "https://hianime.at"
SEARCH_API = "/search?keyword={keyword}"
EPISODES_API = "/api/theme/episode/list/{show_id}"
SERVERS_API = "/api/theme/episode/servers?episodeId={episode_id}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# The TLS cipher strings from ani-cli are retained as configuration/documentation.
# Python's SSLContext.set_ciphers() configures TLS <= 1.2; TLS 1.3 suites are
# selected by the underlying OpenSSL/Python build and cannot be set with it.
CIPHERS = (
    "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305"
)
TLS13_CIPHERS = (
    "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:"
    "TLS_CHACHA20_POLY1305_SHA256"
)

_LOG = logging.getLogger(__name__)


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None


class _SearchParser(HTMLParser):
    """Extract watch-page anchors from the HTML search results."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str]] = []
        self._anchor: dict[str, str] | None = None
        self._anchor_depth = 0
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag == "a" and (
            "/watch/" in attrs_dict.get("href", "")
            or "watch" in attrs_dict.get("class", "").split()
        ):
            self._anchor = attrs_dict
            self._anchor_depth = 1
            self._title_parts = []
        elif self._anchor is not None:
            self._anchor_depth += 1

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._anchor is None:
            return
        if tag == "a":
            attrs = self._anchor
            href = attrs.get("href", "").strip()
            title = (
                attrs.get("title", "").strip()
                or attrs.get("data-jname", "").strip()
                or " ".join(" ".join(self._title_parts).split())
            )
            if href and title:
                self.results.append((href, title))
            self._anchor = None
            self._anchor_depth = 0
            self._title_parts = []


class _ServerParser(HTMLParser):
    """Extract server choices from the HTML fragment inside HiAnime's JSON."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.servers: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "div":
            return
        attrs_dict = {key: value or "" for key, value in attrs}
        classes = attrs_dict.get("class", "").split()
        if "server-item" not in classes:
            return
        encoded_url = attrs_dict.get("data-hash", "").strip()
        if not encoded_url:
            return
        try:
            decoded = base64.b64decode(encoded_url, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return
        try:
            parsed = urlparse(decoded)
            valid = (
                parsed.scheme in ("http", "https") and parsed.hostname
                and not parsed.username and not parsed.password
            )
            parsed.port  # Invalid ports also make a malformed server URL.
        except ValueError:
            return
        if not valid:
            return
        name = attrs_dict.get("data-server-name", "Unknown server").strip()
        language = attrs_dict.get("data-type", "").strip().lower()
        self.servers.append((decoded, name or "Unknown server", language))


def _extract_server_choices(html: str) -> list[tuple[str, str, str]]:
    """Return decoded server/embed URLs, server names, and language labels.

    These URLs identify provider server pages; they are not assumed to be direct
    media files and must not be passed off as playable streams without resolution.
    """
    parser = _ServerParser()
    parser.feed(html)
    seen: set[tuple[str, str]] = set()
    choices: list[tuple[str, str, str]] = []
    for url, name, language in parser.servers:
        key = (url, language)
        if key not in seen:
            seen.add(key)
            choices.append((url, name, language))
    return choices


def _extract_search_results(html: str, base_url: str) -> list[tuple[str, str]]:
    parser = _SearchParser()
    parser.feed(html)
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, title in parser.results:
        url = urljoin(base_url, href)
        # HiAnime show watch paths generally have an ID/slug after /watch/.
        match = re.search(r"/watch/([^/?#]+)", url)
        if not match:
            continue
        show_id = match.group(1)
        if show_id not in seen:
            seen.add(show_id)
            output.append((show_id, title))
    return output



class _EpisodeParser:
    """Parse HiAnime's episode-list HTML using the same fragment pattern as ani-cli.

    HiAnime's `data-number` and `data-id` may be on the ep-item container while
    the matching `/watch/<slug>?ep=...` URL is on a nested anchor. Parsing each
    ep-item fragment together is more reliable than requiring those attributes
    to occur on a single HTML element.
    """

    def __init__(self, show_id: str) -> None:
        self.show_id = show_id
        self.items: list[tuple[float, str, str | None]] = []

    def feed(self, html: str) -> None:
        html = html.replace("\\\\", "")
        # Match ani-cli's `sed 's|ep-item|\\nep-item|g'` strategy.
        fragments = re.split(r"ep-item", html, flags=re.IGNORECASE)
        for fragment in fragments[1:]:
            number_match = re.search(r'data-number="([^"]*)"', fragment)
            id_match = re.search(r'data-id="([0-9]+)"', fragment)
            watch_match = re.search(
                rf'/watch/{re.escape(self.show_id)}\?ep=', fragment
            )
            if not (number_match and id_match and watch_match):
                continue
            number = _float(number_match.group(1))
            if number is None:
                continue
            title_match = re.search(r'title="([^"]*)"', fragment)
            title = title_match.group(1) if title_match else None
            self.items.append((number, id_match.group(1), title))


@register
class AnimeProvider:
    """HiAnime search/episode adapter, following Cue's provider protocol."""

    name = "anime"
    audio_modes = ("sub", "dub")

    def __init__(
        self, options: Mapping[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.base_api = str(options.get("base_api", BASE_API)).rstrip("/")
        if not self.base_api.startswith(("https://", "http://")):
            raise ProviderConfigError("providers.anime.base_api must be an HTTP(S) URL")

        self.search_api = str(options.get("search_api", SEARCH_API))
        self.episodes_api = str(options.get("episodes_api", EPISODES_API))
        self.servers_api = str(options.get("servers_api", SERVERS_API))
        self.language = str(options.get("language", "sub")).strip().lower()
        if self.language not in ("sub", "dub"):
            raise ProviderConfigError("providers.anime.language must be 'sub' or 'dub'")
        self.timeout = float(options.get("timeout", 20))
        self.debug_response = bool(options.get("debug_response", False))
        self.debug_log_file = str(options.get("debug_log_file", "cue-anime-debug.log"))
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={
                "User-Agent": str(options.get("agent", USER_AGENT)),
                "Accept": "text/html,application/json,application/xhtml+xml,*/*",
                **dict(options.get("headers", {})),
            },
            follow_redirects=True,
            transport=transport,
        )
        # Catalog credentials and cookies must not be sent to third-party embeds.
        self._resolver_client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout), follow_redirects=False, transport=transport
        )
        self._episode_ids: dict[tuple[str, float], str] = {}
        self._episode_counts: dict[str, int] = {}

    def _url(self, template: str, **values: Any) -> str:
        try:
            path = template.format(**{key: str(value) for key, value in values.items()})
        except KeyError as exc:
            raise ProviderConfigError(f"Unknown endpoint placeholder: {exc.args[0]}") from exc
        return urljoin(self.base_api + "/", path)

    async def _get(self, url: str, *, params: Mapping[str, Any] | None = None) -> httpx.Response:
        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"HiAnime returned HTTP {exc.response.status_code} for {exc.request.url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not contact HiAnime: {exc}") from exc

    def _log_response(self, label: str, response: httpx.Response) -> None:
        """Append response metadata and a bounded body preview to a debug file."""
        if not self.debug_response:
            return
        from datetime import datetime, timezone
        from pathlib import Path

        body = response.text
        preview_limit = 12000
        preview = body[:preview_limit]
        if len(body) > preview_limit:
            preview += f"\n... [truncated; total chars={len(body)}]"
        log_path = Path(self.debug_log_file).expanduser()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\n[{datetime.now(timezone.utc).isoformat()}] "
                    f"HiAnime {label} response\n"
                    f"status={response.status_code}\n"
                    f"url={response.url}\n"
                    f"content-type={response.headers.get('content-type', 'unknown')}\n"
                    f"bytes={len(response.content)}\n"
                    "----- BEGIN BODY PREVIEW -----\n"
                    f"{preview}\n"
                    "----- END BODY PREVIEW -----\n"
                )
        except OSError as exc:
            _LOG.exception("Could not write HiAnime debug log to %s: %s", log_path, exc)

    async def _json(self, url: str, *, label: str = "json") -> Any:
        response = await self._get(url)
        self._log_response(label, response)
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(f"HiAnime returned invalid JSON for {url}") from exc

    async def search(self, query: str) -> list[Show]:
        query = query.strip()
        if not query:
            return []
        url = self._url(self.search_api, keyword=quote(query, safe=""))
        response = await self._get(url)
        self._log_response("search", response)
        results = _extract_search_results(response.text, self.base_api)
        return [
            Show(id=show_id, title=title)
            for show_id, title in results
        ]

    async def episodes(self, show: Show) -> list[Episode]:
        # ani-cli uses "${show_id##*-}": the endpoint expects the trailing
        # numeric catalog ID, not the full watch-page slug.
        numeric_id = show.id.rsplit("-", 1)[-1]
        if not numeric_id.isdigit():
            raise ProviderError(
                f"Cannot extract HiAnime numeric ID from show slug: {show.id}"
            )

        url = self._url(self.episodes_api, show_id=quote(numeric_id, safe=""))
        response = await self._get(url)
        self._log_response("episode-list", response)

        # The endpoint returns JSON with the episode-list HTML inside `html`.
        # Parse that fragment rather than parsing the outer JSON document.
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("HiAnime episode-list endpoint returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderError("HiAnime episode-list endpoint returned an unexpected JSON shape")
        if payload.get("status") is False:
            raise ProviderError("HiAnime episode discovery reported that this show is unavailable")
        html = payload.get("html", "")
        if not isinstance(html, str):
            raise ProviderError("HiAnime episode-list response is missing its HTML fragment")

        parser = _EpisodeParser(show.id)
        parser.feed(html.replace("\\\\", ""))

        # Some response variants attach data-number/data-id to the outer
        # ep-item element; the parser collects those directly.
        unique: dict[float, Episode] = {}
        for number, episode_id, title in parser.items:
            self._episode_ids[(show.id, number)] = episode_id
            unique.setdefault(
                number,
                Episode(
                    show_id=show.id,
                    number=number,
                    title=title,
                ),
            )

        result = sorted(unique.values(), key=lambda ep: ep.number)
        if not result:
            raise ProviderError(
                "HiAnime returned an episode-list response, but no episode "
                "entries could be parsed. Its HTML markup may have changed."
            )
        self._episode_counts[show.id] = len(result)
        show.episode_count = len(result)
        return result

    def for_language(self, language: str) -> _AnimeLanguageView:
        """Bind a queue's audio choice without changing other active queues."""
        if language not in self.audio_modes:
            raise ProviderConfigError("HiAnime audio mode must be 'sub' or 'dub'")
        return _AnimeLanguageView(self, language)

    async def stream(self, episode: Episode) -> list[Stream]:
        """Discover this episode's servers and resolve the selected language."""
        return await self._stream(episode, language=self.language)

    async def _stream(self, episode: Episode, *, language: str) -> list[Stream]:
        key = (episode.show_id, episode.number)
        if key not in self._episode_ids:
            await self.episodes(Show(id=episode.show_id, title=episode.show_id))
        episode_id = self._episode_ids.get(key)
        if episode_id is None:
            raise ProviderError(f"HiAnime episode {episode.label} was not found in its episode list")
        watch_url = self._url(
            "/watch/{show_id}?ep={episode_id}",
            show_id=quote(episode.show_id, safe=""), episode_id=quote(episode_id, safe=""),
        )
        url = self._url(
            self.servers_api,
            episode_id=quote(episode_id, safe=""),
            show_id=quote(episode.show_id, safe=""),
            number=episode.number,
        )
        payload = await self._json(url, label="servers")
        if isinstance(payload, dict) and payload.get("status") is False:
            raise ProviderError("HiAnime server discovery reported that this episode is unavailable")
        user_agent = self._client.headers["User-Agent"]
        streams = streams_from_payload(
            payload, base_url=url,
            headers={"User-Agent": user_agent, "Referer": watch_url},
            language=language,
        )
        if streams:
            return streams
        server_choices: list[tuple[str, str, str]] = []
        if isinstance(payload, dict):
            html = payload.get("html")
            if isinstance(html, str):
                server_choices = _extract_server_choices(html)

        if not server_choices:
            if streams_from_payload(
                payload, base_url=url,
                headers={"User-Agent": user_agent, "Referer": watch_url},
            ):
                raise ProviderError(f"HiAnime has no {language.upper()} streams for this episode")
            raise ProviderError(
                "HiAnime's server endpoint returned no direct media URLs or "
                "recognizable server choices. Its response markup may have changed."
            )
        choices = [choice for choice in server_choices if choice[2] == language]
        if not choices:
            raise ProviderError(f"HiAnime has no {language.upper()} servers for this episode")
        failures: list[str] = []
        for server_url, name, _language in choices:
            try:
                streams.extend(await resolve_server(
                    self._resolver_client, server_url, referer=watch_url, user_agent=user_agent
                ))
            except ProviderError as exc:
                failures.append(f"{name}: {exc}")
        if streams:
            # Keep alternate headers distinct: they can affect whether a URL plays.
            unique: dict[tuple[str, tuple[tuple[str, str], ...]], Stream] = {}
            for stream in streams:
                identity = (stream.url, tuple(sorted((stream.headers or {}).items())))
                unique.setdefault(identity, stream)
            return list(unique.values())
        detail = "; ".join(failures) or "No supported media was returned"
        raise ProviderError(f"Could not resolve HiAnime {language.upper()} playback. {detail}")

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        finally:
            await self._resolver_client.aclose()


@dataclass(frozen=True)
class _AnimeLanguageView:
    """A borrowed provider view; the app owns and closes the shared clients."""

    _provider: AnimeProvider
    language: str
    name = "anime"

    async def search(self, query: str) -> list[Show]:
        return await self._provider.search(query)

    async def episodes(self, show: Show) -> list[Episode]:
        return await self._provider.episodes(show)

    async def stream(self, episode: Episode) -> list[Stream]:
        return await self._provider._stream(episode, language=self.language)
