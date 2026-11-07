"""Search groups: Movies, Shows, Anime and your own, each made of providers.

A provider says which groups it belongs to with a ``groups`` class attribute, and
``groups = [...]`` in its ``[providers.<name>]`` table overrides that, so one provider
can be in any number of groups. A search in a group asks all of its providers at once.

A provider that returns both movies and series (``mixed_media = True``, telling them
apart by ``Show.media_type``) shows only movies in a group whose ``media`` is
``"movie"`` and only series where it is ``"series"``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from cue.providers.base import Show

ALL = "all"
NAME = re.compile(r"[a-z][a-z0-9_-]{0,39}")
# name, title, media: the groups every Cue has, in this order (empty ones are hidden from Tab).
BUILT_IN = (("movies", "Movies", "movie"), ("shows", "Shows", "series"), ("anime", "Anime", None))


@dataclass(frozen=True)
class Group:
    name: str
    title: str
    media: str | None = None  # "movie" or "series": what mixed providers show here
    providers: tuple[str, ...] = ()
    built_in: bool = False

    def shows(self, provider: object, results: Iterable[Show]) -> list[Show]:
        """``results`` from ``provider`` that belong in this group."""
        if self.media is None or not getattr(provider, "mixed_media", False):
            return list(results)
        return [show for show in results if (show.media_type == "movie") == (self.media == "movie")]


def provider_groups(cls: type | None, table: Mapping[str, Any]) -> list[str]:
    """The groups a provider is in: its config's ``groups``, else its class's."""
    configured = table.get("groups")
    if isinstance(configured, list):
        return [str(name) for name in configured]
    return list(getattr(cls, "groups", ()) or ())


def slug(title: str) -> str:
    """A group name from a title: ``"Kids' TV"`` -> ``"kids-tv"``."""
    name = re.sub(r"[^a-z0-9]+", "-", title.lower().replace("'", "")).strip("-")
    if name and not name[0].isalpha():
        name = f"g-{name}"
    return name[:40]


def definitions(custom: Mapping[str, Mapping[str, Any]], referenced: Iterable[str] = ()) -> list[Group]:
    """Every group, without members: the built-in ones, then ``[groups.*]`` and any other
    name a provider lists, ordered by title. ``[groups.<name>]`` may retitle a built-in one."""
    groups = []
    for name, title, media in BUILT_IN:
        table = custom.get(name, {})
        groups.append(Group(name, str(table.get("title") or title), _media(table, media), built_in=True))
    known = {group.name for group in groups}
    extra = {name: custom.get(name, {}) for name in [*custom, *referenced] if name not in known and name != ALL}
    for name in sorted(extra, key=lambda key: str(extra[key].get("title") or key).lower()):
        groups.append(Group(name, str(extra[name].get("title") or name.replace("-", " ").title()),
                            _media(extra[name], None)))
    return groups


def _media(table: Mapping[str, Any], default: str | None) -> str | None:
    media = table.get("media", default)
    return media if media in ("movie", "series") else None


def build(
    providers: Mapping[str, object],
    tables: Mapping[str, Mapping[str, Any]],
    custom: Mapping[str, Mapping[str, Any]],
) -> list[Group]:
    """The groups to search, for the ready ``providers``: each with at least one provider,
    then All when it adds something (several groups, or a provider in none)."""
    memberships = {name: provider_groups(type(provider), tables.get(name, {})) for name, provider in providers.items()}
    referenced = [group for names in memberships.values() for group in names]
    result = []
    for group in definitions(custom, referenced):
        members = tuple(name for name in providers if group.name in memberships[name])
        if members:
            result.append(Group(group.name, group.title, group.media, members, group.built_in))
    ungrouped = any(not names for names in memberships.values())
    if providers and (len(result) != 1 or ungrouped):
        result.append(Group(ALL, "All", None, tuple(providers), built_in=True))
    return result


def single(provider: str) -> Group:
    """A group of one provider, for ``cue -p <provider>``."""
    return Group(f"provider:{provider}", provider, None, (provider,))
