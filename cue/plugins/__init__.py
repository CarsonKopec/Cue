"""Plugins: installable folders of Python code that add providers to Cue."""

from cue.plugins.catalog import CatalogEntry, fetch_archive, fetch_catalog, is_web_address
from cue.plugins.manager import InstalledPlugin, PluginManager
from cue.plugins.manifest import Manifest, PluginError, is_newer

__all__ = [
    "CatalogEntry",
    "InstalledPlugin",
    "Manifest",
    "PluginError",
    "PluginManager",
    "fetch_archive",
    "fetch_catalog",
    "is_newer",
    "is_web_address",
]
