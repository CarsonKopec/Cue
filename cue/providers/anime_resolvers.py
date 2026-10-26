"""Small, data-only resolvers for the server pages HiAnime actually advertises.

Zoko's public player version 23 publishes a JSON ``window.__P`` envelope using
base64 and the fixed byte mask in https://zokoanime.video/core/obfuscate.js?v=23.
https://zokoanime.video/player.js?v=23 passes the decoded object to
https://zokoanime.video/core/index.js?v=23, which plays its ``src`` as HLS.
This module implements that published serialization, not JavaScript execution,
authentication, DRM, inferred API calls, or the player's edge-host failover.

MegaPlay supports only explicit video/source elements or JSON media sources.
Its observed Error Code 410 page is a removed-content error, not a stream.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from cue.providers.base import ProviderError, Stream, StreamSubtitle

_HOSTS = {"zokoanime.video", "megaplay.buzz"}
_MAX_PAGE_BYTES = 2 * 1024 * 1024
_MAX_REDIRECTS = 3
_ZOKO_MASK = b"otaku-embed-v1"
_MEDIA_EXTENSIONS = {"m3u8", "mpd", "mp4", "m4v", "webm", "mkv", "mov", "avi", "ts"}
_MEDIA_TYPES = {
    "hls", "m3u8", "dash", "mpd", "mp4", "webm",
    "application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl",
    "audio/x-mpegurl", "application/dash+xml", "video/mp4", "video/webm",
    "video/x-matroska", "video/quicktime", "video/mp2t",
}
_CONTAINERS = ("data", "results", "streams", "sources", "items", "list", "playlist")
_SOURCE_KEYS = ("file", "src", "stream_url", "url")
_SUBTITLE_KEYS = ("subtitles", "tracks", "captions")
_SUBTITLE_EXTENSIONS = {"vtt", "srt", "ass", "ssa", "ttml", "dfxp", "sub", "sbv"}
_SUBTITLE_TYPES = {"text/vtt", "application/x-subrip", "application/ttml+xml", "text/srt", "vtt", "srt", "ass", "ssa"}
_PROTECTION_KEYS = {
    "drm", "drm_info", "drminfo", "keysystems", "key_systems", "keysystemoptions",
    "licenseurl", "license_url", "license", "encrypted", "isencrypted", "is_encrypted",
}
_CHALLENGE = re.compile(
    r"(?:<title>\s*(?:just a moment|attention required|access denied)|"
    r"/cdn-cgi/challenge-platform/|cf-chl-|id=[\"']challenge-form|"
    r"class=[\"'][^\"']*(?:g-recaptcha|h-captcha)|verify you are human)",
    re.IGNORECASE,
)


def _http_url(value: Any, base_url: str) -> str | None:
    if not isinstance(value, str) or not value or re.search(r"[\x00-\x20\x7f]", value):
        return None
    try:
        target = urlsplit(urljoin(base_url, value))
        if target.scheme not in ("http", "https") or not target.hostname:
            return None
        if target.username is not None or target.password is not None:
            return None
        _ = target.port  # Reject malformed ports before returning a player URL.
        return urlunsplit(target)
    except ValueError:
        return None


def _media_url(value: Any, base_url: str, media_type: Any = None) -> str | None:
    url = _http_url(value, base_url)
    if url is None:
        return None
    path = urlsplit(url).path.lower()
    extension = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    declared = str(media_type or "").split(";", 1)[0].strip().lower()
    # An explicit HTML/embed declaration never becomes media based on its suffix.
    if declared in {"text/html", "html", "iframe", "embed"}:
        return None
    if extension in _MEDIA_EXTENSIONS or declared in _MEDIA_TYPES:
        return url
    return None


def _quality(row: Mapping[str, Any]) -> str | None:
    value = next((row[key] for key in ("height", "quality", "label", "resolution") if row.get(key) is not None), None)
    text = str(value or "").strip().lower()
    if text in {"4k", "uhd"}:
        return "2160p"
    match = re.fullmatch(r"(?:\d{2,5}\s*x\s*)?(\d{2,4})(?:p)?", text)
    return f"{int(match.group(1))}p" if match else None


def _headers(value: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(value, Mapping):
        return result
    for name, content in value.items():
        if not isinstance(name, str) or not isinstance(content, str):
            continue
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or re.search(r"[\r\n\x00]", content):
            raise ProviderError("Server supplied invalid media HTTP headers")
        result[name] = content
    return result


def _protected(value: Any) -> bool:
    return value not in (None, False, "", "false", "False", 0, [], {})


def _check_protection(row: Mapping[str, Any], depth: int = 0) -> None:
    if depth > 12:
        raise ProviderError("Server media response is nested too deeply")
    for name, value in row.items():
        if str(name).lower() in _PROTECTION_KEYS and _protected(value):
            raise ProviderError("Encrypted or DRM-protected server sources are unsupported")
        if isinstance(value, Mapping):
            _check_protection(value, depth + 1)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    _check_protection(item, depth + 1)


def _request_headers(
    headers: Mapping[str, str], url: str, base_url: str, referrer_policy: bool,
    explicit_referer: bool,
) -> dict[str, str]:
    result = dict(headers)
    if referrer_policy and not explicit_referer and _embed_origin(url) != _embed_origin(base_url):
        result = {key: val for key, val in result.items() if key.lower() != "referer"}
        embed = urlsplit(base_url)
        if not (embed.scheme == "https" and urlsplit(url).scheme == "http"):
            result["Referer"] = urlunsplit((embed.scheme, embed.netloc, "/", "", ""))
    return result


def _subtitle_rows(value: Mapping[str, Any]) -> list[tuple[Any, bool]]:
    rows: list[tuple[Any, bool]] = []
    for key in _SUBTITLE_KEYS:
        items = value.get(key)
        if isinstance(items, (str, Mapping)):
            items = [items]
        if isinstance(items, list):
            rows.extend((item, key != "tracks") for item in items)
    return rows


def _subtitle_tracks(
    rows: list[tuple[Any, bool]], base_url: str, headers: Mapping[str, str],
    referrer_policy: bool, explicit_referer: bool,
) -> list[StreamSubtitle]:
    tracks: list[StreamSubtitle] = []
    for row, subtitle_context in rows:
        if isinstance(row, str):
            row = {"src": row}
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("kind") or "").lower()
        if kind and kind not in {"subtitles", "captions"}:
            continue
        url = next((
            candidate for key in ("src", "file", "url")
            if (candidate := _http_url(row.get(key), base_url)) is not None
        ), None)
        if url is None:
            continue
        path = urlsplit(url).path.lower()
        extension = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
        declared = str(row.get("type") or row.get("mimeType") or "").split(";", 1)[0].strip().lower()
        # Do not turn thumbnails or a media URL into a downloadable subtitle.
        if extension in _MEDIA_EXTENSIONS | {"jpg", "jpeg", "png", "gif", "webp"}:
            continue
        if declared in _MEDIA_TYPES | {"text/html", "html", "iframe", "embed"}:
            continue
        if not (subtitle_context or kind or extension in _SUBTITLE_EXTENSIONS or declared in _SUBTITLE_TYPES):
            continue
        # Media credentials are not subtitle credentials. The caller downloads
        # tracks independently and must not leak media cookies/auth to a CDN.
        merged = {key: val for key, val in headers.items() if key.lower() in {"referer", "user-agent"}}
        own_referer = explicit_referer
        for name, content in _headers(row.get("headers")).items():
            merged = {key: val for key, val in merged.items() if key.lower() != name.lower()}
            merged[name] = content
            own_referer = own_referer or name.lower() == "referer"
        track = StreamSubtitle(
            url=url,
            language=str(row.get("srclang") or row.get("lang") or row.get("language") or "").strip(),
            label=str(row.get("label") or row.get("name") or "").strip(),
            headers=_request_headers(merged, url, base_url, referrer_policy, own_referer) or None,
        )
        if track not in tracks:
            tracks.append(track)
    return tracks


def streams_from_payload(
    payload: Any,
    base_url: str,
    headers: Mapping[str, str] | None,
    language: str | None = None,
    *,
    referrer_policy: bool = False,
) -> list[Stream]:
    """Read explicit media rows in JSON envelopes; never accept arbitrary embeds.

    Relative URLs are resolved against the response URL. An extensionless URL
    must have a recognized media ``type``/MIME declaration. Known sub/dub labels
    are filtered before quality selection; unlabeled rows retain compatibility.
    Subtitle objects are attached as tracks, never traversed as media sources.
    The resolver enables ``referrer_policy`` for browser-derived default headers;
    explicit source Referer headers remain intact.
    """
    output: list[Stream] = []
    seen: dict[tuple[str, tuple[tuple[str, str], ...]], Stream] = {}
    initial_headers = _headers(headers)

    def walk(
        value: Any, inherited: Mapping[str, str], inherited_language: str | None,
        depth: int = 0, explicit_referer: bool = False,
        inherited_tracks: list[tuple[Any, bool]] | None = None,
    ) -> None:
        if depth > 12:
            raise ProviderError("Server media response is nested too deeply")
        if isinstance(value, list):
            for item in value:
                walk(item, inherited, inherited_language, depth + 1, explicit_referer, inherited_tracks)
            return
        if isinstance(value, str):
            value = {"url": value}
        if not isinstance(value, Mapping):
            return
        _check_protection(value)
        declared_language = str(value.get("language") or value.get("audio_type") or inherited_language or "").lower()
        if language and declared_language in {"sub", "dub"} and declared_language != language:
            return
        merged = dict(inherited)
        for name, content in _headers(value.get("headers")).items():
            merged = {key: val for key, val in merged.items() if key.lower() != name.lower()}
            merged[name] = content
            explicit_referer = explicit_referer or name.lower() == "referer"
        track_rows = [*(inherited_tracks or []), *_subtitle_rows(value)]
        media_type = value.get("type") or value.get("mimeType") or value.get("mime_type") or value.get("mime")
        for key in _SOURCE_KEYS:
            url = _media_url(value.get(key), base_url, media_type)
            if url is None:
                continue
            playback_headers = _request_headers(merged, url, base_url, referrer_policy, explicit_referer)
            tracks = _subtitle_tracks(track_rows, base_url, merged, referrer_policy, explicit_referer)
            identity = (url, tuple(sorted(playback_headers.items())))
            if identity not in seen:
                stream = Stream(url=url, headers=playback_headers or None, quality=_quality(value), subtitle_tracks=tracks or None)
                seen[identity] = stream
                output.append(stream)
            elif tracks:
                stream = seen[identity]
                stream.subtitle_tracks = list(stream.subtitle_tracks or [])
                stream.subtitle_tracks.extend(track for track in tracks if track not in stream.subtitle_tracks)
            break
        for key in _CONTAINERS:
            if key in value:
                walk(value[key], merged, declared_language or None, depth + 1, explicit_referer, track_rows)
        for key in ("sub", "dub"):
            if key in value and (language is None or language == key):
                walk(value[key], merged, key, depth + 1, explicit_referer, track_rows)

    walk(payload, initial_headers, None)
    return output


class _MediaHTML(HTMLParser):
    """Collect only explicit media tags and scripts that contain JSON data."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self.scripts: list[str] = []
        self._script: list[str] | None = None
        self._videos: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag == "video":
            video: dict[str, Any] = {**attributes, "sources": [], "tracks": []}
            self._videos.append(video)
            self.rows.append(video)
        elif tag == "source" and self._videos:
            self._videos[-1]["sources"].append(attributes)
        elif tag == "track" and self._videos:
            self._videos[-1]["tracks"].append({"kind": "subtitles", **attributes})
        if tag == "script" and not attributes.get("src"):
            self._script = []

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "video":
            if self._videos:
                self._videos.pop()
        if tag == "script" and self._script is not None:
            self.scripts.append("".join(self._script))
            self._script = None


def _embed_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname or "", parsed.port or (443 if parsed.scheme == "https" else 80)


async def _fetch_page(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> httpx.Response:
    origin = _embed_origin(url)
    for redirect in range(_MAX_REDIRECTS + 1):
        try:
            # Construct a fresh Request: catalog credentials must not be inherited
            # from a caller's default headers or cookies on a third-party embed.
            request = httpx.Request("GET", url, headers=headers)
            response = await client.send(request, follow_redirects=False, stream=True)
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = _http_url(response.headers.get("location"), url)
                    if target is None or _embed_origin(target) != origin:
                        raise ProviderError("Server redirect leaves its permitted origin")
                    if redirect == _MAX_REDIRECTS:
                        raise ProviderError("Server exceeded the redirect limit")
                    url = target
                    continue
                if response.status_code in {404, 410}:
                    raise ProviderError(f"Server content is unavailable or removed (HTTP {response.status_code})")
                if response.status_code in {401, 403, 429}:
                    raise ProviderError(f"Server access is restricted (HTTP {response.status_code}); no bypass attempted")
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > _MAX_PAGE_BYTES:
                        raise ProviderError("Server page exceeds the supported size limit")
                # aiter_bytes has already decompressed the body. Do not ask the
                # replacement Response to decompress it a second time.
                response_headers = {
                    name: value for name, value in response.headers.items()
                    if name.lower() not in {"content-encoding", "content-length"}
                }
                return httpx.Response(response.status_code, headers=response_headers, content=bytes(body), request=request)
            finally:
                await response.aclose()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"Server returned HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not load server page: {type(exc).__name__}") from exc
    raise ProviderError("Server exceeded the redirect limit")


def _zoko_payload(scripts: list[str]) -> Any | None:
    for script in scripts:
        match = re.search(r"\bwindow\.__P\s*=\s*([\"'])([A-Za-z0-9+/=]+)\1\s*;?", script)
        if match is None:
            continue
        try:
            encoded = base64.b64decode(match.group(2), validate=True)
            decoded = bytes(value ^ _ZOKO_MASK[index % len(_ZOKO_MASK)] for index, value in enumerate(encoded))
            payload = json.loads(decoded.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise ProviderError("ZokoAnime's published player payload format has changed or is invalid") from exc
        if not isinstance(payload, dict):
            raise ProviderError("ZokoAnime returned an unsupported player payload")
        return payload
    return None


def _json_scripts(scripts: list[str]) -> list[Any]:
    output: list[Any] = []
    decoder = json.JSONDecoder()
    for script in scripts:
        try:
            output.append(json.loads(script))
            continue
        except ValueError:
            pass
        if re.search(
            r"[\"']?\b(?:drm|keySystems|key_systems|licenseUrl|license_url|encrypted)[\"']?\s*:",
            script,
            re.IGNORECASE,
        ):
            raise ProviderError("Encrypted or DRM-protected JavaScript player configuration is unsupported")
        # A JSON array/object assigned to sources is data, not executable JS.
        # Single-quoted or computed JS expressions are deliberately unsupported.
        for match in re.finditer(r"\bsources\s*[:=]\s*(?=[\[{])", script):
            try:
                value, _ = decoder.raw_decode(script[match.end():])
                output.append({"sources": value})
            except ValueError:
                continue
    return output


async def resolve_server(
    client: httpx.AsyncClient,
    url: str,
    *,
    referer: str,
    user_agent: str,
) -> list[Stream]:
    """Resolve an allowlisted embed through its normal public source data."""
    valid_url = _http_url(url, url)
    if valid_url is None or urlsplit(valid_url).hostname not in _HOSTS:
        raise ProviderError("Unsupported HiAnime server host")
    parsed = urlsplit(valid_url)
    if parsed.port not in (None, 80, 443):
        raise ProviderError("Unsupported HiAnime server port")
    request_headers = _headers({"Referer": referer, "User-Agent": user_agent})
    request_headers["Accept"] = "text/html,application/json"
    response = await _fetch_page(client, valid_url, request_headers)
    text = response.text
    if response.headers.get("cf-mitigated", "").lower() == "challenge" or _CHALLENGE.search(text):
        raise ProviderError("Server requires a browser challenge; no bypass attempted")
    if re.search(r"Error\s+Code\s*:\s*(?:<[^>]*>\s*)*410\b", text, re.IGNORECASE):
        raise ProviderError("Server content was removed (Error Code 410)")
    response_url = str(response.url)
    media_headers = {"Referer": response_url, "User-Agent": user_agent}
    streams: list[Stream] = []
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/json" or text.lstrip().startswith(("{", "[")):
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("Server returned invalid JSON") from exc
        streams = streams_from_payload(payload, response_url, media_headers, referrer_policy=True)
    else:
        parser = _MediaHTML()
        parser.feed(text)
        if parsed.hostname == "zokoanime.video":
            payload = _zoko_payload(parser.scripts)
            if payload is not None:
                streams = streams_from_payload(payload, response_url, media_headers, referrer_policy=True)
        if not streams:
            streams = streams_from_payload(parser.rows, response_url, media_headers, referrer_policy=True)
            for payload in _json_scripts(parser.scripts):
                streams.extend(streams_from_payload(payload, response_url, media_headers, referrer_policy=True))
    if not streams:
        raise ProviderError("Server has no supported explicit media source; its player format may have changed")
    return streams
