"""Installed plugins: folders under ``%APPDATA%\\cue\\plugins``, each a Python package.

A plugin folder holds ``plugin.toml`` and ``__init__.py``; it may hold more modules
(imported relatively, ``from .helpers import x``) and a ``lib`` folder of pure-Python
dependencies. Plugins can use the Python standard library and Cue's own packages
(``cue``, ``httpx``, ``rapidfuzz``, ``textual``). A plugin is imported as
``cue_plugins.<name>`` and registers providers with :func:`cue.providers.register`
and players with :func:`cue.player.registry.register_player`.

Plugins are ordinary Python code that runs with the user's own access, so Cue only
installs one when asked to, and shows where it comes from first.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import shutil
import sys
import tempfile
import types
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from cue import providers
from cue.origins import loading
from cue.player.registry import players_registered_by, unregister_players
from cue.subtitles.registry import subtitles_registered_by, unregister_subtitles
from cue.plugins.archive import extract
from cue.plugins.manifest import MANIFEST, Manifest, PluginError, check_compatible, read_manifest

log = logging.getLogger(__name__)

PACKAGE = "cue_plugins"
SOURCE_FILE = ".source"  # where the plugin was installed from, for the menu


@dataclass
class InstalledPlugin:
    manifest: Manifest
    path: Path
    enabled: bool = True
    valid: bool = True  # False when plugin.toml can't be read; error says why
    loaded: bool = False
    error: str | None = None
    providers: tuple[str, ...] = field(default=())  # what it registered when loaded
    players: tuple[str, ...] = field(default=())
    subtitles: tuple[str, ...] = field(default=())

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def source(self) -> str:
        try:
            return (self.path / SOURCE_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            return ""


def _namespace() -> types.ModuleType:
    module = sys.modules.get(PACKAGE)
    if module is None:
        module = types.ModuleType(PACKAGE, "Installed Cue plugins.")
        module.__path__ = []
        sys.modules[PACKAGE] = module
    return module


def _origin(name: str) -> str:
    return f"plugin:{name}"


class PluginManager:
    def __init__(self, root: Path, disabled: set[str] | list[str] | tuple[str, ...] = ()) -> None:
        self.root = root
        self.disabled = set(disabled)
        self._plugins: dict[str, InstalledPlugin] = {}

    # -- reading ------------------------------------------------------------------

    def scan(self) -> list[InstalledPlugin]:
        """Read every installed plugin's manifest; nothing is imported."""
        found: dict[str, InstalledPlugin] = {}
        folders = sorted(self.root.iterdir()) if self.root.is_dir() else []
        for folder in folders:
            if folder.name.startswith(".") or not (folder / MANIFEST).is_file():
                continue
            previous = self._plugins.get(folder.name)
            try:
                manifest = read_manifest(folder)
                if manifest.name != folder.name:
                    raise PluginError(f"{folder} holds plugin {manifest.name!r}; the folder must have the same name")
                plugin = InstalledPlugin(manifest, folder, enabled=folder.name not in self.disabled)
            except PluginError as exc:
                placeholder = Manifest(name=folder.name, version="0", title=folder.name)
                plugin = InstalledPlugin(placeholder, folder, enabled=folder.name not in self.disabled,
                                         valid=False, error=str(exc))
            if previous is not None and previous.loaded and previous.manifest == plugin.manifest:
                plugin.loaded, plugin.providers, plugin.players = True, previous.providers, previous.players
                plugin.subtitles = previous.subtitles
            found[folder.name] = plugin
        for name, previous in self._plugins.items():  # removed or changed on disk since it was loaded
            current = found.get(name)
            if previous.loaded and not (current and current.loaded):
                self._forget(previous)
        self._plugins = found
        return self.plugins()

    def plugins(self) -> list[InstalledPlugin]:
        return list(self._plugins.values())

    def get(self, name: str) -> InstalledPlugin | None:
        return self._plugins.get(name)

    # -- loading ------------------------------------------------------------------

    def load_enabled(self) -> list[InstalledPlugin]:
        """Scan, then import every enabled plugin that isn't loaded yet."""
        for plugin in self.scan():
            if plugin.enabled and plugin.valid and not plugin.loaded:
                self.load(plugin.name)
        return self.plugins()

    def load(self, name: str) -> InstalledPlugin:
        """Import a plugin (again, if it was loaded) so its providers and players register."""
        plugin = self._plugins[name]
        self.unload(name)
        self._forget(plugin)  # also what another manager (the CLI, say) loaded under this name
        package = f"{PACKAGE}.{plugin.manifest.module}"
        init = plugin.path / "__init__.py"
        try:
            check_compatible(plugin.manifest)
            if not init.is_file():
                raise PluginError(f"{name} has no __init__.py")
        except PluginError as exc:
            plugin.error = str(exc)
            return plugin
        lib = plugin.path / "lib"
        if lib.is_dir() and str(lib) not in sys.path:
            sys.path.append(str(lib))
        namespace = _namespace()
        spec = importlib.util.spec_from_file_location(package, init, submodule_search_locations=[str(plugin.path)])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        importlib.invalidate_caches()
        try:
            with loading(_origin(name)):
                spec.loader.exec_module(module)
        except Exception as exc:
            log.exception("Could not load plugin %s", name)
            self._forget(plugin)
            plugin.error = f"failed to load: {type(exc).__name__}: {exc}"
            return plugin
        setattr(namespace, plugin.manifest.module, module)
        plugin.loaded, plugin.error = True, None
        plugin.providers = tuple(providers.registered_by(_origin(name)))
        plugin.players = tuple(players_registered_by(_origin(name)))
        plugin.subtitles = tuple(subtitles_registered_by(_origin(name)))
        return plugin

    def _forget(self, plugin: InstalledPlugin) -> list[str]:
        package = f"{PACKAGE}.{plugin.manifest.module}"
        for module in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
            del sys.modules[module]
        namespace = sys.modules.get(PACKAGE)
        if namespace is not None and hasattr(namespace, plugin.manifest.module):
            delattr(namespace, plugin.manifest.module)
        unregister_players(_origin(plugin.name))
        unregister_subtitles(_origin(plugin.name))
        return providers.unregister(_origin(plugin.name))

    def unload(self, name: str) -> list[str]:
        """Drop a plugin's modules and providers; returns the provider names it had."""
        plugin = self._plugins.get(name)
        if plugin is None or not plugin.loaded:
            return []
        removed = self._forget(plugin)
        plugin.loaded, plugin.providers, plugin.players, plugin.subtitles = False, (), (), ()
        return removed

    def set_enabled(self, name: str, enabled: bool) -> InstalledPlugin:
        plugin = self._plugins[name]
        plugin.enabled = enabled
        if enabled:
            self.disabled.discard(name)
            return self.load(name) if plugin.valid else plugin
        self.disabled.add(name)
        self.unload(name)
        return plugin

    # -- installing ---------------------------------------------------------------

    def install(self, source: Path | bytes, origin: str) -> InstalledPlugin:
        """Install or replace a plugin from a folder, a ``.zip`` file or zip bytes, then load it.

        ``origin`` is recorded and shown in the menu. A failed install leaves the
        plugin that was there before untouched.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".install-", dir=self.root))
        try:
            if isinstance(source, bytes):
                extract(source, staging)
            elif source.is_dir():
                shutil.copytree(source, staging, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".*"))
            elif source.is_file():
                extract(source.read_bytes(), staging)
            else:
                raise PluginError(f"{source} doesn't exist")
            manifest = read_manifest(staging)
            check_compatible(manifest)
            if not (staging / "__init__.py").is_file():
                raise PluginError(f"{manifest.name} has no __init__.py")
            (staging / SOURCE_FILE).write_text(origin, encoding="utf-8")
            self.unload(manifest.name)
            target = self.root / manifest.name
            if target.exists():
                retired = self.root / f".old-{manifest.name}-{uuid.uuid4().hex[:8]}"
                target.rename(retired)
                staging.rename(target)
                shutil.rmtree(retired, ignore_errors=True)
            else:
                staging.rename(target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self.disabled.discard(manifest.name)
        self.scan()
        return self.load(manifest.name)

    def remove(self, name: str) -> list[str]:
        """Uninstall a plugin; returns the provider names it had registered."""
        plugin = self._plugins.get(name)
        if plugin is None:
            raise PluginError(f"plugin {name!r} isn't installed")
        removed = self.unload(name)
        retired = self.root / f".old-{name}-{uuid.uuid4().hex[:8]}"
        plugin.path.rename(retired)  # one step, so a half-deleted plugin never loads
        shutil.rmtree(retired, ignore_errors=True)
        self.disabled.discard(name)
        self.scan()
        return removed

    def clean_up(self) -> None:
        """Delete leftovers of interrupted installs and removals."""
        if self.root.is_dir():
            for folder in self.root.iterdir():
                if folder.is_dir() and folder.name.startswith((".install-", ".old-")):
                    shutil.rmtree(folder, ignore_errors=True)
