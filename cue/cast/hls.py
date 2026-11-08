"""HLS playlist rewriting for the cast relay and the local relay (cue.player.relay).

Every URI in a playlist is replaced with a relay URL, so the TV's player fetches
playlists, segments and keys through Cue, which adds the headers the source needs.
Media playlists also yield each segment's start time, which Cue uses to follow
playback progress on the TV.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin

# (absolute upstream URL, kind, segment start in seconds or None) -> relay URL.
# Kinds: "playlist" (a media playlist), "subtitles" (a subtitle media playlist),
# "segment" (media data, keys and init sections).
Register = Callable[[str, str, float | None], str]

_URI = re.compile(r'URI="([^"]*)"')
_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^",]*)')
_SRT_TIME = re.compile(r"(\d{1,2}:\d{2}:\d{2}),(\d{3})")
_VTT_TIME = re.compile(r"(?:(\d{1,3}):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d{1,3}):)?(\d{2}):(\d{2})[.,](\d{3})")
SUBTITLE_GROUP = "cue-subs"


@dataclass(frozen=True)
class Rendition:
    """A subtitle track Cue adds to a master playlist."""

    name: str
    language: str
    url: str
    default: bool = False


def is_playlist(data: bytes | str) -> bool:
    head = data[:64].decode("utf-8", "replace") if isinstance(data, bytes) else data[:64]
    return head.lstrip("﻿ \t\r\n").startswith("#EXTM3U")


def is_master(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def attributes(tag: str) -> dict[str, str]:
    """A tag's attribute list, ``#EXT-X-KEY:METHOD=AES-128,URI="..."`` -> {"METHOD": ..., "URI": ...}."""
    return {key: value.strip('"') for key, value in _ATTRIBUTE.findall(tag.partition(":")[2])}


_attributes = attributes


def _quote(value: str) -> str:
    return value.replace('"', "'").replace("\r", " ").replace("\n", " ")


def _media_line(rendition: Rendition) -> str:
    flag = "YES" if rendition.default else "NO"
    return (
        f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="{SUBTITLE_GROUP}",NAME="{_quote(rendition.name)}",'
        f'DEFAULT={flag},AUTOSELECT={flag},FORCED=NO,LANGUAGE="{_quote(rendition.language)}",'
        f'URI="{rendition.url}"'
    )


def rewrite_master(text: str, base_url: str, register: Register, renditions: Sequence[Rendition] = ()) -> str:
    """Relay every URI. With ``renditions``, they replace the source's subtitles."""
    lines = text.lstrip("﻿").splitlines()
    output: list[str] = []
    variant_next = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if stripped.startswith("#EXT-X-MEDIA:"):
                is_subtitles = _attributes(stripped).get("TYPE") == "SUBTITLES"
                if is_subtitles and renditions:
                    continue
                kind = "subtitles" if is_subtitles else "playlist"
                stripped = _URI.sub(lambda m: f'URI="{register(urljoin(base_url, m[1]), kind, None)}"', stripped)
            elif stripped.startswith("#EXT-X-STREAM-INF:"):
                variant_next = True
                if renditions:
                    stripped = re.sub(r',?SUBTITLES="[^"]*"', "", stripped) + f',SUBTITLES="{SUBTITLE_GROUP}"'
            elif stripped.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
                stripped = _URI.sub(lambda m: f'URI="{register(urljoin(base_url, m[1]), "playlist", None)}"', stripped)
            elif "URI=" in stripped:  # session keys and other referenced data
                stripped = _URI.sub(lambda m: f'URI="{register(urljoin(base_url, m[1]), "segment", None)}"', stripped)
            output.append(stripped)
            if stripped == "#EXTM3U":
                output.extend(_media_line(rendition) for rendition in renditions)
            continue
        output.append(register(urljoin(base_url, stripped), "playlist", None) if variant_next else stripped)
        variant_next = False
    return "\n".join(output) + "\n"


def rewrite_media(text: str, base_url: str, register: Register, *, track_progress: bool = True) -> tuple[str, float]:
    """Relay a media playlist's segments; returns the playlist and its total duration."""
    output: list[str] = []
    elapsed = 0.0
    duration = 0.0
    for line in text.lstrip("﻿").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if stripped.startswith("#EXTINF:"):
                try:
                    duration = float(stripped[len("#EXTINF:"):].split(",", 1)[0])
                except ValueError:
                    duration = 0.0
                if not math.isfinite(duration) or duration < 0:
                    duration = 0.0
            elif "URI=" in stripped:  # keys, init sections, partial segments
                stripped = _URI.sub(lambda m: f'URI="{register(urljoin(base_url, m[1]), "segment", None)}"', stripped)
            output.append(stripped)
            continue
        output.append(register(urljoin(base_url, stripped), "segment", elapsed if track_progress else None))
        elapsed += duration
        duration = 0.0
    return "\n".join(output) + "\n", elapsed


def single_variant(text: str, height: int | None = None) -> str:
    """A master playlist cut down to one video variant and that variant's default audio.

    The variant is the best one, or with ``height`` the tallest at or below it (the
    smallest when none is). FFmpeg reads every variant and audio track it is given;
    this leaves it exactly one of each, so joining the stream into one file keeps the
    language the source marks as default. Subtitle and I-frame entries are dropped.
    """
    lines = [line.strip() for line in text.lstrip("﻿").splitlines() if line.strip()]
    variants: list[tuple[dict[str, str], str, str]] = []  # attributes, tag, URI
    audio: list[tuple[dict[str, str], str]] = []
    for position, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:") and position + 1 < len(lines) and not lines[position + 1].startswith("#"):
            variants.append((_attributes(line), line, lines[position + 1]))
        elif line.startswith("#EXT-X-MEDIA:") and _attributes(line).get("TYPE") == "AUDIO":
            audio.append((_attributes(line), line))
    if not variants:
        return text

    def bandwidth(variant: tuple[dict[str, str], str, str]) -> int:
        try:
            return int(variant[0].get("BANDWIDTH", "0"))
        except ValueError:
            return 0

    def tall(variant: tuple[dict[str, str], str, str]) -> int:
        match = re.fullmatch(r"\d+x(\d+)", variant[0].get("RESOLUTION", ""))
        return int(match[1]) if match else 0

    chosen_variant = max(variants, key=bandwidth)
    if height is not None:
        fitting = [variant for variant in variants if 0 < tall(variant) <= height]
        sized = [variant for variant in variants if tall(variant)]
        if fitting:
            chosen_variant = max(fitting, key=lambda variant: (tall(variant), bandwidth(variant)))
        elif sized:
            chosen_variant = min(sized, key=lambda variant: (tall(variant), bandwidth(variant)))
        elif height <= 1:  # "worst", with no resolutions listed: the smallest by bitrate
            chosen_variant = min(variants, key=bandwidth)
    attributes, tag, uri = chosen_variant
    output = ["#EXTM3U"]
    group = attributes.get("AUDIO")
    if group:
        choices = [(attrs, line) for attrs, line in audio if attrs.get("GROUP-ID") == group]
        chosen = next((line for attrs, line in choices if attrs.get("DEFAULT") == "YES"), choices[0][1] if choices else None)
        if chosen is not None:
            output.append(re.sub(r"DEFAULT=\w+", "DEFAULT=YES", chosen))
    tag = re.sub(r',?(?:SUBTITLES|CLOSED-CAPTIONS)="[^"]*"', "", tag)
    output += [tag, uri]
    return "\n".join(output) + "\n"


def wrap_media(media_url: str, renditions: Sequence[Rendition]) -> str:
    """A one-variant master playlist, so a media playlist can carry Cue's subtitles."""
    lines = ["#EXTM3U", *(_media_line(rendition) for rendition in renditions)]
    lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH=2000000,SUBTITLES="{SUBTITLE_GROUP}"')
    lines.append(media_url)
    return "\n".join(lines) + "\n"


def subtitle_playlist(vtt_url: str, duration: float) -> str:
    """One complete WebVTT file as an HLS subtitle media playlist."""
    length = max(1.0, duration)
    return (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXT-X-MEDIA-SEQUENCE:0\n"
        f"#EXT-X-TARGETDURATION:{math.ceil(length)}\n#EXTINF:{length:.3f},\n{vtt_url}\n#EXT-X-ENDLIST\n"
    )


def to_webvtt(text: str, extension: str) -> str | None:
    """WebVTT text for a cached ``.vtt`` or ``.srt`` file; other formats are unsupported."""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if extension == ".vtt":
        return text if text.startswith("WEBVTT") else None
    if extension == ".srt":
        return "WEBVTT\n\n" + _SRT_TIME.sub(r"\1.\2", text.strip()) + "\n"
    return None


def webvtt_duration(text: str) -> float:
    """The end time of the last cue, in seconds."""
    end = 0.0
    for match in _VTT_TIME.finditer(text):
        hours, minutes, seconds, millis = match.group(5, 6, 7, 8)
        end = max(end, int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000)
    return end
