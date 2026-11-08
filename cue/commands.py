"""Commands besides the TUI: ``cue install``, ``cue uninstall``, ``cue update`` and ``cue plugin …``."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path

import httpx

from cue import __version__
from cue.config import (
    Config, ConfigError, app_dir, config_path, edit_config, load_config, plugins_dir, set_plugin_disabled,
)
from cue.install import EXE, InstallError, UserRegistry, install, install_dir, installed, is_frozen, uninstall
from cue.plugins import PluginError, PluginManager, fetch_catalog, is_newer, is_web_address
from cue.plugins.actions import WARNING, Source, after_install, after_remove, fetch, find_source, updates

DECLINED = "install-declined"  # in the app folder: "never" was the answer to the first-run question


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, help=f"config file (default: {config_path()})")
    parser = argparse.ArgumentParser(prog="cue")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install", parents=[common], help="install cue.exe for this user and add it to PATH")
    remove = commands.add_parser("uninstall", parents=[common], help="remove the installed cue.exe and its PATH entry")
    remove.add_argument("-y", "--yes", action="store_true", help="don't ask first")
    refresh = commands.add_parser("update", parents=[common], help="update Cue and its plugins now")
    refresh.add_argument("--check", action="store_true", help="only say what's available")

    plugin = commands.add_parser("plugin", aliases=["plugins"], help="list, install and remove plugins")
    actions = plugin.add_subparsers(dest="action", required=True)
    actions.add_parser("list", parents=[common], help="installed plugins")
    actions.add_parser("catalog", parents=[common], help="plugins in the catalog")
    add = actions.add_parser("install", parents=[common], help="install from the catalog, a link, a .zip or a folder")
    add.add_argument("source", help="a catalog name, an https:// link to a .zip, or a .zip file or folder")
    add.add_argument("-y", "--yes", action="store_true", help="don't ask first")
    update = actions.add_parser("update", parents=[common], help="update plugins from the catalog")
    update.add_argument("names", nargs="*", help="plugins to update (default: all)")
    update.add_argument("-y", "--yes", action="store_true", help="don't ask first")
    for name, text in (("remove", "uninstall a plugin"), ("enable", "turn a plugin on"), ("disable", "turn a plugin off")):
        action = actions.add_parser(name, parents=[common], help=text)
        action.add_argument("name")
    return parser


def run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "install":
            return _install(args)
        if args.command == "uninstall":
            return _uninstall(args)
        if args.command == "update":
            return asyncio.run(_update(args))
        return _plugin(args)
    except (ConfigError, InstallError, PluginError, OSError) as exc:
        print(f"cue: {exc}", file=sys.stderr)
        return 1


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# -- install / uninstall -----------------------------------------------------------------


def _install(args: argparse.Namespace) -> int:
    if not is_frozen():
        print("cue install copies cue.exe. This Cue runs from Python, where pip already put `cue` on PATH.")
        return 1
    target, path_changed = install(Path(sys.executable), UserRegistry())
    print(f"Installed Cue {__version__} to {target.parent}")
    if path_changed:
        print("Added it to your PATH. Open a new terminal and type `cue`.")
    else:
        print("It was already on your PATH.")
    print("It's listed in Settings → Apps; `cue uninstall` removes it.")
    return 0


def _uninstall(args: argparse.Namespace) -> int:
    registry = UserRegistry()
    found = installed(registry)
    if found is None:
        print("Cue isn't installed (no entry in Apps & Features).")
    if not args.yes and not _confirm(f"Remove Cue from {found.folder if found else install_dir()} and PATH?"):
        print("Nothing removed.")
        return 1
    folder = uninstall(registry)
    print(f"Removed Cue from {folder} and from PATH. Your settings and history in {app_dir()} are kept.")
    return 0


def offer_install(ask: Callable[[str], str] | None = None) -> None:
    """When a downloaded cue.exe runs, offer to install it, or to update an older installed copy."""
    if sys.platform != "win32" or not is_frozen() or os.environ.get("CUE_HOME") or not sys.stdin.isatty():
        return
    marker = app_dir() / DECLINED
    if marker.exists():
        return
    running = Path(sys.executable).resolve()
    registry = UserRegistry()
    current = installed(registry)
    if current is not None:
        if running == (current.folder / EXE).resolve() or (current.version and not is_newer(__version__, current.version)):
            return
        question = f"Update the installed Cue {current.version or ''} to {__version__}?"
    else:
        if running.parent == install_dir().resolve():
            return
        question = "Install Cue for this user and add it to PATH, so `cue` works in any terminal?"
    try:
        answer = (ask or input)(f"{question} [Y/n/never] ").strip().lower()
    except EOFError:
        return
    if answer == "never":
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("Delete this file to be asked again.\n", encoding="utf-8")
        print("OK, Cue won't ask again. `cue install` still works.")
    elif answer in ("", "y", "yes"):
        try:
            target, path_changed = install(Path(sys.executable), registry)
        except (InstallError, OSError) as exc:
            print(f"Couldn't install: {exc}")
            return
        print(f"Installed to {target.parent}." + (" Open a new terminal to use `cue`." if path_changed else ""))


# -- update ------------------------------------------------------------------------------


async def _update(args: argparse.Namespace) -> int:
    """Update cue.exe and the plugins now (not waiting for the daily check), or with --check just report."""
    from cue import update

    config = load_config(args.config)
    manager = PluginManager(plugins_dir(), config.plugins.disabled)
    manager.clean_up()
    manager.load_enabled()
    frozen = is_frozen()
    async with _client() as http:
        outcome = await update.run(http, manager, config.plugins.catalog, install=not args.check, app=frozen,
                                   registry=update.user_registry() if frozen else None)
    update.mark_checked()  # counts as today's check

    release = outcome.release
    if not frozen:
        print(f"This is Cue {__version__} running from Python: update it with git pull, "
              "or pip install -U for an installed package.")
    elif outcome.app_error:
        print(f"Couldn't check for a new Cue: {outcome.app_error}")
    elif release is not None and outcome.app_installed:
        print(f"Updated Cue {__version__} to {release.version}. Open cue again to use it.")
    elif release is not None and release.newer:
        print(f"Cue {release.version} is available (this is {__version__}). Run cue update to install it.")
    else:
        print(f"Cue {__version__} is up to date.")

    if not config.plugins.catalog:
        print("Plugins: the catalog is turned off ([plugins] catalog is empty).")
    elif outcome.plugins_installed:
        print("Updated plugins: " + ", ".join(
            f"{plugin.manifest.title} {plugin.manifest.version}" for plugin in outcome.plugins_installed))
    elif outcome.plugins_available:
        print("Plugin updates available: " + ", ".join(
            f"{entry.manifest.title} {entry.manifest.version}" for entry in outcome.plugins_available))
    elif not outcome.plugin_errors:
        print("Plugins are up to date.")
    for problem in outcome.plugin_errors:
        print(f"Plugin update problem: {problem}")
    return 1 if outcome.app_error or outcome.plugin_errors else 0


# -- plugins ------------------------------------------------------------------------------


def _plugin(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    manager = PluginManager(plugins_dir(), config.plugins.disabled)
    manager.clean_up()
    if args.action == "list":
        return _list(manager)
    if args.action == "catalog":
        return asyncio.run(_catalog(config, manager))
    if args.action in ("enable", "disable"):
        manager.scan()
        if manager.get(args.name) is None:
            raise PluginError(f"plugin {args.name!r} isn't installed")
        edit_config(config.path or config_path(), set_plugin_disabled(args.name, args.action == "disable"))
        print(f"Turned {args.name} {'off' if args.action == 'disable' else 'on'}.")
        return 0
    manager.load_enabled()  # so the players and providers in use are known
    if args.action == "remove":
        plugin = manager.get(args.name)
        if plugin is None:
            raise PluginError(f"plugin {args.name!r} isn't installed")
        provided = manager.remove(args.name)
        changes = after_remove(plugin, provided, config)
        if changes:
            edit_config(config.path or config_path(), *changes)
        print(f"Removed {plugin.manifest.title}. Its settings stay in the config.")
        return 0
    return asyncio.run(_install_plugins(args, config, manager))


def _list(manager: PluginManager) -> int:
    plugins = manager.scan()
    if not plugins:
        print(f"No plugins installed in {manager.root}. `cue plugin catalog` lists some.")
    for plugin in plugins:
        state = f"error: {plugin.error}" if plugin.error else "on" if plugin.enabled else "off"
        adds = ", ".join([*plugin.manifest.providers, *plugin.manifest.players])
        print(f"{plugin.name:<16} {plugin.manifest.version:<9} {state:<5} {adds}")
    return 0


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": f"Cue/{__version__}"})


async def _catalog(config: Config, manager: PluginManager) -> int:
    if not config.plugins.catalog:
        print("The catalog is turned off ([plugins] catalog is empty).")
        return 1
    async with _client() as http:
        entries = await fetch_catalog(http, config.plugins.catalog)
    installed_plugins = {plugin.name: plugin for plugin in manager.scan()}
    for entry in entries:
        plugin = installed_plugins.get(entry.manifest.name)
        mark = "" if plugin is None else " (installed)" if not is_newer(
            entry.manifest.version, plugin.manifest.version) else f" (update from {plugin.manifest.version})"
        print(f"{entry.manifest.name:<16} {entry.manifest.version:<9} {entry.manifest.description}{mark}")
    return 0


async def _install_plugins(args: argparse.Namespace, config: Config, manager: PluginManager) -> int:
    async with _client() as http:
        catalog = None
        wants_catalog = args.action == "update" or not (
            is_web_address(args.source) or Path(args.source).expanduser().exists()
        )
        if wants_catalog and config.plugins.catalog:
            try:
                catalog = await fetch_catalog(http, config.plugins.catalog)
            except PluginError as exc:
                print(f"The catalog is unavailable: {exc}", file=sys.stderr)
        if args.action == "update":
            newer = updates(manager.plugins(), catalog or [])
            chosen = [entry for name, entry in newer.items() if not args.names or name in args.names]
            if not chosen:
                print("Everything is up to date." if catalog is not None else "Nothing to update.")
                return 0 if catalog is not None else 1
            sources = [Source(entry.url, entry) for entry in chosen]
        else:
            sources = [find_source(args.source, catalog)]
        listing = "\n".join(f"  {source.title} from {source.location}" for source in sources)
        if not args.yes and not _confirm(f"Install:\n{listing}\n{WARNING}\nGo ahead?"):
            print("Nothing installed.")
            return 1
        failed = False
        for source in sources:
            plugin = manager.install(await fetch(http, source), source.location)
            changes = after_install(plugin, config)
            if changes:
                config = edit_config(config.path or config_path(), *changes)
            if plugin.error:
                print(f"Installed {plugin.manifest.title} {plugin.manifest.version}, but it didn't load: {plugin.error}")
                failed = True
            else:
                adds = ", ".join([*plugin.providers, *plugin.players]) or "nothing yet"
                print(f"Installed {plugin.manifest.title} {plugin.manifest.version} (adds {adds}).")
    return 1 if failed else 0
