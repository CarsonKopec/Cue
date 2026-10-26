"""Entry point: ``cue [query]``."""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from cue import __version__
from cue.config import ConfigError, config_path, load_config, log_path


def _setup_logging() -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cue", description="Keyboard-driven terminal media player with automatic intro/outro skipping."
    )
    parser.add_argument("query", nargs="*", help="search for a show right away")
    parser.add_argument("-p", "--provider", help="provider to start with (default: from the config)")
    parser.add_argument("--config", type=Path, help=f"config file (default: {config_path()})")
    parser.add_argument("--smoke-test", action="store_true", help="start headless, show the home screen, exit")
    parser.add_argument("--version", action="version", version=f"cue {__version__}")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as exc:
        print(f"cue: {exc}", file=sys.stderr)
        return 2
    _setup_logging()

    from cue.app import CueApp

    app = CueApp(config, initial_query=" ".join(args.query) or None, provider=args.provider)
    if args.smoke_test:
        return _smoke_test(app)
    app.run()
    return app.return_code or 0


def _smoke_test(app: object) -> int:
    """Check that a (frozen) build can start the TUI: Textual, CSS, store and providers."""
    from cue.app import CueApp
    from cue.providers.base import Show
    from cue.screens.home import HomeScreen
    from cue.screens.results import fuzzy_filter

    assert isinstance(app, CueApp)
    result: dict[str, object] = {}

    async def pilot(driver: object) -> None:
        await app.workers.wait_for_complete()
        result["screen"] = type(app.screen).__name__
        result["providers"] = sorted(app.providers)
        result["fuzzy"] = [s.id for s in fuzzy_filter("frieren", [Show("a", "Sousou no Frieren"), Show("b", "Other")])]
        app.exit()

    app.run(headless=True, size=(100, 30), auto_pilot=pilot)
    ok = result.get("screen") == HomeScreen.__name__ and result.get("fuzzy") == ["a"]
    print(f"cue {__version__} smoke test {'ok' if ok else 'FAILED'}: {result}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
