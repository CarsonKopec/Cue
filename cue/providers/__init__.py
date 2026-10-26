"""Provider registry.

Providers are found three ways, so new ones can be dropped in without touching the core:

* modules in this package (the built-in ``jellyfin`` and ``local``),
* the ``cue.providers`` entry-point group of installed packages,
* ``*.py`` files in the plug-in folder (``%APPDATA%\\cue\\providers``).

A provider module registers its class with :func:`register`.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
import pkgutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cue.providers.base import Provider, ProviderConfigError

log = logging.getLogger(__name__)

_registry: dict[str, type] = {}


def register(cls: type) -> type:
    """Class decorator that makes a provider available under its ``name``."""
    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not name:
        raise TypeError(f"{cls!r} needs a non-empty 'name' attribute")
    _registry[name] = cls
    return cls


def discover(plugin_dir: Path | None = None) -> dict[str, type]:
    """Import every provider source and return the registry."""
    for module in pkgutil.iter_modules(__path__):
        if module.name != "base":
            importlib.import_module(f"{__name__}.{module.name}")
    for entry_point in importlib.metadata.entry_points(group="cue.providers"):
        try:
            register(entry_point.load())
        except Exception:
            log.exception("Could not load provider entry point %r", entry_point.name)
    if plugin_dir is not None and plugin_dir.is_dir():
        for path in sorted(plugin_dir.glob("*.py")):
            _load_plugin_file(path)
    return dict(_registry)


def _load_plugin_file(path: Path) -> None:
    module_name = f"cue_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        log.exception("Could not load provider plug-in %s", path)


def create_providers(
    settings: Mapping[str, Mapping[str, Any]], plugin_dir: Path | None = None
) -> tuple[dict[str, Provider], dict[str, str]]:
    """Instantiate every registered provider that has a config table.

    Returns the ready providers and, separately, why each of the others is unavailable.
    """
    ready: dict[str, Provider] = {}
    problems: dict[str, str] = {}
    for name, cls in sorted(discover(plugin_dir).items()):
        if name not in settings:
            continue
        try:
            ready[name] = cls(settings[name])
        except ProviderConfigError as exc:
            problems[name] = str(exc)
        except Exception as exc:
            log.exception("Provider %r failed to start", name)
            problems[name] = f"failed to start: {exc}"
    return ready, problems
