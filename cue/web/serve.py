"""Running ``cue serve``: the core, the cast server (whose relay browsers play from) and the web app."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

from aiohttp import web

from cue import __version__
from cue.cast.network import lan_address
from cue.config import Config
from cue.core import Core
from cue.web.api import Events, create_app

log = logging.getLogger(__name__)


async def serve(config: Config, *, host: str = "", port: int = 0, stop: asyncio.Event | None = None) -> int:
    """Serve until ``stop`` is set (or SIGINT/SIGTERM); returns an exit code."""
    host = host or config.web.host
    port = port or config.web.port
    events = Events()
    core = Core(config, on_notice=events.notice, on_status=events.status)
    # Browsers play through the cast server's relay, so it runs whatever [cast] enabled says,
    # and it looks for subtitles in the subtitle sources as playback on a PC does.
    problem = core.start_cast(source_subtitles=True)
    if problem:
        print(f"cue: {problem}", file=sys.stderr)
    app = create_app(core, events, cast_problem=problem)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host or None, port)
        try:
            await site.start()
        except OSError as exc:
            print(f"cue: cannot listen on {host or 'every network adapter'}, port {port} ({exc}). "
                  "Choose another with --port or [web] port.", file=sys.stderr)
            return 1
        address = host if host and host not in {"0.0.0.0", "::"} else lan_address()
        print(f"Cue {__version__} is serving http://{address}:{port}", flush=True)
        if core.cast_server is not None:
            print(f"Videos and TVs use the cast server at {core.cast_server.base_url}", flush=True)
        for line in core.provider_problems.items():
            print("cue: %s: %s" % line, file=sys.stderr)
        stop = stop or asyncio.Event()
        loop = asyncio.get_running_loop()
        for number in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):  # Windows has no handlers
                loop.add_signal_handler(number, stop.set)
        await stop.wait()
        print("Cue is stopping", flush=True)
        return 0
    finally:
        await runner.cleanup()
        await core.aclose()


def run(config: Config, *, host: str = "", port: int = 0) -> int:
    try:
        return asyncio.run(serve(config, host=host, port=port))
    except KeyboardInterrupt:  # Ctrl+C on Windows, where asyncio can't install a handler for it
        return 0
