"""Subtitle sources: OpenSubtitles built in, more from plugins, all searched together."""

from __future__ import annotations

import tomllib
from pathlib import Path

import httpx
import pytest

from cue.app import CueApp
from cue.config import Config, ConfigError, SubtitleConfig, edit_config, load_config, parse_config
from cue.config import set_subtitle_enabled, set_subtitle_options
from cue.plugins import PluginError, PluginManager
from cue.plugins.manifest import parse_manifest
from cue.providers.base import Episode, Setting, Show
from cue.store import Store
from cue.subtitles import SubtitleError, SubtitleService, SubtitleTrack
from cue.subtitles.registry import (
    SubtitleBackend, create_sources, register_subtitles, subtitle_backends, subtitle_origin, unregister_subtitles,
)
from test_plugins import write_plugin

SHOW = Show("s1", "Show")
EPISODE = Episode("s1", 1)


class FakeSource:
    def __init__(self, name: str, folder: Path, tracks: tuple[tuple[str, str], ...] = (), *,
                 error: Exception | None = None, broken_files: bool = False) -> None:
        self.name = name
        self._folder = folder
        self._tracks = tracks  # (language, id)
        self._error = error
        self._broken_files = broken_files
        self.searched: list[list[str]] = []
        self.fetched: list[str] = []

    async def find(self, show, episode, languages, *, file_path=None):
        self.searched.append(list(languages))
        if self._error is not None:
            raise self._error
        return [SubtitleTrack(language, f"{self.name} {id_}", id=id_, source=self.name if id_ != "anon" else "")
                for language, id_ in self._tracks if language in languages]

    async def fetch(self, track):
        if self._broken_files:
            raise SubtitleError(f"{self.name}: daily limit reached")
        self.fetched.append(track.id)
        path = self._folder / f"{self.name}-{track.id}.srt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8")
        return str(path)


# -- the service: every source at once ------------------------------------------------------


async def test_every_source_is_searched_in_order(tmp_path: Path, store: Store) -> None:
    first = FakeSource("first", tmp_path, (("en", "a"), ("fr", "b")))
    second = FakeSource("second", tmp_path, (("en", "c"), ("en", "anon")))
    service = SubtitleService([first, second], store, ["en", "fr"])
    tracks = await service.search(SHOW, EPISODE)
    assert [(t.source, t.id) for t in tracks] == [("first", "a"), ("first", "b"), ("second", "c"), ("second", "anon")]
    assert service.source is first  # the preferred one

    path = await service.choose("p", SHOW, EPISODE, tracks[2])  # downloaded by the source that found it
    assert second.fetched == ["c"] and first.fetched == [] and Path(path).name == "second-c.srt"


async def test_one_source_failing_doesnt_stop_the_others(tmp_path: Path, store: Store) -> None:
    down = FakeSource("down", tmp_path, error=SubtitleError("server is down"))
    fine = FakeSource("fine", tmp_path, (("en", "a"),))
    service = SubtitleService([down, fine], store, ["en"])
    assert [t.id for t in await service.search(SHOW, EPISODE)] == ["a"]

    nothing = SubtitleService([down, FakeSource("other", tmp_path, error=OSError("no network"))], store, ["en"])
    with pytest.raises(SubtitleError, match="down: server is down; other: no network"):
        await nothing.search(SHOW, EPISODE)
    assert await SubtitleService([fine], store, ["de"]).search(SHOW, EPISODE) == []  # no match isn't an error


async def test_a_failed_download_falls_back_to_the_next_match(tmp_path: Path, store: Store) -> None:
    quota = FakeSource("quota", tmp_path, (("en", "a"),), broken_files=True)
    backup = FakeSource("backup", tmp_path, (("en", "b"),))
    found = await SubtitleService([quota, backup], store, ["en"]).ensure("p", SHOW, EPISODE)
    assert [Path(path).name for _, path in found] == ["backup-b.srt"]
    cached = await SubtitleService([quota, backup], store, ["en"]).ensure("p", SHOW, EPISODE)
    assert cached == found and backup.fetched == ["b"]  # kept, not fetched again

    with pytest.raises(SubtitleError, match="daily limit"):  # nothing worked: say why
        await SubtitleService([quota], store, ["en"]).ensure("p", Show("s2", "Other"), EPISODE)
    with pytest.raises(SubtitleError, match="isn't available"):
        await SubtitleService([backup], store, ["en"]).choose("p", SHOW, EPISODE, SubtitleTrack("en", "x", source="gone"))
    with pytest.raises(ValueError):
        SubtitleService([], store, ["en"])


# -- the registry ------------------------------------------------------------------------------


@pytest.fixture
def plugin_sources():
    """Two sources as a plugin would add them: one needs a key, one doesn't, plus a broken one."""
    made: list[tuple[dict, Path]] = []

    def create(options, http, folder):
        made.append((dict(options), folder))
        return FakeSource("needs-key", folder)

    def broken(options, http, folder):
        raise RuntimeError("bad plugin")

    origin = "plugin:test-subs"
    register_subtitles(SubtitleBackend("needs-key", "Needs a key", create,
                                       settings=(Setting("api_key", "API key", "secret", required=True),)),
                       origin=origin)
    register_subtitles(SubtitleBackend("free", "Free", lambda options, http, folder: FakeSource("free", folder)),
                       origin=origin)
    register_subtitles(SubtitleBackend("broken", "Broken", broken), origin=origin)
    yield made
    unregister_subtitles(origin)


def test_sources_start_when_set_up(tmp_path: Path, plugin_sources) -> None:
    assert list(subtitle_backends())[0] == "opensubtitles"  # built in, so first
    assert subtitle_origin("opensubtitles") == "built-in" and subtitle_origin("free") == "plugin:test-subs"
    http = httpx.AsyncClient()

    sources, problems = create_sources({}, http, tmp_path)
    assert [s.name for s in sources] == ["free"]
    assert problems == {"opensubtitles": "needs api key", "needs-key": "needs api key",
                        "broken": "couldn't start: bad plugin"}

    tables = {"opensubtitles": {"api_key": "os"}, "needs-key": {"api_key": " k "}, "free": {"enabled": False}}
    sources, problems = create_sources(tables, http, tmp_path)
    assert [s.name for s in sources] == ["opensubtitles", "needs-key"] and problems["free"] == "off"
    assert plugin_sources == [({"api_key": " k "}, tmp_path)]
    assert unregister_subtitles("plugin:test-subs") == ["broken", "free", "needs-key"]
    assert list(subtitle_backends()) == ["opensubtitles"]


def test_a_plugin_adds_and_removes_its_sources(tmp_path: Path) -> None:
    body = ('from cue.subtitles.registry import SubtitleBackend, register_subtitles\n'
            'register_subtitles(SubtitleBackend("tsubs-s", "Demo", lambda options, http, folder: None))\n')
    manager = PluginManager(tmp_path / "plugins")
    plugin = manager.install(write_plugin(tmp_path / "src", "tsubs", body=body, extra='subtitles = ["tsubs-s"]\n'), "src")
    assert plugin.subtitles == ("tsubs-s",) and plugin.manifest.subtitles == ("tsubs-s",)
    assert subtitle_origin("tsubs-s") == "plugin:tsubs"
    manager.unload("tsubs")
    assert "tsubs-s" not in subtitle_backends()


def test_manifest_lists_subtitle_sources() -> None:
    table = {"name": "subdl", "version": "1.0.0", "subtitles": ["subdl"]}
    assert parse_manifest(table).subtitles == ("subdl",)
    assert parse_manifest({"name": "x", "version": "1.0.0"}).subtitles == ()
    with pytest.raises(PluginError, match="subtitles must be a list of names"):
        parse_manifest({**table, "subtitles": "subdl"})


# -- settings ----------------------------------------------------------------------------------


def test_each_source_has_a_subtitles_table(tmp_path: Path) -> None:
    config = parse_config(tomllib.loads(
        '[subtitles]\nlanguages = ["en"]\n\n[subtitles.opensubtitles]\napi_key = "os"\n\n'
        '[subtitles.subdl]\napi_key = "sd"\nenabled = false\n'))
    assert config.subtitles.opensubtitles_api_key == "os"
    assert config.subtitles.sources == {"opensubtitles": {"api_key": "os"}, "subdl": {"api_key": "sd", "enabled": False}}
    with pytest.raises(ConfigError, match="subtitles.subdl.enabled must be true or false"):
        parse_config(tomllib.loads('[subtitles.subdl]\nenabled = "no"\n'))

    path = tmp_path / "config.toml"
    path.write_text('# mine\n[subtitles]\nlanguages = ["en", "fr"]\n', encoding="utf-8")
    edit_config(path, set_subtitle_options("subdl", {"api_key": "new"}), set_subtitle_enabled("gestdown", False))
    saved = load_config(path)
    assert saved.subtitles.languages == ["en", "fr"]
    assert saved.subtitles.sources == {"subdl": {"api_key": "new"}, "gestdown": {"enabled": False}}
    edit_config(path, set_subtitle_enabled("gestdown", True))
    assert load_config(path).subtitles.sources["gestdown"] == {}
    assert path.read_text(encoding="utf-8").startswith("# mine")


def test_the_app_searches_the_sources_that_are_set_up(store: Store, tmp_path: Path, plugin_sources) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    app = CueApp(Config(), store=store, http=http, dropin_dir=tmp_path / "none")
    assert app.subtitles is not None and [s.name for s in app.subtitles.sources] == ["free"]  # needs no key
    assert app.subtitle_problems["opensubtitles"] == "needs api key"

    sources = {"opensubtitles": {"api_key": "k"}, "free": {"enabled": False}}
    app = CueApp(Config(subtitles=SubtitleConfig(languages=["fr"], sources=sources)), store=store, http=http,
                 dropin_dir=tmp_path / "none")
    assert [s.name for s in app.subtitles.sources] == ["opensubtitles"] and app.subtitles.languages == ["fr"]

    unregister_subtitles("plugin:test-subs")
    app = CueApp(Config(), store=store, http=http, dropin_dir=tmp_path / "none")
    assert app.subtitles is None  # nothing set up: no subtitle search at all
