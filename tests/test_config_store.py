from __future__ import annotations

from pathlib import Path

import pytest

from cue.config import ConfigError, config_path, load_config, parse_config
from cue.store import Store


def test_first_run_writes_default_config(cue_home: Path) -> None:
    config = load_config()
    assert config_path() == cue_home / "config.toml"
    assert config_path().exists()
    assert config.default_provider == "jellyfin"
    assert config.auto_skip and config.auto_next
    assert config.player == "vlc"
    assert config.player_args == []
    assert config.providers == {"jellyfin": {"url": "", "api_key": ""}, "local": {"path": ""}}
    assert config.subtitles.languages == ["en"]
    assert config.subtitles.opensubtitles_api_key == ""


def test_reads_existing_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'default_provider = "local"\nauto_skip = false\nquality = 720\n'
        '[providers.local]\npath = "D:/Media/Shows"\n'
        '[subtitles]\nlanguages = ["EN", "de"]\n[subtitles.opensubtitles]\napi_key = "k"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.default_provider == "local"
    assert config.auto_skip is False
    assert config.quality == "720"
    assert config.providers == {"local": {"path": "D:/Media/Shows"}}
    assert config.subtitles.languages == ["en", "de"]
    assert config.subtitles.opensubtitles_api_key == "k"


def test_player_selection_and_legacy_config() -> None:
    assert parse_config({"player": "mpv"}).player == "mpv"
    assert parse_config({"player": "VLC"}).player == "vlc"
    # Old configs did not name a player; keep their arguments for the backend
    # to translate while using the new VLC default.
    config = parse_config({"player_args": ["--fs"]})
    assert config.player == "vlc" and config.player_args == ["--fs"]
    with pytest.raises(ConfigError, match="player must"):
        parse_config({"player": "unknown"})
    with pytest.raises(ConfigError, match="player has the wrong type"):
        parse_config({"player": 1})


def test_bad_config_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("auto_skip = [", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
    with pytest.raises(ConfigError, match="auto_skip"):
        parse_config({"auto_skip": "yes"})
    with pytest.raises(ConfigError, match="player_args"):
        parse_config({"player_args": [1]})


def test_history_and_continue_watching(store: Store) -> None:
    store.save_progress("local", "a", "Show A", 1, 100.0)
    store.save_progress("local", "b", "Show B", 3, 0.0)
    store.save_progress("local", "a", "Show A", 2, 50.0)
    entry = store.get_history("local", "a")
    assert entry is not None and (entry.last_episode, entry.position) == (2, 50.0)
    assert {e.show_id for e in store.continue_watching()} == {"a", "b"}
    assert store.get_history("local", "missing") is None


def test_watched_mal_skip_and_subtitles(store: Store) -> None:
    store.mark_watched("jellyfin", "s", 1)
    store.mark_watched("jellyfin", "s", 2.5)
    assert store.watched("jellyfin", "s") == {1.0, 2.5}

    assert store.get_mal_id("jellyfin", "s") is None
    store.set_mal_id("jellyfin", "s", 52991)
    assert store.get_mal_id("jellyfin", "s") == 52991

    store.set_skip_times(52991, 1, [("ed", 1300.0, 1390.0), ("op", 10.0, 100.0)])
    assert store.get_skip_times(52991, 1) == [("op", 10.0, 100.0), ("ed", 1300.0, 1390.0)]
    store.set_skip_times(52991, 1, [("op", 12.0, 101.0)])
    assert store.get_skip_times(52991, 1) == [("op", 12.0, 101.0)]

    store.set_subtitle("jellyfin", "s", 1, "en", "opensubtitles:9", "C:/subs/1.en.srt")
    cached = store.get_subtitle("jellyfin", "s", 1, "en")
    assert cached is not None and cached.path == "C:/subs/1.en.srt" and cached.source == "opensubtitles:9"


def test_store_creates_its_folder(tmp_path: Path) -> None:
    db = Store(tmp_path / "nested" / "cue.db")
    db.save_progress("p", "s", "t", 1, 1.0)
    db.close()
    assert (tmp_path / "nested" / "cue.db").exists()
