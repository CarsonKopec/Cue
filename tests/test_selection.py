from __future__ import annotations

import pytest

from cue.providers.base import Stream
from cue.selection import SelectionError, parse_episode_range, select_quality

EPISODES = [1.0, 2.0, 3.0, 4.0, 5.0, 5.5, 6.0, 7.0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3", [3.0]),
        ("1-3", [1.0, 2.0, 3.0]),
        ("1 3", [1.0, 2.0, 3.0]),
        (" 2 - 4 ", [2.0, 3.0, 4.0]),
        ("-1", [7.0]),
        ("6--1", [6.0, 7.0]),
        ("5-6", [5.0, 5.5, 6.0]),  # ranges follow the list, so specials come along
        ("5.5", [5.5]),
        ("3-1", [1.0, 2.0, 3.0]),
        ("1-2, 7, 2", [1.0, 2.0, 7.0]),
    ],
)
def test_parse_episode_range(text: str, expected: list[float]) -> None:
    assert parse_episode_range(text, EPISODES) == expected


@pytest.mark.parametrize("text", ["9", "abc", "1-", "", " , "])
def test_parse_episode_range_rejects(text: str) -> None:
    with pytest.raises(SelectionError):
        parse_episode_range(text, EPISODES)


def test_select_quality() -> None:
    streams = [Stream("a", quality="720p"), Stream("b", quality="1080p"), Stream("c", quality="480p")]
    assert select_quality(streams, "best").url == "b"
    assert select_quality(streams, "worst").url == "c"
    assert select_quality(streams, "720").url == "a"
    assert select_quality(streams, "720p").url == "a"
    assert select_quality(streams, "360").url == "b"  # missing: fall back to best
    assert select_quality([Stream("x")], "best").url == "x"
    assert select_quality([], "best") is None
