"""Which of this PC's network addresses the TV should use."""

from __future__ import annotations

import socket
import struct
import sys


class AdapterError(Exception):
    """The configured network adapter is missing, disconnected or has no IPv4 address."""


def lan_address() -> str:
    """This PC's address on the default route (no packets are sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("10.255.255.255", 1))
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def adapters() -> dict[str, str]:
    """Connected network adapters by name (as in Windows' network settings) and their IPv4 address."""
    if sys.platform == "win32":
        return _windows_adapters()
    if sys.platform.startswith("linux"):
        return _linux_adapters()
    raise AdapterError("Choosing an adapter by name works on Windows and Linux; set cast.host instead")


def adapter_address(name: str) -> str:
    wanted = name.strip().casefold()
    found = adapters()
    for adapter, address in found.items():
        if adapter.casefold() == wanted:
            return address
    available = ", ".join(f"{adapter} ({address})" for adapter, address in sorted(found.items()))
    raise AdapterError(
        f"No connected network adapter is named {name!r}. "
        f"Connected adapters: {available or 'none with an IPv4 address'}"
    )


def _windows_adapters() -> dict[str, str]:
    import ctypes
    from ctypes import wintypes

    class Sockaddr(ctypes.Structure):
        _fields_ = [("family", ctypes.c_ushort), ("data", ctypes.c_ubyte * 14)]

    class SocketAddress(ctypes.Structure):
        _fields_ = [("sockaddr", ctypes.POINTER(Sockaddr)), ("length", ctypes.c_int)]

    class Unicast(ctypes.Structure):
        pass

    Unicast._fields_ = [
        ("Length", ctypes.c_ulong), ("Flags", wintypes.DWORD),
        ("Next", ctypes.POINTER(Unicast)), ("Address", SocketAddress),
    ]

    class Adapter(ctypes.Structure):  # the leading fields of IP_ADAPTER_ADDRESSES
        pass

    Adapter._fields_ = [
        ("Length", ctypes.c_ulong), ("IfIndex", wintypes.DWORD),
        ("Next", ctypes.POINTER(Adapter)), ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.POINTER(Unicast)), ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p), ("FirstDnsServerAddress", ctypes.c_void_p),
        ("DnsSuffix", ctypes.c_wchar_p), ("Description", ctypes.c_wchar_p), ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8), ("PhysicalAddressLength", wintypes.DWORD),
        ("Flags", wintypes.DWORD), ("Mtu", wintypes.DWORD), ("IfType", wintypes.DWORD), ("OperStatus", ctypes.c_int),
    ]

    get_addresses = ctypes.windll.iphlpapi.GetAdaptersAddresses
    get_addresses.argtypes = [wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG)]
    get_addresses.restype = wintypes.ULONG
    skip_anycast_multicast_dns = 0x2 | 0x4 | 0x8
    size = wintypes.ULONG(16 * 1024)
    for _ in range(3):
        buffer = ctypes.create_string_buffer(size.value)
        result = get_addresses(socket.AF_INET, skip_anycast_multicast_dns, None, buffer, ctypes.byref(size))
        if result != 111:  # ERROR_BUFFER_OVERFLOW: size now holds the length needed
            break
    if result != 0:
        raise AdapterError(f"Windows could not list the network adapters (error {result})")

    found: dict[str, str] = {}
    pointer = ctypes.cast(buffer, ctypes.POINTER(Adapter))
    while pointer:
        adapter = pointer.contents
        if adapter.OperStatus == 1 and adapter.FriendlyName:  # IfOperStatusUp
            unicast = adapter.FirstUnicastAddress
            while unicast:
                sockaddr = unicast.contents.Address.sockaddr
                if sockaddr and sockaddr.contents.family == socket.AF_INET:
                    address = socket.inet_ntoa(bytes(sockaddr.contents.data[2:6]))
                    if not address.startswith("127."):
                        found.setdefault(adapter.FriendlyName, address)
                        break
                unicast = unicast.contents.Next
        pointer = adapter.Next
    return found


def _linux_adapters() -> dict[str, str]:
    import fcntl

    found: dict[str, str] = {}
    for _, name in socket.if_nameindex():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                packed = fcntl.ioctl(probe.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))  # SIOCGIFADDR
            except OSError:
                continue  # down or no IPv4 address
        address = socket.inet_ntoa(packed[20:24])
        if not address.startswith("127."):
            found[name] = address
    return found
