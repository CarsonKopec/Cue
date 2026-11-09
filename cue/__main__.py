"""Entry point: ``cue [query]``."""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from cue import __version__
from cue.config import ConfigError, config_path, load_config, log_path

COMMANDS = ("install", "uninstall", "update", "plugin", "plugins")  # see cue.commands


def _setup_logging() -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def _tolerant_output() -> None:
    """Print characters the console's code page lacks as ``?`` instead of crashing.

    Piped output, or an old console, uses a code page like cp1252, which has no ``→``.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    _tolerant_output()
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in COMMANDS:
        from cue.commands import run

        return run(argv)
    parser = argparse.ArgumentParser(
        prog="cue", description="Keyboard-driven terminal media player with automatic intro/outro skipping.",
        epilog="Other commands: cue install, cue uninstall, cue update, cue plugin (list|catalog|install|update|remove). "
               "To search for one of these words, put -- first: cue -- install",
    )
    parser.add_argument("query", nargs="*", help="search for a show right away")
    parser.add_argument("-g", "--group", help="search group to start in, such as movies, shows or anime")
    parser.add_argument("-p", "--provider", help="search only this provider")
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

    if not args.smoke_test:
        from cue.commands import offer_install

        offer_install()

    from cue.app import CueApp

    app = CueApp(config, initial_query=" ".join(args.query) or None, provider=args.provider,
                 group=args.group.lower() if args.group else None)
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
        result["tvs"] = _cast_libraries()
        result["aes"] = _aes_works()
        result["pairing"] = _airplay_pairing_works()
        app.exit()

    app.run(headless=True, size=(100, 30), auto_pilot=pilot)
    ok = (result.get("screen") == HomeScreen.__name__ and result.get("fuzzy") == ["a"]
          and result.get("tvs") == ["airplay", "chromecast", "renderer"] and result.get("aes") is True
          and result.get("pairing") is True)
    print(f"cue {__version__} smoke test {'ok' if ok else 'FAILED'}: {result}")
    return 0 if ok else 1


def _cast_libraries() -> list[str]:
    """The TV kinds a build can cast to, after importing what each needs (so a build missing them fails)."""
    import pychromecast.controllers.media
    import pychromecast.discovery
    import pyatv.protocols.airplay
    import pyatv.storage.file_storage  # noqa: F401

    from cue.cast import devices

    return devices.supported()


def _airplay_pairing_works(code: str = "1234") -> bool:
    """Pair the way an AirPlay TV that shows a code does: pyatv for Cue, srptools for the TV.

    pyatv's pairing broke with srptools 2.0 ("SRPClientSession.__init__() takes 2
    positional arguments"), and only typing a TV's code reached it.
    """
    import binascii
    import hashlib

    from pyatv.auth.hap_srp import SRPAuthHandler
    from srptools import SRPContext, SRPServerSession, constants

    cue = SRPAuthHandler()
    cue.initialize()
    cue.step1(code)
    tv_context = SRPContext("Pair-Setup", code, prime=constants.PRIME_3072, generator=constants.PRIME_3072_GEN,
                            hash_func=hashlib.sha512)
    _, verifier, salt = tv_context.get_user_data_triplet()
    tv = SRPServerSession(tv_context, verifier)
    public, proof = cue.step2(binascii.unhexlify(tv.public), binascii.unhexlify(salt))
    tv.process(binascii.hexlify(public).decode(), salt)
    return bool(tv.verify_proof(binascii.hexlify(proof)))  # the TV accepts the code


def _aes_works() -> bool:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from cue.player.relay import _can_decrypt, _decrypt

    key, iv = bytes(range(16)), bytes(range(16, 32))
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    piece = encryptor.update(b"cue" + bytes([13]) * 13) + encryptor.finalize()
    return _can_decrypt() and _decrypt(piece, key, iv) == b"cue"


if __name__ == "__main__":
    sys.exit(main())
