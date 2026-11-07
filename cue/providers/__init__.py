"""Provider registry.

Providers are found four ways, so new ones can be added without touching the core:

* modules in this package (the built-in ``jellyfin`` and ``local``),
* installed plugins (``%APPDATA%\\cue\\plugins\\<name>\\``, loaded by :mod:`cue.plugins`),
* the ``cue.providers`` entry-point group of installed packages,
* single ``*.py`` files in the drop-in folder (``%APPDATA%\\cue\\providers``).

A provider module registers its class with :func:`register`. The registry remembers
where each provider came from, so the Plugins menu can show it and a plugin's
providers can be dropped when the plugin is turned off or removed.
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

from cue.origins import BUILT_IN, current, loading
from cue.providers.base import Provider, ProviderConfigError

__all__ = [
    "BUILT_IN", "create_providers", "discover", "is_enabled", "origin_of", "provider_options",
    "register", "registered_by", "unregister",
]

log = logging.getLogger(__name__)

_NOT_PROVIDERS = {"base", "web"}

_registry: dict[str, type] = {}
_origins: dict[str, str] = {}
_builtins_loaded = False
_dropins: dict[Path, float] = {}  # drop-in file -> modification time when it was loaded


def register(cls: type) -> type:
    """Class decorator that makes a provider available under its ``name``."""
    name = getattr(cls, "name", None)
    if not isinstance(name, str) or not name:
        raise TypeError(f"{cls!r} needs a non-empty 'name' attribute")
    _registry[name] = cls
    _origins[name] = current()
    return cls


def registered_by(origin: str) -> list[str]:
    """The providers that ``origin`` registered."""
    return sorted(name for name, source in _origins.items() if source == origin)


def unregister(origin: str) -> list[str]:
    """Forget every provider that ``origin`` registered and return their names."""
    names = registered_by(origin)
    for name in names:
        _registry.pop(name, None)
        _origins.pop(name, None)
    return names


def origin_of(name: str) -> str | None:
    """Where provider ``name`` came from: ``built-in``, ``plugin:<name>``, ``package:<name>`` or ``file:<path>``."""
    return _origins.get(name)


def discover(plugin_dir: Path | None = None) -> dict[str, type]:
    """Import the built-in, entry-point and drop-in providers once, and return the registry.

    Plugins are loaded separately by :class:`cue.plugins.PluginManager`; their
    providers are already in the registry when this runs.
    """
    global _builtins_loaded
    if not _builtins_loaded:
        for module in pkgutil.iter_modules(__path__):
            if module.name not in _NOT_PROVIDERS:
                importlib.import_module(f"{__name__}.{module.name}")
        for entry_point in importlib.metadata.entry_points(group="cue.providers"):
            package = entry_point.dist.name if entry_point.dist else entry_point.name
            try:
                with loading(f"package:{package}"):
                    register(entry_point.load())
            except Exception:
                log.exception("Could not load provider entry point %r", entry_point.name)
        _builtins_loaded = True
    if plugin_dir is not None and plugin_dir.is_dir():
        for path in sorted(plugin_dir.glob("*.py")):
            modified = path.stat().st_mtime
            if _dropins.get(path) != modified:
                _dropins[path] = modified
                _load_dropin(path)
    return dict(_registry)


def _load_dropin(path: Path) -> None:
    module_name = f"cue_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        with loading(f"file:{path}"):
            spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        log.exception("Could not load provider plug-in %s", path)


def provider_options(table: Mapping[str, Any]) -> dict[str, Any]:
    """A config table without Cue's own ``enabled`` key: what the provider is made with."""
    return {key: value for key, value in table.items() if key != "enabled"}


def is_enabled(options: Mapping[str, Any]) -> bool:
    """A ``[providers.<name>]`` table turns its provider on unless it says ``enabled = false``."""
    return options.get("enabled", True) is not False


def create_providers(
    settings: Mapping[str, Mapping[str, Any]],
    plugin_dir: Path | None = None,
    *,
    reuse: Mapping[str, tuple[Provider, Mapping[str, Any]]] | None = None,
) -> tuple[dict[str, Provider], dict[str, str]]:
    """Instantiate every registered provider whose config table turns it on.

    Returns the ready providers and, separately, why each of the others is unavailable.
    ``reuse`` maps names to an existing instance and the options it was made with;
    an instance is kept when its class and options are unchanged.
    """
    classes = discover(plugin_dir)
    ready: dict[str, Provider] = {}
    problems: dict[str, str] = {}
    for name in sorted(settings):
        options = provider_options(settings[name])
        if not is_enabled(settings[name]):
            continue
        cls = classes.get(name)
        if cls is None:
            problems[name] = "not installed; press p on the Home screen to find its plugin"
            continue
        previous = (reuse or {}).get(name)
        if previous is not None and type(previous[0]) is cls and previous[1] == options:
            ready[name] = previous[0]
            continue
        try:
            ready[name] = cls(options)
        except ProviderConfigError as exc:
            problems[name] = str(exc)
        except Exception as exc:
            log.exception("Provider %r failed to start", name)
            problems[name] = f"failed to start: {exc}"
    return ready, problems
