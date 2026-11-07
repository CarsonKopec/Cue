"""Search groups: membership, merged searches across providers, switching groups, and the Groups tab."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import tomllib
from textual.widgets import DataTable, Input, OptionList, SelectionList, TabbedContent

from cue import providers
from cue.app import CueApp
from cue.config import ConfigError, edit_config, load_config, parse_config, remove_group, set_provider_groups
from cue.groups import ALL, Group, build, definitions, provider_groups, slug
from cue.origins import loading
from cue.providers.base import Episode, ProviderError, Show
from cue.screens.episodes import EpisodesScreen
from cue.screens.plugins import GROUPS, ConfirmDialog, GroupDialog, NewGroupDialog, PluginsScreen
from cue.screens.results import ResultsScreen
from cue.store import Store

ORIGIN = "plugin:test-groups"


def fake(name: str, groups: tuple[str, ...], titles: list[tuple[str, str]], *, mixed: bool = False,
         delay: float = 0.0, error: str | None = None) -> type:
    """A provider class whose search returns ``titles`` as (title, media_type) shows."""

    class Fake:
        searched: list[str] = []

        def __init__(self, options):
            self.options = options

        async def search(self, query):
            await asyncio.sleep(delay)
            if error:
                raise ProviderError(error)
            type(self).searched.append(query)
            return [Show(f"{name}-{i}", title, media_type=kind) for i, (title, kind) in enumerate(titles)]

        async def episodes(self, show):
            return [Episode(show.id, 1)]

        async def stream(self, episode):
            return []

    Fake.name, Fake.groups, Fake.mixed_media = name, groups, mixed
    with loading(ORIGIN):
        providers.register(Fake)
    return Fake


@pytest.fixture
def fakes():
    made = {
        "films": fake("films", ("movies", "shows"), [("Dune", "movie"), ("Dune: Prophecy", "episode")], mixed=True),
        "tube": fake("tube", ("shows",), [("Dune Documentary", "episode")], delay=0.05),
        "otaku": fake("otaku", ("anime",), [("Dune Anime", "episode")]),
        "broken": fake("broken", ("shows",), [], error="site is down"),
    }
    yield made
    providers.unregister(ORIGIN)


# -- the model -----------------------------------------------------------------------


def test_membership_comes_from_the_class_or_the_config() -> None:
    class Both:
        groups = ("movies", "shows")

    assert provider_groups(Both, {}) == ["movies", "shows"]
    assert provider_groups(Both, {"groups": ["kids"]}) == ["kids"]
    assert provider_groups(None, {}) == [] and provider_groups(Both, {"groups": []}) == []


def test_groups_from_ready_providers() -> None:
    class A:
        groups = ("movies", "shows")

    class B:
        groups = ("shows", "kids")

    class C:
        pass

    ready = {"a": A(), "b": B()}
    groups = build(ready, {}, {"kids": {"title": "Kids"}})
    assert [(g.name, g.providers) for g in groups] == [
        ("movies", ("a",)), ("shows", ("a", "b")), ("kids", ("b",)), (ALL, ("a", "b")),
    ]
    assert [g.title for g in groups] == ["Movies", "Shows", "Kids", "All"]
    # One group holding every provider needs no All; a provider in no group does.
    assert [g.name for g in build({"a": B()}, {"a": {"groups": ["shows"]}}, {})] == ["shows"]
    assert [g.name for g in build({"a": A(), "c": C()}, {}, {})] == ["movies", "shows", ALL]
    assert build({}, {}, {}) == []


def test_definitions_titles_media_and_order() -> None:
    groups = definitions({"shows": {"title": "TV"}, "zoo": {}, "kids": {"title": "Kids", "media": "series"}},
                         ["docs", "kids"])
    assert [(g.name, g.title, g.media, g.built_in) for g in groups] == [
        ("movies", "Movies", "movie", True), ("shows", "TV", "series", True), ("anime", "Anime", None, True),
        ("docs", "Docs", None, False), ("kids", "Kids", "series", False), ("zoo", "Zoo", None, False),
    ]
    assert slug("Kids' TV!") == "kids-tv" and slug("4K Films") == "g-4k-films"


def test_mixed_providers_are_split_between_movies_and_shows() -> None:
    class Mixed:
        mixed_media = True

    class Plain:
        pass

    shows = [Show("1", "A Movie", media_type="movie"), Show("2", "A Series")]
    movies, series, anything = Group("movies", "Movies", "movie"), Group("shows", "Shows", "series"), Group("x", "X")
    assert [s.title for s in movies.shows(Mixed(), shows)] == ["A Movie"]
    assert [s.title for s in series.shows(Mixed(), shows)] == ["A Series"]
    assert len(anything.shows(Mixed(), shows)) == 2 and len(movies.shows(Plain(), shows)) == 2


def test_group_config_is_checked_and_edited(tmp_path: Path) -> None:
    config = parse_config({"default_group": "Kids", "groups": {"kids": {"title": "Kids", "media": "series"}},
                           "providers": {"a": {"groups": ["kids", "shows"]}}})
    assert config.default_group == "kids" and config.groups["kids"]["title"] == "Kids"
    for bad, message in (({"groups": {"Bad Name": {}}}, "lowercase"),
                         ({"groups": {"kids": {"media": "film"}}}, "media"),
                         ({"providers": {"a": {"groups": "kids"}}}, "groups")):
        with pytest.raises(ConfigError, match=message):
            parse_config(bad)

    path = tmp_path / "config.toml"
    path.write_text('default_group = "kids"\n\n[providers.a]\ngroups = ["kids", "shows"]\n\n'
                    '[groups.kids]\ntitle = "Kids"\n', encoding="utf-8")
    config = edit_config(path, remove_group("kids"), set_provider_groups("b", ["movies"], exists=False))
    assert config.groups == {} and config.default_group == ""
    assert config.providers["a"]["groups"] == ["shows"]
    assert config.providers["b"] == {"groups": ["movies"], "enabled": False}  # not turned on by grouping


# -- searching a group --------------------------------------------------------------------


def _app(tmp_path: Path, store: Store, config_text: str, **options) -> CueApp:
    path = tmp_path / "config.toml"
    path.write_text(config_text, encoding="utf-8")
    return CueApp(
        load_config(path), store=store,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(404))),
        dropin_dir=tmp_path / "no-dropins", plugin_root=tmp_path / "no-plugins", **options,
    )


ALL_FAKES = "[providers.films]\n\n[providers.tube]\n\n[providers.otaku]\n\n[providers.broken]\n"


def _titles(screen: ResultsScreen) -> list[str]:
    return [str(screen.query_one("#results", OptionList).get_option_at_index(i).prompt)
            for i in range(screen.query_one("#results", OptionList).option_count)]


async def _search(app: CueApp, pilot, query: str) -> ResultsScreen:
    app.open_results(query)
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()
    assert isinstance(app.screen, ResultsScreen)
    return app.screen


async def test_a_group_search_merges_its_providers(fakes, store: Store, tmp_path: Path) -> None:
    app = _app(tmp_path, store, ALL_FAKES)
    async with app.run_test(size=(110, 30)) as pilot:
        assert [g.name for g in app.groups] == ["movies", "shows", "anime", ALL]
        assert app.group.name == "movies"
        screen = await _search(app, pilot, "dune")
        assert _titles(screen) == ["Dune  · movie"]  # films' series stay out of Movies; one provider: no tag
        await pilot.press("q")

        await pilot.press("escape", "tab")  # Movies -> Shows
        assert app.group.name == "shows"
        screen = await _search(app, pilot, "dune")
        titles = _titles(screen)
        assert "Dune: Prophecy  [films]" in titles and "Dune Documentary  [tube]" in titles
        assert not any("· movie" in title for title in titles)
        hint = str(screen.query_one("#results-hint").render())
        assert "broken: site is down" in hint  # a failing provider doesn't hide the others

        screen.query_one("#filter", Input).value = "documentary"
        await pilot.pause()
        assert _titles(screen) == ["Dune Documentary  [tube]"]
        screen.query_one("#results", OptionList).focus()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, EpisodesScreen) and app.screen.provider_name == "tube"


async def test_choosing_groups_and_single_providers(fakes, store: Store, tmp_path: Path) -> None:
    app = _app(tmp_path, store, 'default_group = "anime"\n' + ALL_FAKES)
    async with app.run_test(size=(110, 30)) as pilot:
        assert app.group.name == "anime"
        await pilot.press("escape", "tab")
        assert app.group.name == ALL and "All" in str(app.screen.query_one("StatusBar").render())

    app = _app(tmp_path, store, ALL_FAKES, provider="tube")
    async with app.run_test(size=(110, 30)) as pilot:
        assert (app.group.title, app.group.providers) == ("tube", ("tube",))
        screen = await _search(app, pilot, "dune")
        assert _titles(screen) == ["Dune Documentary"]

    app = _app(tmp_path, store, ALL_FAKES, group="shows")
    async with app.run_test(size=(110, 30)):
        assert app.group.name == "shows"

    # Older configs name a default provider: Cue starts in that provider's first group.
    app = _app(tmp_path, store, 'default_provider = "otaku"\n' + ALL_FAKES)
    async with app.run_test(size=(110, 30)):
        assert app.group.name == "anime"


# -- the Groups tab ----------------------------------------------------------------------------


async def test_groups_tab_edits_membership_and_makes_groups(fakes, store: Store, tmp_path: Path) -> None:
    app = _app(tmp_path, store, ALL_FAKES)
    config = tmp_path / "config.toml"
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("escape", "p")
        screen = app.screen
        assert isinstance(screen, PluginsScreen)
        screen.query_one(TabbedContent).active = GROUPS
        await pilot.pause()
        names = [g.name for g in screen._group_rows]
        assert names[:3] == ["movies", "shows", "anime"] and names[-1] == ALL

        # Put otaku in Shows as well as Anime.
        screen.query_one("#group-table", DataTable).move_cursor(row=names.index("shows"))
        await pilot.press("enter")
        dialog = app.screen
        assert isinstance(dialog, GroupDialog)
        members = dialog.query_one(SelectionList)
        # Built-in Jellyfin and Local are in Shows by default too, though not set up here.
        assert set(members.selected) == {"films", "tube", "broken", "jellyfin", "local"}
        members.select("otaku")
        await pilot.press("ctrl+s")
        await pilot.pause()
        saved = tomllib.loads(config.read_text(encoding="utf-8"))
        assert saved["providers"]["otaku"]["groups"] == ["anime", "shows"]
        assert "otaku" in next(g for g in app.groups if g.name == "shows").providers

        await pilot.press("d")
        assert tomllib.loads(config.read_text(encoding="utf-8"))["default_group"] == "shows"
        assert app.group.name == "shows"

        await pilot.press("n")
        assert isinstance(app.screen, NewGroupDialog)
        app.screen.query_one(Input).value = "Kids' TV"
        await pilot.press("enter")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, GroupDialog)
        dialog.query_one(SelectionList).select("tube")
        await pilot.press("ctrl+s")
        await pilot.pause()
        saved = tomllib.loads(config.read_text(encoding="utf-8"))
        assert saved["groups"]["kids-tv"]["title"] == "Kids' TV"
        assert saved["providers"]["tube"]["groups"] == ["shows", "kids-tv"]
        assert any(g.name == "kids-tv" and g.providers == ("tube",) for g in app.groups)

        rows = [g.name for g in screen._group_rows]
        screen.query_one("#group-table", DataTable).move_cursor(row=rows.index("movies"))
        await pilot.press("x")
        assert isinstance(app.screen, PluginsScreen)  # built-in groups can't be removed
        screen.query_one("#group-table", DataTable).move_cursor(row=rows.index("kids-tv"))
        await pilot.press("x")
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("y")
        await pilot.pause()
        saved = tomllib.loads(config.read_text(encoding="utf-8"))
        assert "groups" not in saved and saved["providers"]["tube"]["groups"] == ["shows"]
        assert not any(g.name == "kids-tv" for g in app.groups)
