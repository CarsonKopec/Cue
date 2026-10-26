"""HydraHD's published play-button request and advertised server discovery.

Only literal data in the observed movie/TV scripts is read. Remote JavaScript
is never executed, and the site's web-player auto-next is left to Cue's queue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx

from cue.providers.anime_resolvers import (
    _MediaHTML, _http_url, _json_scripts, streams_from_payload,
)
from cue.providers.base import ProviderError, Stream
from cue.providers.hydra_servers import SUPPORTED_HOSTS, resolve_server

_MAX_BYTES = 2 * 1024 * 1024
_BLOCK_PAGE = re.compile(
    r'<title>\s*(?:just a moment|attention required|access denied)|'
    r'id=["\']challenge-form|(?:class|id)=["\'][^"\']*(?:g-recaptcha|h-captcha)',
    re.I,
)


@dataclass(frozen=True)
class ServerChoice:
    url: str
    name: str


class _ServerList(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, list[str]]] = []
        self._active: list[str] | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if self._active is not None and tag == "div":
            self._depth += 1
        if "iframe-server-button" in classes and self._active is None:
            # Premium/login controls are not public playback choices.
            if "premium" in classes or values.get("data-premium") in {"1", "true"}:
                return
            self._active = []
            self._depth = 1
            self.rows.append((values.get("data-link") or "", self._active))

    def handle_data(self, data: str) -> None:
        if self._active is not None and data.strip():
            self._active.append(data.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._active is not None:
            self._depth -= 1
            if self._depth == 0:
                self._active = None


def server_choices(html: str, base_url: str) -> list[ServerChoice]:
    parser = _ServerList()
    parser.feed(html)
    result: list[ServerChoice] = []
    seen: set[str] = set()
    for raw, text in parser.rows:
        url = _http_url(raw.strip(), base_url)
        if url is None or url in seen:
            continue
        if urlsplit(url).port not in (None, 80, 443):
            continue
        seen.add(url)
        name = text[0] if text else urlsplit(url).hostname or "server"
        result.append(ServerChoice(url, name))
    return result


def _ajax_request(html: str, page_url: str) -> str | None:
    parser = _MediaHTML()
    parser.feed(html)
    for script in parser.scripts:
        if "ajaxlink_mov0" in script:
            path = "/ajax/mov_0.php"
            call = re.search(r"(?:\$|jQuery)\s*\.\s*get\s*\(\s*(['\"])/ajax/mov_0\.php\1\s*,\s*", script)
        elif "ajaxlink_tv0" in script:
            path = "/ajax/tv_0.php"
            # The browser's normal, non-auto-next branch is explicitly present.
            declaration = re.search(
                r"\b(?:let|const|var)\s+url\s*=\s*isAutoNextEnabled\s*\?\s*"
                r"['\"]/ajax/auto_tv\.php['\"]\s*:\s*['\"]/ajax/tv_0\.php['\"]", script,
            )
            call = re.search(r"(?:\$|jQuery)\s*\.\s*get\s*\(\s*url\s*,\s*", script) if declaration else None
        else:
            continue
        if call is None:
            continue
        try:
            params, _ = json.JSONDecoder().raw_decode(script[call.end():])
        except ValueError as exc:
            raise ProviderError("HydraHD's player request data has changed") from exc
        needed = {"i", "t"} if path.endswith("mov_0.php") else {"i", "t", "s", "e"}
        if not isinstance(params, dict) or set(params) != needed:
            raise ProviderError("HydraHD supplied unsupported player request parameters")
        if not all(isinstance(value, str) for value in params.values()):
            raise ProviderError("HydraHD supplied invalid player identifiers")
        if not re.fullmatch(r"tt\d+|", params["i"]) or not params["t"].isdigit():
            raise ProviderError("HydraHD supplied invalid movie/series identifiers")
        if any(not params[key].isdigit() for key in ("s", "e") if key in params):
            raise ProviderError("HydraHD supplied invalid season/episode numbers")
        return urljoin(page_url, path) + "?" + urlencode(params)
    return None


async def _fetch_ajax(client: httpx.AsyncClient, url: str, referer: str, user_agent: str) -> httpx.Response:
    original = urlsplit(url)
    origin = (original.scheme, original.hostname, original.port or (443 if original.scheme == "https" else 80))
    for attempt in range(4):
        try:
            request = httpx.Request("GET", url, headers={
                "User-Agent": user_agent, "Referer": referer,
                "X-Requested-With": "XMLHttpRequest", "Accept": "*/*",
            })
            response = await client.send(request, auth=None, follow_redirects=False, stream=True)
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = _http_url(response.headers.get("location"), url)
                    parsed = urlsplit(target) if target else None
                    if parsed is None or (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)) != origin:
                        raise ProviderError("HydraHD player request redirected outside its origin")
                    if attempt == 3:
                        raise ProviderError("HydraHD player request exceeded its redirect limit")
                    url = target
                    continue
                if response.status_code in {401, 403, 429}:
                    raise ProviderError(f"HydraHD player access was refused (HTTP {response.status_code})")
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > _MAX_BYTES:
                        raise ProviderError("HydraHD player response is too large")
                clean_headers = {k: v for k, v in response.headers.items() if k.lower() not in {"content-encoding", "content-length"}}
                result = httpx.Response(response.status_code, content=bytes(body), headers=clean_headers, request=request)
                if result.headers.get("cf-mitigated", "").lower() == "challenge" or _BLOCK_PAGE.search(result.text):
                    raise ProviderError("HydraHD requires a browser challenge; no bypass attempted")
                return result
            finally:
                await response.aclose()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"HydraHD player returned HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not load HydraHD player: {type(exc).__name__}") from exc
    raise ProviderError("HydraHD player request exceeded its redirect limit")


def _direct_streams(html: str, page_url: str, user_agent: str) -> list[Stream]:
    parser = _MediaHTML()
    parser.feed(html)
    payloads = [*parser.rows, *_json_scripts(parser.scripts)]
    if html.lstrip().startswith(("{", "[")):
        try:
            payloads.append(json.loads(html))
        except ValueError:
            pass
    return streams_from_payload(payloads, page_url, {"Referer": page_url, "User-Agent": user_agent}, referrer_policy=True)


async def resolve_page(client: httpx.AsyncClient, page_url: str, html: str, *, user_agent: str) -> list[Stream]:
    """Resolve a movie/episode page through its normal public server menu."""
    if _BLOCK_PAGE.search(html):
        raise ProviderError("HydraHD requires a browser challenge; no bypass attempted")
    direct = _direct_streams(html, page_url, user_agent)
    if direct:
        return direct
    choices = server_choices(html, page_url)
    if not choices:
        ajax = _ajax_request(html, page_url)
        if ajax is None:
            raise ProviderError("HydraHD's page has no supported player configuration")
        response = await _fetch_ajax(client, ajax, page_url, user_agent)
        page_url, html = str(response.url), response.text
        direct = _direct_streams(html, page_url, user_agent)
        if direct:
            return direct
        choices = server_choices(html, page_url)
    supported = [choice for choice in choices if urlsplit(choice.url).hostname in SUPPORTED_HOSTS]
    if not supported:
        raise ProviderError("HydraHD advertised no supported public playback servers")
    parts = urlsplit(page_url)
    referer = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))  # iframe referrerpolicy=origin
    failures = []
    # Stop at the first successful advertised source; avoid contacting unrelated
    # ads, premium controls, or every third-party server on a large menu.
    for choice in supported[:6]:
        try:
            streams = await resolve_server(client, choice.url, referer=referer, user_agent=user_agent)
            if streams:
                return streams
        except ProviderError as exc:
            failures.append(f"{choice.name}: {exc}")
    raise ProviderError("HydraHD playback could not be resolved. " + "; ".join(failures))
