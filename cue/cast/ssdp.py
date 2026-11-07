"""SSDP announcements, so DLNA clients such as VLC find Cue on the local network."""

from __future__ import annotations

import email.utils
import logging
import socket
import struct
import threading

log = logging.getLogger(__name__)

GROUP = "239.255.255.250"
PORT = 1900
MAX_AGE = 1800
ANNOUNCE_EVERY = 60.0
DEVICE = "urn:schemas-upnp-org:device:MediaServer:1"
SERVICES = (
    "urn:schemas-upnp-org:service:ContentDirectory:1",
    "urn:schemas-upnp-org:service:ConnectionManager:1",
)


class Advertiser:
    """Sends NOTIFY alive/byebye and answers M-SEARCH for one media server."""

    def __init__(self, *, uuid: str, location: str, address: str, server: str) -> None:
        self.uuid = uuid
        self.location = location
        self.address = address
        self.server = server
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sender: socket.socket | None = None
        self._listener: socket.socket | None = None

    def targets(self) -> list[tuple[str, str]]:
        """(NT/ST, USN) pairs this device announces."""
        udn = f"uuid:{self.uuid}"
        return [
            ("upnp:rootdevice", f"{udn}::upnp:rootdevice"),
            (udn, udn),
            (DEVICE, f"{udn}::{DEVICE}"),
            *((service, f"{udn}::{service}") for service in SERVICES),
        ]

    def notify(self, kind: str) -> list[bytes]:
        messages = []
        for target, usn in self.targets():
            lines = ["NOTIFY * HTTP/1.1", f"HOST: {GROUP}:{PORT}", f"NT: {target}", f"NTS: ssdp:{kind}", f"USN: {usn}"]
            if kind == "alive":
                lines += [f"CACHE-CONTROL: max-age={MAX_AGE}", f"LOCATION: {self.location}", f"SERVER: {self.server}"]
            messages.append(("\r\n".join(lines) + "\r\n\r\n").encode())
        return messages

    def responses(self, request: bytes) -> list[bytes]:
        """Replies to an M-SEARCH request; empty when it is not for this device."""
        try:
            text = request.decode("utf-8")
        except UnicodeError:
            return []
        head, *rest = text.split("\r\n")
        if not head.upper().startswith("M-SEARCH * HTTP/1.1"):
            return []
        headers = {}
        for line in rest:
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().upper()] = value.strip()
        if headers.get("MAN", "").strip('"') != "ssdp:discover":
            return []
        wanted = headers.get("ST", "")
        replies = []
        for target, usn in self.targets():
            if wanted in {"ssdp:all", target}:
                replies.append((
                    "HTTP/1.1 200 OK\r\n"
                    f"CACHE-CONTROL: max-age={MAX_AGE}\r\n"
                    f"DATE: {email.utils.formatdate(usegmt=True)}\r\n"
                    "EXT:\r\n"
                    f"LOCATION: {self.location}\r\n"
                    f"SERVER: {self.server}\r\n"
                    f"ST: {target}\r\n"
                    f"USN: {usn}\r\n\r\n"
                ).encode())
        return replies

    def start(self) -> None:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.address))
        sender.bind((self.address, 0))
        self._sender = sender
        try:
            self._listener = self._listen()
        except OSError as exc:
            # Windows' own SSDP service may hold the port. Clients still learn
            # about Cue from the periodic NOTIFY announcements.
            log.warning("Cannot answer SSDP searches (%s); announcing only", exc)
        self._threads = [threading.Thread(target=self._announce, name="cue-ssdp-notify", daemon=True)]
        if self._listener is not None:
            self._threads.append(threading.Thread(target=self._answer, name="cue-ssdp-search", daemon=True))
        for thread in self._threads:
            thread.start()

    def _listen(self) -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("", PORT))
            membership = struct.pack("4s4s", socket.inet_aton(GROUP), socket.inet_aton(self.address))
            listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            listener.settimeout(1.0)
        except OSError:
            listener.close()
            raise
        return listener

    def _send(self, message: bytes, address: tuple[str, int]) -> None:
        if self._sender is None:
            return
        try:
            self._sender.sendto(message, address)
        except OSError as exc:
            log.debug("SSDP send failed: %s", exc)

    def _announce(self) -> None:
        delay = 0.5  # repeat quickly at first; UDP announcements can be lost
        while not self._stop.is_set():
            for message in self.notify("alive"):
                self._send(message, (GROUP, PORT))
            self._stop.wait(delay)
            delay = min(delay * 4, ANNOUNCE_EVERY)

    def _answer(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                data, sender = self._listener.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            for reply in self.responses(data):
                self._send(reply, sender)

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        for message in self.notify("byebye"):
            self._send(message, (GROUP, PORT))
        for sock in (self._listener, self._sender):
            if sock is not None:
                sock.close()
        for thread in self._threads:
            thread.join(timeout=2)
