"""Download a stream's published subtitle files for local player loading."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from cue.providers.base import Stream
from cue.subtitles.base import SubtitleError, SubtitleTrack

MAX_SUBTITLE_BYTES = 8 * 1024 * 1024
_EXTENSIONS = (".vtt", ".srt", ".ass")
_TIMING = re.compile(r"^[ \t]*(?:\d{1,3}:)?\d{2}:\d{2}[.,]\d{3}[ \t]*-->[ \t]*(?:\d{1,3}:)?\d{2}:\d{2}[.,]\d{3}", re.M)
_LANGUAGES = {
    "english": "en", "eng": "en", "japanese": "ja", "jpn": "ja", "jap": "ja",
    "arabic": "ar", "ara": "ar", "french": "fr", "fra": "fr", "fre": "fr",
    "german": "de", "deu": "de", "ger": "de", "spanish": "es", "spa": "es",
    "portuguese": "pt", "por": "pt", "italian": "it", "ita": "it",
    "russian": "ru", "rus": "ru", "korean": "ko", "kor": "ko",
    "chinese": "zh", "zho": "zh", "chi": "zh", "mandarin": "zh",
    "polish": "pl", "pol": "pl", "turkish": "tr", "tur": "tr",
    "indonesian": "id", "ind": "id", "vietnamese": "vi", "vie": "vi",
    "thai": "th", "tha": "th", "hindi": "hi", "hin": "hi",
    "dutch": "nl", "nld": "nl", "dut": "nl", "swedish": "sv", "swe": "sv",
    "norwegian": "no", "nor": "no", "danish": "da", "dan": "da",
    "finnish": "fi", "fin": "fi", "czech": "cs", "ces": "cs", "cze": "cs",
    "greek": "el", "ell": "el", "gre": "el", "hebrew": "he", "heb": "he",
    "romanian": "ro", "ron": "ro", "rum": "ro", "hungarian": "hu", "hun": "hu",
    "ukrainian": "uk", "ukr": "uk", "malay": "ms", "msa": "ms", "may": "ms",
    "filipino": "fil", "fil": "fil", "tagalog": "tl", "tgl": "tl",
}
_ISO_LANGUAGES = frozenset("aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik in io is it iu iw ja ji jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mo mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu".split())


def _base_language(value: str) -> str:
    return _LANGUAGES.get(value, value if value in _ISO_LANGUAGES else "")


def language_code(value: str | None) -> str:
    """Normalize known language labels/codes, retaining useful regional variants."""
    value = str(value or "").strip().casefold()
    if not value:
        return ""
    explicit = re.fullmatch(r"([a-z]{2,3})(?:[-_]([a-z]{2}|\d{3}))?", value)
    if explicit:
        primary = _base_language(explicit[1])
        return primary + ("-" + explicit[2] if explicit[2] else "") if primary else ""
    if value.startswith(("brazilian portuguese", "portuguese brazilian", "portuguese (brazil", "portuguese - brazil")):
        return "pt-br"
    if value.startswith(("spanish lat", "spanish (lat", "latin american spanish")):
        return "es-419"
    if value.startswith(("spanish esp", "spanish (spain", "spanish - spain")):
        return "es-es"
    first = re.split(r"[\s(_-]", value, maxsplit=1)[0]
    return _base_language(first)


def _url(value: str) -> httpx.URL:
    try:
        parsed = httpx.URL(value)
        if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.userinfo:
            raise ValueError("not a public HTTP URL")
        # Validate the port before allowing the request to be constructed.
        _ = parsed.port
        return parsed.copy_with(fragment=None)
    except (httpx.InvalidURL, ValueError) as exc:
        raise SubtitleError("The provider returned an invalid subtitle URL") from exc


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    return (url.scheme, url.host, url.port or (443 if url.scheme == "https" else 80))


def _playlist_segment(data: bytes, base: str) -> str:
    """The single WebVTT file behind an HLS subtitle rendition's media playlist."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise SubtitleError("The provider returned an unreadable subtitle playlist") from exc
    lines = [line.strip() for line in text.splitlines()]
    segments = [line for line in lines if line and not line.startswith("#")]
    # Only a complete playlist with one file can be loaded as one subtitle file;
    # live or segmented renditions remain available in the player's track menu.
    if "#EXT-X-ENDLIST" not in lines or len(segments) != 1:
        raise SubtitleError("The server's subtitles are split into segments, which Cue cannot load")
    try:
        return str(_url(str(httpx.URL(base).join(segments[0]))))
    except httpx.InvalidURL as exc:
        raise SubtitleError("The provider returned an invalid subtitle URL") from exc


def _content(data: bytes) -> tuple[str, bytes]:
    if not data or len(data) > MAX_SUBTITLE_BYTES:
        raise SubtitleError("The subtitle file is empty or too large")
    try:
        text = data.decode("utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    except UnicodeError as exc:
        raise SubtitleError("The provider returned an unreadable subtitle file") from exc
    text = text.strip()
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        raise SubtitleError("The provider returned a binary subtitle file")
    if re.search(r"<!doctype\s+html|<(?:html|head|body|script|form)(?:\s|>)", text[:2048], re.I):
        raise SubtitleError("The provider returned a web page instead of subtitles")
    if text.startswith("WEBVTT") and _TIMING.search(text):
        extension = ".vtt"
    elif "[Script Info]" in text and "[Events]" in text and re.search(r"^Dialogue:\s*\d+,\d+:\d{2}:\d{2}\.\d{2},", text, re.M):
        extension = ".ass"
    elif _TIMING.search(text):
        extension = ".srt"
    else:
        raise SubtitleError("The provider did not return supported subtitle cues")
    normalized = (text + "\n").encode("utf-8")
    if len(normalized) > MAX_SUBTITLE_BYTES:
        raise SubtitleError("The subtitle file is too large")
    return extension, normalized


class ProviderSubtitles:
    """A separate download path that needs no OpenSubtitles account or API key."""

    def __init__(self, client: httpx.AsyncClient, directory: Path) -> None:
        self._client = client
        self._directory = Path(directory)
        self._registered: dict[str, tuple[str, dict[str, str]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def choices(self, stream: Stream) -> list[SubtitleTrack]:
        fallback = {
            key: value for key, value in (stream.headers or {}).items()
            if key.casefold() in {"referer", "user-agent"}
        }
        candidates = [
            (track.url, track.language, track.label, track.headers if track.headers is not None else fallback)
            for track in (getattr(stream, "subtitle_tracks", None) or [])
        ]
        candidates.extend((url, "", "", fallback) for url in (stream.subtitles or []) if isinstance(url, str))
        result: list[SubtitleTrack] = []
        seen: set[str] = set()
        for url, language, label, supplied_headers in candidates:
            try:
                url = str(_url(url))
                headers = dict(httpx.Headers(supplied_headers))
                if any(not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", key) or "\r" in value or "\n" in value for key, value in headers.items()):
                    continue
            except (SubtitleError, TypeError, ValueError, UnicodeError):
                continue
            identity = hashlib.sha256(json.dumps([url, sorted(headers.items())], ensure_ascii=True).encode()).hexdigest()
            if identity in seen:
                continue
            seen.add(identity)
            # Filename language hints are useful only when the server supplied no label.
            hint = ""
            if not label and not language:
                filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
                for token in re.split(r"[._\s-]+", filename.rsplit(".", 1)[0]):
                    hint = language_code(token)
                    if hint:
                        break
            # Some servers incorrectly mark every track as "en"; their visible
            # language labels are the more reliable choice in that case.
            label_language = language_code(label)
            metadata_language = language_code(language)
            code = label_language or metadata_language or hint
            if label_language and "-" not in label_language and metadata_language.startswith(label_language + "-"):
                code = metadata_language
            self._registered[identity] = (url, headers)
            result.append(SubtitleTrack(language=code, label=label or language or hint or "Provider subtitles", url=url, source="provider", id=identity))
        return result

    async def fetch(self, track: SubtitleTrack) -> str:
        registered = self._registered.get(track.id)
        if track.source != "provider" or not registered or track.url != registered[0]:
            raise SubtitleError("Choose a subtitle track from the current stream first")
        async with self._locks.setdefault(track.id, asyncio.Lock()):
            for extension in _EXTENSIONS:
                cached = self._directory / f"{track.id}{extension}"
                try:
                    if cached.is_file() and cached.stat().st_size <= MAX_SUBTITLE_BYTES:
                        detected, _ = _content(cached.read_bytes())
                        if detected == extension:
                            track.path = str(cached)
                            return track.path
                except (OSError, SubtitleError):
                    pass
            try:
                async with asyncio.timeout(30):
                    data = await self._download(*registered)
                    if data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"#EXTM3U"):
                        url, headers = registered
                        data = await self._download(_playlist_segment(data, url), headers)
                extension, data = _content(data)
                target = self._directory / f"{track.id}{extension}"
                self._directory.mkdir(parents=True, exist_ok=True)
                temporary = ""
                try:
                    with tempfile.NamedTemporaryFile(dir=self._directory, prefix=f".{track.id}.", suffix=".tmp", delete=False) as output:
                        temporary = output.name
                        output.write(data)
                    os.replace(temporary, target)
                finally:
                    if temporary and os.path.exists(temporary):
                        os.unlink(temporary)
            except TimeoutError as exc:
                raise SubtitleError("The provider subtitle download timed out") from exc
            except httpx.HTTPError as exc:
                raise SubtitleError("The provider subtitle download failed") from exc
            except OSError as exc:
                raise SubtitleError("Cannot save the provider subtitle file") from exc
            track.path = str(target)
            return track.path

    async def _download(self, url: str, headers: dict[str, str]) -> bytes:
        current = _url(url)
        for attempt in range(4):
            # Construct a fresh request: never merge the shared client's credentials,
            # default headers, or cookies into an unrelated subtitle host's request.
            request = httpx.Request("GET", current, headers=headers, extensions={"timeout": httpx.Timeout(15).as_dict()})
            response = await self._client.send(request, auth=None, follow_redirects=False, stream=True)
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location or attempt == 3:
                        raise SubtitleError("The subtitle server redirected too many times")
                    try:
                        next_url = _url(str(current.join(location)))
                    except (httpx.InvalidURL, ValueError) as exc:
                        raise SubtitleError("The subtitle server returned an invalid redirect") from exc
                    if _origin(next_url) != _origin(current):
                        raise SubtitleError("The subtitle server redirected to another host")
                    current = next_url
                    continue
                if response.status_code in {401, 403, 429}:
                    raise SubtitleError(f"Subtitle access was refused ({response.status_code})")
                if response.status_code != 200:
                    raise SubtitleError(f"Subtitle download failed ({response.status_code})")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type in {"text/html", "application/xhtml+xml", "application/json"}:
                    raise SubtitleError("The provider returned a web page or error instead of subtitles")
                try:
                    length = int(response.headers.get("content-length", "0"))
                except ValueError:
                    length = 0
                if length > MAX_SUBTITLE_BYTES:
                    raise SubtitleError("The subtitle file is too large")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_SUBTITLE_BYTES:
                        raise SubtitleError("The subtitle file is too large")
                return bytes(data)
            finally:
                await response.aclose()
        raise SubtitleError("The subtitle server redirected too many times")
