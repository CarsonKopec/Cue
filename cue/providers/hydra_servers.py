"""Data-only adapters for HydraHD's supported external players.

VixSrc's public movie/TV bundles request /api/<page path>; its returned iframe
publishes window.masterPlaylist. The linked vixsrc skin appends its nonempty
params, optional cdn, h=1 and lang. This implements that published flow without
executing JavaScript, inventing tokens, or solving browser challenges.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from cue.providers.anime_resolvers import (
    _CHALLENGE, _MediaHTML, _check_protection, _embed_origin, _fetch_page,
    _headers, _http_url, _json_scripts, streams_from_payload,
)
from cue.providers.base import ProviderError, Stream, StreamSubtitle

SUPPORTED_HOSTS = {"vixsrc.to"}
_PAGE_PATH = re.compile(r"/(?:movie/[1-9][0-9]*|tv/[1-9][0-9]*/[1-9][0-9]*/[1-9][0-9]*)/?")
_STRING = re.compile(r'''"(?:[^"\\\r\n]|\\(?:["'\\/bfnrt]|u[0-9a-fA-F]{4}))*"|'(?:[^'\\\r\n]|\\(?:["'\\/bfnrt]|u[0-9a-fA-F]{4}))*' ''', re.VERBOSE)
_MEDIA_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^",]*)')
_LOG = logging.getLogger(__name__)


class _LiteralObject:
    """Read only nested string-valued object literals, never JS expressions."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.pos = 0

    def _space(self) -> None:
        while self.pos < len(self.source) and self.source[self.pos].isspace():
            self.pos += 1

    def _take(self, value: str) -> bool:
        self._space()
        if self.source.startswith(value, self.pos):
            self.pos += len(value)
            return True
        return False

    def _string(self) -> str:
        self._space()
        match = _STRING.match(self.source, self.pos)
        if match is None:
            raise ValueError("Expected a literal string")
        self.pos = match.end()
        # literal_eval accepts string data only; the token regex excludes other
        # Python/JavaScript constructs and nonstandard escape forms.
        value = ast.literal_eval(match.group())
        if not isinstance(value, str):
            raise ValueError("Expected a string")
        return value.replace("\\/", "/")

    def object(self, depth: int = 0) -> dict[str, Any]:
        if depth > 4 or not self._take("{"):
            raise ValueError("Unsupported player object")
        result: dict[str, Any] = {}
        while not self._take("}"):
            self._space()
            if self.source[self.pos:self.pos + 1] in {"'", '"'}:
                key = self._string()
            else:
                match = re.match(r"[A-Za-z_$][A-Za-z0-9_$]*", self.source[self.pos:])
                if match is None:
                    raise ValueError("Invalid object key")
                key = match.group()
                self.pos += len(key)
            if key in result or not self._take(":"):
                raise ValueError("Invalid object entry")
            self._space()
            value = self.object(depth + 1) if self.source[self.pos:self.pos + 1] == "{" else self._string()
            result[key] = value
            if self._take("}"):
                return result
            if not self._take(","):
                raise ValueError("Expected object delimiter")
        return result


async def _page(client: httpx.AsyncClient, url: str, referer: str, user_agent: str) -> httpx.Response:
    response = await _fetch_page(client, url, _headers({
        "Referer": referer, "User-Agent": user_agent, "Accept": "text/html,application/json",
    }))
    if response.headers.get("cf-mitigated", "").lower() == "challenge" or _CHALLENGE.search(response.text):
        raise ProviderError("Server requires a browser challenge; no bypass attempted")
    return response


def _explicit(response: httpx.Response, user_agent: str) -> list[Stream]:
    """Preserve normal source/track support if the server changes to plain JSON."""
    base = str(response.url)
    headers = {"Referer": base, "User-Agent": user_agent}
    if response.headers.get("content-type", "").split(";", 1)[0].lower() == "application/json" or response.text.lstrip().startswith(("{", "[")):
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("Server returned invalid JSON") from exc
        return streams_from_payload(payload, base, headers, referrer_policy=True)
    parser = _MediaHTML()
    parser.feed(response.text)
    result = streams_from_payload(parser.rows, base, headers, referrer_policy=True)
    for payload in _json_scripts(parser.scripts):
        result.extend(streams_from_payload(payload, base, headers, referrer_policy=True))
    return result


def _playlist(response: httpx.Response, user_agent: str) -> list[Stream]:
    parser = _MediaHTML()
    parser.feed(response.text)
    configs: list[dict[str, Any]] = []
    for script in parser.scripts:
        match = re.search(r"\bwindow\.masterPlaylist\s*=\s*", script)
        if match is None:
            continue
        try:
            reader = _LiteralObject(script[match.end():])
            config = reader.object()
            tail = reader.source[reader.pos:]
            # A literal followed by an expression is computed code, not data.
            if tail and not re.match(r"\s*(?:;|$|\r?\n\s*window\.)", tail):
                raise ValueError("Computed player configuration")
            configs.append(config)
        except (ValueError, SyntaxError) as exc:
            raise ProviderError("VixSrc published player configuration is not supported literal data") from exc
    if len(configs) != 1:
        raise ProviderError("VixSrc has no supported public playlist configuration")
    config = configs[0]
    _check_protection(config)
    playlist = _http_url(config.get("url"), str(response.url))
    if playlist is None or _embed_origin(playlist) != _embed_origin(str(response.url)) or not re.fullmatch(r"/playlist/[1-9][0-9]*/?", urlsplit(playlist).path):
        raise ProviderError("VixSrc playlist leaves its supported origin or path")
    params = config.get("params")
    if not isinstance(params, dict) or not all(isinstance(v, str) for v in params.values()):
        raise ProviderError("VixSrc playlist parameters are invalid")
    parsed = urlsplit(playlist)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((key, value) for key, value in params.items() if value)
    iframe_query = dict(parse_qsl(urlsplit(str(response.url)).query))
    if iframe_query.get("cdn"):
        query.append(("cdn", iframe_query["cdn"]))
    query.extend((("h", "1"), ("lang", iframe_query.get("lang") or "en")))
    try:
        media = urlunsplit(parsed._replace(query=urlencode(query)))
    except UnicodeError as exc:
        raise ProviderError("VixSrc playlist parameters contain invalid text") from exc
    return streams_from_payload(
        {"file": media, "type": "hls"}, str(response.url),
        {"Referer": str(response.url), "User-Agent": user_agent}, referrer_policy=True,
    )


def _hls_subtitles(playlist: str, base_url: str) -> list[StreamSubtitle]:
    """Read the subtitle renditions an HLS master playlist advertises."""
    tracks = []
    for line in playlist.splitlines():
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attributes = {key: value.strip('"') for key, value in _MEDIA_ATTRIBUTE.findall(line[len("#EXT-X-MEDIA:"):])}
        url = _http_url(attributes.get("URI"), base_url)
        if attributes.get("TYPE") == "SUBTITLES" and url is not None:
            tracks.append(StreamSubtitle(url, language=attributes.get("LANGUAGE", ""), label=attributes.get("NAME", "")))

    # Automatic loading takes the first track in a preferred language, so full
    # subtitles go ahead of captions and forced (foreign-dialogue-only) tracks.
    def rank(track: StreamSubtitle) -> tuple[bool, bool]:
        text = f"{track.label} {track.language}"
        return bool(re.search(r"forced", text, re.I)), bool(re.search(r"\b(?:cc|sdh)\b", text, re.I))

    return sorted(tracks, key=rank)


async def _add_subtitles(client: httpx.AsyncClient, stream: Stream) -> None:
    """Expose the master playlist's subtitles to Cue's subtitle loading and picker.

    VixSrc marks every rendition DEFAULT=NO, so VLC shows none of them unless Cue
    loads one. Subtitles are optional: a failed read leaves playback unchanged.
    """
    try:
        response = await _fetch_page(client, stream.url, dict(stream.headers or {}))
    except ProviderError as exc:
        _LOG.warning("Could not read VixSrc subtitle renditions: %s", exc)
        return
    playlist = response.text.lstrip("﻿")
    if playlist.startswith("#EXTM3U"):
        stream.subtitle_tracks = _hls_subtitles(playlist, str(response.url)) or None


async def resolve_server(client: httpx.AsyncClient, url: str, *, referer: str, user_agent: str) -> list[Stream]:
    """Resolve VixSrc using only ordinary public page/API requests."""
    valid = _http_url(url, url)
    if valid is None:
        raise ProviderError("Invalid HydraHD server URL")
    parsed = urlsplit(valid)
    if parsed.scheme != "https" or parsed.hostname not in SUPPORTED_HOSTS or parsed.port not in (None, 443):
        raise ProviderError("Unsupported HydraHD server host or port")
    if not _PAGE_PATH.fullmatch(parsed.path):
        raise ProviderError("Unsupported VixSrc movie or episode URL")
    outer = await _page(client, valid, referer, user_agent)
    direct = _explicit(outer, user_agent)
    if direct:
        return direct
    # These routes are published by the movie and TV page bundles, including
    # the query string. They do not require authentication or token generation.
    api = urlunsplit(parsed._replace(path="/api" + parsed.path.rstrip("/"), fragment=""))
    data = await _page(client, api, str(outer.url), user_agent)
    try:
        payload = data.json()
    except ValueError as exc:
        raise ProviderError("VixSrc returned invalid embed JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderError("VixSrc returned an unsupported embed response")
    _check_protection(payload)
    embed = _http_url(payload.get("src"), str(data.url))
    if embed is None or _embed_origin(embed) != _embed_origin(valid) or not re.fullmatch(r"/embed/[1-9][0-9]*/?", urlsplit(embed).path):
        raise ProviderError("VixSrc embed leaves its supported origin or path")
    inner = await _page(client, embed, "https://vixsrc.to/", user_agent)
    direct = _explicit(inner, user_agent)
    if direct:
        return direct
    streams = _playlist(inner, user_agent)
    for stream in streams:
        await _add_subtitles(client, stream)
    return streams
