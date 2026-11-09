"""DLNA TVs: the media renderer built into most smart TVs (Samsung, LG, Sony, …), told what to play.

UPnP, as a control point:

1. **Find** renderers: an SSDP ``M-SEARCH`` for ``MediaRenderer:1`` on each network adapter.
   Each answer's ``LOCATION`` is the address of the TV's description.
2. **Describe**: the description names the TV and gives its ``AVTransport`` control
   address. ``ConnectionManager``'s ``GetProtocolInfo`` says what it plays, so speakers
   (audio only) can be left out.
3. **Control** it with SOAP: ``SetAVTransportURI`` (an episode's address on Cue's cast
   server), ``Play``, ``Seek`` to the resume point, and ``GetTransportInfo`` to learn when
   it stops.

Everything comes from devices on the local network, so answers are checked: only plain
http addresses on the device that answered, descriptions of limited size and without
DTDs (no entity tricks).
"""

from __future__ import annotations

import logging
import re
import select
import socket
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from xml.sax.saxutils import escape

import httpx

log = logging.getLogger(__name__)

GROUP, PORT = "239.255.255.250", 1900
RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"
MAX_XML = 512 * 1024


class RendererError(Exception):
    """A DLNA TV refused a command or stopped answering."""


@dataclass(frozen=True)
class Renderer:
    name: str
    udn: str  # the TV's unique id, uuid:…
    host: str
    model: str
    av_transport: str  # AVTransport's control address
    connection_manager: str = ""  # ConnectionManager's control address, if it has one


# -- finding them -------------------------------------------------------------------------


def search(addresses: Iterable[str], timeout: float) -> list[str]:
    """Description addresses of the media renderers that answer within ``timeout`` seconds. Blocks."""
    wait = max(1, min(int(timeout) - 1, 3))
    message = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {GROUP}:{PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {wait}\r\n"
        f"ST: {RENDERER}\r\n\r\n"
    ).encode()
    sockets: list[socket.socket] = []
    for address in dict.fromkeys(addresses):
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
            sender.bind((address, 0))
            for _ in range(2):  # UDP can be lost
                sender.sendto(message, (GROUP, PORT))
            sockets.append(sender)
        except OSError as exc:
            log.debug("SSDP search from %s failed: %s", address, exc)
            sender.close()
    locations: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        while sockets and (remaining := deadline - time.monotonic()) > 0:
            readable, _, _ = select.select(sockets, [], [], remaining)
            for receiver in readable:
                try:
                    data, (sender_host, _) = receiver.recvfrom(8192)
                except OSError:
                    continue
                location = _location(data, sender_host)
                if location and location not in locations:
                    locations.append(location)
    finally:
        for receiver in sockets:
            receiver.close()
    return locations


def _location(answer: bytes, sender: str) -> str:
    """An SSDP answer's LOCATION, if it's a plain http address on the device that answered."""
    match = re.search(rb"^location:[ \t]*(\S+)", answer, re.IGNORECASE | re.MULTILINE)
    if match is None:
        return ""
    location = match.group(1).decode("ascii", "replace")
    parts = urlsplit(location)
    return location if parts.scheme == "http" and parts.hostname == sender else ""


async def describe(http: httpx.AsyncClient, location: str) -> Renderer | None:
    """The renderer a description address describes; None if it isn't one Cue can drive."""
    try:
        root = _parse(await _fetch(http, location))
    except (httpx.HTTPError, RendererError, ET.ParseError) as exc:
        log.info("Couldn't read the DLNA description at %s: %s", location, exc)
        return None
    base = _text(root, "URLBase") or location
    host = urlsplit(location).hostname or ""
    for device in root.iter():
        if _local(device.tag) != "device" or RENDERER.rsplit(":", 1)[0] not in _text(device, "deviceType", deep=False):
            continue
        controls: dict[str, str] = {}
        for service in device.iter():
            if _local(service.tag) == "service":
                kind, control = _text(service, "serviceType", deep=False), _text(service, "controlURL", deep=False)
                address = urljoin(base, control) if control else ""
                if address and urlsplit(address).hostname == host:  # only ever the TV itself
                    controls[kind.rsplit(":", 1)[0]] = address
        transport = controls.get(AV_TRANSPORT.rsplit(":", 1)[0])
        if transport is None:
            continue
        name = _text(device, "friendlyName", deep=False) or host
        name = re.sub(r"^\[(TV|tv)\]\s*", "", name)  # Samsung's "[TV] Samsung 7 Series (50)"
        return Renderer(name, _text(device, "UDN", deep=False) or location, host, _text(device, "modelName", deep=False),
                        transport, controls.get(CONNECTION_MANAGER.rsplit(":", 1)[0], ""))
    return None


async def plays_video(http: httpx.AsyncClient, renderer: Renderer) -> bool:
    """Whether its ConnectionManager lists any video (or HLS); unknown counts as yes."""
    if not renderer.connection_manager:
        return True
    try:
        sink = (await call(http, renderer.connection_manager, CONNECTION_MANAGER, "GetProtocolInfo", ())).get("Sink", "")
    except (httpx.HTTPError, RendererError):
        return True
    return not sink or "video/" in sink or "mpegurl" in sink.lower()


# -- controlling them ---------------------------------------------------------------------


async def call(
    http: httpx.AsyncClient, control: str, service: str, action: str, arguments: Sequence[tuple[str, str]],
) -> dict[str, str]:
    """Send one SOAP action; returns its answer's fields. Raises RendererError when refused."""
    fields = "".join(f"<{name}>{escape(str(value))}</{name}>" for name, value in arguments)
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service}">{fields}</u:{action}></s:Body></s:Envelope>'
    )
    response = await http.post(control, content=envelope.encode(), headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{service}#{action}"',
    })
    text = response.text[:MAX_XML]
    if response.status_code != 200:
        description = re.search(r"<errorDescription>(.*?)</errorDescription>", text, re.S)
        code = re.search(r"<errorCode>(.*?)</errorCode>", text, re.S)
        reason = description.group(1).strip() if description else f"HTTP {response.status_code}"
        raise RendererError(f"{action}: {reason}" + (f" ({code.group(1).strip()})" if code else ""))
    try:
        root = _parse(text)
    except (RendererError, ET.ParseError) as exc:
        raise RendererError(f"{action}: unreadable answer ({exc})") from exc
    answer = next((element for element in root.iter() if _local(element.tag) == f"{action}Response"), None)
    return {_local(child.tag): (child.text or "") for child in answer} if answer is not None else {}


def didl(url: str, title: str, content_type: str, subtitle: str = "") -> str:
    """DIDL-Lite metadata for one video, which SetAVTransportURI passes along.

    ``subtitle`` (an SRT address) is given the ways TVs look for one: Samsung's
    ``sec:CaptionInfoEx``/``sec:CaptionInfo``, and ``pv:subtitleFileUri`` on the video,
    which LG and others read.
    """
    srt = escape(subtitle, {'"': "&quot;"})
    resource = f'protocolInfo="http-get:*:{escape(content_type)}:*"'
    captions = ""
    if subtitle:
        resource += f' pv:subtitleFileUri="{srt}" pv:subtitleFileType="srt"'
        captions = (f'<res protocolInfo="http-get:*:text/srt:*">{srt}</res>'
                    f'<sec:CaptionInfoEx sec:type="srt">{srt}</sec:CaptionInfoEx>'
                    f'<sec:CaptionInfo sec:type="srt">{srt}</sec:CaptionInfo>')
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:sec="http://www.sec.co.kr/" xmlns:pv="http://www.pv.com/pvns/">'
        '<item id="cue" parentID="-1" restricted="1">'
        f"<dc:title>{escape(title)}</dc:title><upnp:class>object.item.videoItem</upnp:class>"
        f"<res {resource}>{escape(url)}</res>{captions}"
        "</item></DIDL-Lite>"
    )


def clock(seconds: float) -> str:
    """Seconds as UPnP's H:MM:SS."""
    whole = max(0, int(seconds))
    return f"{whole // 3600}:{whole // 60 % 60:02d}:{whole % 60:02d}"


# -- XML -----------------------------------------------------------------------------------


async def _fetch(http: httpx.AsyncClient, url: str) -> str:
    async with http.stream("GET", url) as response:
        response.raise_for_status()
        data = b""
        async for chunk in response.aiter_bytes():
            data += chunk
            if len(data) > MAX_XML:
                raise RendererError("description too large")
    return data.decode("utf-8", "replace")


def _parse(text: str) -> ET.Element:
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise RendererError("XML with a DTD")
    return ET.fromstring(text.strip())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ET.Element, name: str, *, deep: bool = True) -> str:
    found = element.iter() if deep else iter(element)
    return next(((child.text or "").strip() for child in found if _local(child.tag) == name), "")
