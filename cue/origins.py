"""Where a registered provider or player came from, so a plugin's additions can be removed with it.

Origins are ``built-in``, ``plugin:<name>``, ``package:<name>`` (an entry point) or
``file:<path>`` (a drop-in module).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

BUILT_IN = "built-in"
_current = BUILT_IN


def current() -> str:
    return _current


@contextmanager
def loading(origin: str) -> Iterator[None]:
    """Credit everything registered inside the block to ``origin``."""
    global _current
    previous, _current = _current, origin
    try:
        yield
    finally:
        _current = previous
