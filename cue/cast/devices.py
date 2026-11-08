"""TVs Cue tells what to play: AirPlay (pyatv) and Chromecast (pychromecast).

VLC on the Xbox browses Cue's media server (DLNA) and picks episodes itself. AirPlay
and Chromecast receivers are driven from Cue instead: Cue finds them on the network,
hands each one an episode's address on the cast server, and starts the next episode
when one finishes. The cast server relays the stream either way, so headers,
subtitles and resume points work the same.

Both libraries are imported only when they're used, and either may be missing.
AirPlay TVs that ask for a code are paired once; pyatv keeps the credentials in
``airplay.json`` beside Cue's config.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

AIRPLAY = "airplay"
CHROMECAST = "chromecast"
SCAN_SECONDS = 4.0
CONNECT_SECONDS = 15.0
_DEFAULT_RECEIVER = "CC1AD845"  # Chromecast's Default Media Receiver, which play_media opens
_PLAYING = {"PLAYING", "BUFFERING", "PAUSED"}


class DeviceError(Exception):
    """A TV could not be reached, refused to play, or stopped with an error."""


class PairingNeeded(DeviceError):
    """An AirPlay TV wants a code before it plays anything."""


@dataclass(frozen=True)
class Device:
    kind: str  # AIRPLAY or CHROMECAST
    name: str
    identifier: str
    address: str
    model: str = ""
    needs_pairing: bool = False  # AirPlay: the TV asks for a code, and Cue has none saved
    handle: Any = field(default=None, compare=False, repr=False)  # the library's own record

    @property
    def kind_label(self) -> str:
        return "AirPlay" if self.kind == AIRPLAY else "Chromecast"


@dataclass(frozen=True)
class Media:
    """One episode on the cast server, ready for a TV."""

    url: str
    title: str
    content_type: str
    start: float = 0.0  # seconds; where to resume


class Receiver:
    """A TV Cue is connected to."""

    async def play(self, media: Media) -> None:
        """Play ``media``; returns once it stops: it finished, was stopped on the TV, or was replaced."""
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


def supported() -> list[str]:
    """The kinds of TV this copy of Cue can drive (its libraries are installed)."""
    return [kind for kind, module in ((AIRPLAY, "pyatv"), (CHROMECAST, "pychromecast"))
            if importlib.util.find_spec(module) is not None]


async def discover(credentials: Path, timeout: float = SCAN_SECONDS) -> list[Device]:
    """The AirPlay and Chromecast video receivers that answer within ``timeout`` seconds."""
    kinds = supported()
    searches = []
    if AIRPLAY in kinds:
        searches.append(_airplay_devices(credentials, timeout))
    if CHROMECAST in kinds:
        searches.append(_chromecast_devices(timeout))
    found: list[Device] = []
    for result in await asyncio.gather(*searches, return_exceptions=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            log.warning("Looking for TVs failed: %r", result)
            continue
        found.extend(result)
    return sorted(found, key=lambda device: (device.name.lower(), device.kind))


async def connect(device: Device, credentials: Path) -> Receiver:
    if device.kind == AIRPLAY:
        return await _AirPlay.open(device, credentials)
    if device.kind == CHROMECAST:
        return await _Chromecast.open(device)
    raise DeviceError(f"Unknown kind of TV: {device.kind}")


async def pair(device: Device, credentials: Path, ask_code: Callable[[], Awaitable[str | None]]) -> None:
    """Pair with an AirPlay TV: it shows a code, ``ask_code`` returns what was typed (None cancels)."""
    import pyatv
    from pyatv import exceptions
    from pyatv.const import Protocol

    storage = await _storage(credentials)
    try:
        handler = await pyatv.pair(device.handle, Protocol.AirPlay, asyncio.get_running_loop(), storage=storage)
    except Exception as exc:  # pyatv's errors share no base class
        raise DeviceError(f"Could not start pairing with {device.name}: {_describe(exc)}") from exc
    try:
        await handler.begin()
        if handler.device_provides_pin:
            code = await ask_code()
            if not code:
                raise DeviceError("Pairing cancelled")
            handler.pin(code.strip())
        await handler.finish()
        if not handler.has_paired:
            raise DeviceError(f"{device.name} didn't accept the code")
    except exceptions.PairingError as exc:
        raise DeviceError(f"{device.name} didn't accept the code ({_describe(exc)})") from exc
    except DeviceError:
        raise
    except Exception as exc:
        raise DeviceError(f"Pairing with {device.name} failed: {_describe(exc)}") from exc
    finally:
        with contextlib.suppress(Exception):
            await handler.close()
    await storage.save()


# -- AirPlay ---------------------------------------------------------------------------


async def _storage(path: Path) -> Any:
    from pyatv.storage.file_storage import FileStorage

    path.parent.mkdir(parents=True, exist_ok=True)
    storage = FileStorage(str(path), asyncio.get_running_loop())
    await storage.load()
    return storage


def _plays_video(properties: dict[str, str]) -> bool:
    """AirPlay's ``features`` flags: bit 0 is video. Speakers (HomePod, AirPort) leave it off."""
    features = properties.get("features", "")
    try:
        return bool(int(features.split(",")[0], 16) & 1) if features else True
    except ValueError:
        return True


async def _airplay_devices(credentials: Path, timeout: float) -> list[Device]:
    import pyatv
    from pyatv.const import PairingRequirement, Protocol

    storage = await _storage(credentials)
    configs = await pyatv.scan(asyncio.get_running_loop(), timeout=int(max(1, timeout)), protocol=Protocol.AirPlay,
                               storage=storage)
    devices = []
    for config in configs:
        service = config.get_service(Protocol.AirPlay)
        if service is None or not _plays_video(dict(service.properties)):
            continue
        needs_code = service.pairing == PairingRequirement.Mandatory and not service.credentials
        model = config.device_info.model_str if config.device_info else ""
        devices.append(Device(AIRPLAY, config.name, config.identifier or str(config.address), str(config.address),
                              "" if model in {"", "Unknown"} else model, needs_code, config))
    return devices


class _AirPlay(Receiver):
    def __init__(self, device: Device, atv: Any) -> None:
        self._device = device
        self._atv = atv

    @classmethod
    async def open(cls, device: Device, credentials: Path) -> _AirPlay:
        import pyatv

        storage = await _storage(credentials)
        try:
            atv = await asyncio.wait_for(
                pyatv.connect(device.handle, asyncio.get_running_loop(), storage=storage), CONNECT_SECONDS)
        except TimeoutError as exc:
            raise DeviceError(f"{device.name} didn't answer") from exc
        except Exception as exc:
            raise _airplay_error(device, exc) from exc
        return cls(device, atv)

    async def play(self, media: Media) -> None:
        try:
            # pyatv returns once the TV stops playing, whatever stopped it.
            await self._atv.stream.play_url(media.url, position=int(media.start))
        except Exception as exc:
            raise _airplay_error(self._device, exc) from exc

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            pending = self._atv.close()
            if pending:
                await asyncio.wait(pending, timeout=5)


def _airplay_error(device: Device, exc: Exception) -> DeviceError:
    from pyatv import exceptions

    if isinstance(exc, (exceptions.AuthenticationError, exceptions.NoCredentialsError,
                        exceptions.InvalidCredentialsError)):
        return PairingNeeded(f"{device.name} wants a code before it plays anything")
    if isinstance(exc, exceptions.HttpError) and getattr(exc, "status_code", None) in {401, 403, 470}:
        return PairingNeeded(f"{device.name} wants a code before it plays anything")
    if isinstance(exc, exceptions.NotSupportedError):
        return DeviceError(f"{device.name} can't play videos sent over AirPlay ({_describe(exc)})")
    return DeviceError(f"{device.name}: {_describe(exc)}")


# -- Chromecast --------------------------------------------------------------------------


async def _chromecast_devices(timeout: float) -> list[Device]:
    def scan() -> list[Any]:
        from pychromecast.discovery import discover_chromecasts

        found, browser = discover_chromecasts(timeout=timeout)
        browser.stop_discovery()
        return list(found)

    return [
        Device(CHROMECAST, info.friendly_name or info.model_name or info.host, str(info.uuid), info.host,
               info.model_name or "", handle=info)
        for info in await asyncio.to_thread(scan)
        if info.cast_type in (None, "cast")  # not speakers ("audio") or speaker groups ("group")
    ]


class _Chromecast(Receiver):
    def __init__(self, device: Device, cast: Any) -> None:
        self._device = device
        self._cast = cast
        self._ended: asyncio.Future[None] | None = None
        self._loop = asyncio.get_running_loop()
        self._url = ""
        self._started = False
        self._listening = False

    @classmethod
    async def open(cls, device: Device) -> _Chromecast:
        def open_cast() -> Any:
            import pychromecast

            info = device.handle
            cast = pychromecast.get_chromecast_from_host(
                (info.host, info.port, info.uuid, info.model_name, info.friendly_name), tries=2,
                timeout=CONNECT_SECONDS)
            cast.wait(timeout=CONNECT_SECONDS)
            return cast

        try:
            cast = await asyncio.to_thread(open_cast)
        except Exception as exc:
            raise DeviceError(f"Could not reach {device.name}: {_describe(exc)}") from exc
        return cls(device, cast)

    # pychromecast calls these on its own thread.

    def new_media_status(self, status: Any) -> None:
        if getattr(status, "content_id", None) != self._url:
            return  # what was playing before, or someone else's video
        if status.player_state in _PLAYING:
            self._started = True
        elif status.player_state == "IDLE" and self._started:
            error = DeviceError(f"{self._device.name} stopped with an error") if status.idle_reason == "ERROR" else None
            self._finish(error)

    def load_media_failed(self, queue_item_id: int, error_code: int) -> None:
        self._finish(DeviceError(f"{self._device.name} couldn't play this (error {error_code})"))

    def new_cast_status(self, status: Any) -> None:
        if self._started and getattr(status, "app_id", None) != _DEFAULT_RECEIVER:
            self._finish(None)  # something else took over the TV

    def _finish(self, error: DeviceError | None) -> None:
        def settle() -> None:
            if self._ended is not None and not self._ended.done():
                if error is None:
                    self._ended.set_result(None)
                else:
                    self._ended.set_exception(error)

        self._loop.call_soon_threadsafe(settle)

    async def play(self, media: Media) -> None:
        if not self._listening:
            self._cast.media_controller.register_status_listener(self)
            self._cast.register_status_listener(self)
            self._listening = True
        self._url, self._started = media.url, False
        self._ended = self._loop.create_future()
        try:
            await asyncio.to_thread(
                self._cast.media_controller.play_media, media.url, media.content_type, title=media.title,
                current_time=media.start or None, stream_type="BUFFERED")
        except Exception as exc:
            raise DeviceError(f"Could not start playback on {self._device.name}: {_describe(exc)}") from exc
        await self._ended

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._cast.disconnect, 5)


def _describe(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__
