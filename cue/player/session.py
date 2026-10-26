"""Playback session: plays a queue of episodes and keeps everything in step.

* seeks past AniSkip intervals when the position enters one (auto-skip),
* writes the position to SQLite every few seconds and on exit, for resume,
* loads the server's preferred-language subtitles, with optional OpenSubtitles fallback,
* moves to the next episode on ``eof-reached`` when auto-next is on.

Each episode gets its own player process, started at the saved resume position.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any

from cue.player.base import Player, PlayerError
from cue.providers.base import Episode, Provider, ProviderError, Show, Stream, is_local_file
from cue.selection import select_quality
from cue.skip import KIND_NAMES, SkipEngine, SkipInterval
from cue.store import Store
from cue.subtitles import SubtitleError, SubtitleService, SubtitleTrack
from cue.subtitles.provider import ProviderSubtitles, language_code
from cue.util import format_time

log = logging.getLogger(__name__)

MANUAL_SKIP = 85.0  # seconds jumped by the `s` key
SAVE_INTERVAL = 5.0  # seconds of playback between progress writes
WATCHED_AT = 0.9  # share of the duration that counts as watched
RESUME_MIN = 5.0  # resume points closer to the start than this are ignored


@dataclass
class PlaybackState:
    show: Show
    episode: Episode | None = None
    position: float | None = None
    duration: float | None = None
    skip: str = ""
    subtitles: str = ""
    next_up: Episode | None = None
    message: str = ""
    error: str | None = None
    finished: bool = False


class PlaybackSession:
    def __init__(
        self,
        *,
        provider_name: str,
        provider: Provider,
        show: Show,
        queue: Sequence[Episode],
        index: int = 0,
        store: Store,
        player_factory: Callable[[], Player],
        skip: SkipEngine | None = None,
        subtitles: SubtitleService | None = None,
        provider_subtitles: ProviderSubtitles | None = None,
        subtitle_languages: Sequence[str] = ("en",),
        auto_subtitles: bool = True,
        auto_skip: Callable[[], bool] = lambda: True,
        auto_next: bool = True,
        quality: str = "best",
        player_args: Sequence[str] = (),
    ) -> None:
        self.provider_name = provider_name
        self.provider = provider
        self.show = show
        self.queue = list(queue)
        self.index = index
        self.store = store
        self.skip = skip
        self.subtitles = subtitles
        self.provider_subtitles = provider_subtitles
        self.subtitle_languages = list(subtitle_languages) or ["en"]
        self.auto_subtitles = auto_subtitles
        self.auto_next = auto_next
        self.quality = quality
        self.player_args = list(player_args)
        self.state = PlaybackState(show)
        self.listener: Callable[[PlaybackState], None] | None = None
        self._player_factory = player_factory
        self._auto_skip = auto_skip
        self._actions: asyncio.Queue[tuple[int | None, str]] = asyncio.Queue()
        self._player: Player | None = None
        self._stream: Stream | None = None
        self._generation = 0
        self._intervals: list[SkipInterval] = []
        self._skipped: set[SkipInterval] = set()
        self._last_saved = 0.0
        self._last_second = -1
        self._loaded = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._provider_tracks: list[SubtitleTrack] = []
        self._offered_tracks: dict[int, int] = {}
        self._subtitle_revision = 0
        self._subtitle_command_lock = asyncio.Lock()

    # -- controls ------------------------------------------------------------

    def next(self) -> None:
        if self.index + 1 >= len(self.queue):
            self._message("No next episode")
        else:
            self._actions.put_nowait((None, "next"))

    def previous(self) -> None:
        if self.index <= 0:
            self._message("No previous episode")
        else:
            self._actions.put_nowait((None, "prev"))

    def replay(self) -> None:
        self._actions.put_nowait((None, "replay"))

    def stop(self) -> None:
        self._actions.put_nowait((None, "stop"))

    async def manual_skip(self) -> None:
        if await self._send("seek", MANUAL_SKIP, "relative"):
            self._message(f"Skipped {MANUAL_SKIP:.0f}s")

    async def cycle_subtitles(self) -> None:
        await self._send("cycle", "sub")

    async def subtitle_choices(self) -> list[SubtitleTrack]:
        if self.state.episode is None:
            return []
        generation = self._generation
        tracks = list(self._provider_tracks)
        if self.subtitles is not None:
            try:
                tracks.extend(await self.subtitles.search(
                    self.show, self.state.episode, file_path=self._local_file()
                ))
            except SubtitleError:
                if not tracks:
                    raise
        if generation != self._generation:
            raise SubtitleError("Episode changed; open the subtitle picker again")
        self._offered_tracks.update((id(track), generation) for track in tracks)
        return tracks

    async def use_subtitle(self, track: SubtitleTrack) -> None:
        if self.state.episode is None:
            return
        generation = self._generation
        loaded = self._loaded
        if self._offered_tracks.get(id(track)) != generation:
            raise SubtitleError("Episode changed; open the subtitle picker again")
        self._subtitle_revision += 1  # A manual choice wins over pending automatic loading.
        revision = self._subtitle_revision
        if track.source == "provider" and self.provider_subtitles is not None:
            path = await self.provider_subtitles.fetch(track)
        elif self.subtitles is not None:
            path = await self.subtitles.choose(self.provider_name, self.show, self.state.episode, track)
        else:
            raise SubtitleError("This subtitle source is unavailable")
        if generation != self._generation or revision != self._subtitle_revision:
            return
        await loaded.wait()
        if generation != self._generation or revision != self._subtitle_revision:
            return
        async with self._subtitle_command_lock:
            if generation != self._generation or revision != self._subtitle_revision:
                return
            success = await self._send("sub-add", path, "select")
        if generation != self._generation or revision != self._subtitle_revision:
            return
        if success:
            self.state.subtitles = f"{track.language}: {track.label}"
            self._message("Subtitles changed")
        else:
            raise SubtitleError("The player could not load this subtitle file")

    def kill(self) -> None:
        """Last-resort cleanup when the app exits mid-playback."""
        if self._player is not None:
            self._player.kill()

    # -- main loop -----------------------------------------------------------

    async def run(self) -> None:
        try:
            while 0 <= self.index < len(self.queue):
                action = await self._play(self.queue[self.index])
                if action == "next" or (action == "eof" and self.auto_next and self.index + 1 < len(self.queue)):
                    self.index += 1
                elif action == "prev":
                    self.index -= 1
                else:
                    break
        finally:
            self._cancel_tasks()
            await self._close_player()
            self.state.finished = True
            self._emit()

    async def _play(self, episode: Episode) -> str:
        self._generation += 1
        generation = self._generation
        self._provider_tracks = []
        self._offered_tracks.clear()
        self._stream = None
        self._subtitle_revision += 1
        self.state.episode = episode
        self.state.next_up = self.queue[self.index + 1] if self.index + 1 < len(self.queue) else None
        self.state.position = self.state.duration = None
        self.state.skip = self.state.subtitles = ""
        self._intervals, self._skipped = [], set()
        self._loaded = asyncio.Event()
        self._message(f"Loading episode {episode.label}…")

        try:
            stream = select_quality(await self.provider.stream(episode), self.quality)
        except ProviderError as exc:
            return self._fail(str(exc))
        if stream is None:
            return self._fail("No playable stream for this episode")
        self._stream = stream
        if self.provider_subtitles is not None:
            self._provider_tracks = self.provider_subtitles.choices(stream)
        start = self._resume_point(episode)

        player = self._player_factory()
        player.observe("time-pos", lambda value: self._on_time(generation, value))
        player.observe("duration", lambda value: self._on_duration(generation, value))
        player.observe("eof-reached", lambda value: value and self._actions.put_nowait((generation, "eof")))
        player.observe("error", lambda value: self._on_player_error(generation, value))
        player.on_close(lambda: self._actions.put_nowait((generation, "closed")))
        self._player = player
        try:
            await player.launch(
                stream.url,
                start=start,
                headers=stream.headers,
                title=self.show.title + ("" if self.show.media_type == "movie" else f" — Episode {episode.label}") + (
                    f" [{self.provider.language.upper()}]" if getattr(self.provider, "language", None) else ""
                ),
                extra_args=self.player_args,
            )
        except PlayerError as exc:
            self._player = None
            return self._fail(str(exc))

        self._last_saved = start or 0.0
        self._message(f"Resumed at {format_time(start)}" if start else "")
        self._spawn(self._load_skip(generation, episode))
        self._spawn(self._load_subtitles(generation, episode, stream))

        while True:
            source, action = await self._actions.get()
            if source is not None and source != generation:
                continue  # left over from an earlier episode's player
            if action != "replay":
                break
            self._skipped.clear()
            if await self._send("seek", 0, "absolute"):
                self._message("Replaying")

        self._cancel_tasks()
        self._record(episode, finished=action == "eof")
        await self._close_player()
        return action

    def _fail(self, message: str) -> str:
        self.state.error = message
        self._message(message)
        return "error"

    def _resume_point(self, episode: Episode) -> float | None:
        entry = self.store.get_history(self.provider_name, self.show.id)
        if entry and entry.last_episode == episode.number and entry.position >= RESUME_MIN:
            return entry.position
        return None

    def _record(self, episode: Episode, *, finished: bool) -> None:
        position, duration = self.state.position, self.state.duration
        if finished or (position and duration and position >= duration * WATCHED_AT):
            self.store.mark_watched(self.provider_name, self.show.id, episode.number)
            self.store.save_progress(self.provider_name, self.show.id, self.show.title, episode.number, 0.0)
        elif position:
            self.store.save_progress(self.provider_name, self.show.id, self.show.title, episode.number, position)

    async def _close_player(self) -> None:
        player, self._player = self._player, None
        if player is not None:
            await player.quit()

    # -- player callbacks ------------------------------------------------------

    def _on_player_error(self, generation: int, value: str | None) -> None:
        if generation != self._generation or not value:
            return
        self._fail(str(value))
        self._actions.put_nowait((generation, "error"))

    def _on_time(self, generation: int, value: float | None) -> None:
        if generation != self._generation or value is None or self.state.episode is None:
            return
        self.state.position = value
        self._loaded.set()
        self._apply_skips(value)
        if abs(value - self._last_saved) >= SAVE_INTERVAL:
            self._last_saved = value
            self.store.save_progress(
                self.provider_name, self.show.id, self.show.title, self.state.episode.number, value
            )
        if int(value) != self._last_second:
            self._last_second = int(value)
            self._emit()

    def _on_duration(self, generation: int, value: float | None) -> None:
        if generation == self._generation:
            self.state.duration = value
            self._emit()

    def _apply_skips(self, position: float) -> None:
        if not self._auto_skip() or self._player is None:
            return
        for interval in self._intervals:
            if interval not in self._skipped and interval.start <= position < interval.end - 1:
                self._skipped.add(interval)
                self._spawn(self._skip_past(interval))

    async def _skip_past(self, interval: SkipInterval) -> None:
        text = f"Skipped {KIND_NAMES.get(interval.kind, interval.kind)}"
        if await self._send("seek", interval.end, "absolute"):
            await self._send("show-text", text, 2000)
            self._message(text)

    # -- background lookups ----------------------------------------------------

    async def _load_skip(self, generation: int, episode: Episode) -> None:
        if self.skip is None:
            return
        if getattr(self.provider, "skip_supported", True) is False:
            self.state.skip = "no skip data for this provider"
            self._emit()
            return
        self.state.skip = "looking up skip times…"
        self._emit()
        try:
            info = await self.skip.lookup(self.provider_name, self.show, episode)
        except Exception as exc:  # skipping is a bonus; never let it break playback
            log.warning("Skip lookup failed: %s", exc)
            summary, intervals = "no skip data (lookup failed)", []
        else:
            summary, intervals = info.summary, info.intervals
        if generation != self._generation:
            return
        self._intervals = intervals
        self.state.skip = summary
        if self.state.position is not None:
            self._apply_skips(self.state.position)
        self._emit()

    async def _load_subtitles(self, generation: int, episode: Episode, stream: Stream) -> None:
        if not self.auto_subtitles:
            self.state.subtitles = "automatic loading off · Shift+C to choose"
            self._emit()
            return
        revision = self._subtitle_revision
        loaded = self._loaded
        tracks = list(self._provider_tracks)
        await loaded.wait()  # sub-add needs a loaded file
        if generation != self._generation or revision != self._subtitle_revision:
            return
        self.state.subtitles = "loading server subtitles…" if tracks else "checking subtitles…"
        self._emit()
        found: list[tuple[str, str]] = []
        failures: list[str] = []
        if self.provider_subtitles is not None:
            for preference in self.subtitle_languages:
                preferred = language_code(preference)
                candidates = [track for track in tracks if self._language_matches(track.language, preferred)]
                candidates.sort(key=lambda track: language_code(track.language) != preferred)
                for track in candidates:
                    try:
                        path = await self.provider_subtitles.fetch(track)
                    except SubtitleError as exc:
                        log.warning("Provider subtitle failed: %s", exc)
                        failures.append(str(exc))
                        continue
                    if not any(path == previous for _, previous in found):
                        found.append((track.language, path))
                    break
        if generation != self._generation or revision != self._subtitle_revision:
            return
        # A server track is tied to this exact stream. Search the optional service
        # only when no preferred server subtitle could be loaded.
        try:
            if not found and self.subtitles is not None:
                self.state.subtitles = "searching OpenSubtitles…"
                self._emit()
                found = await self.subtitles.ensure(
                    self.provider_name, self.show, episode, file_path=self._local_file(stream)
                )
        except Exception as exc:
            log.warning("Subtitle lookup failed: %s", exc)
            failures.append(str(exc))
        if generation != self._generation or revision != self._subtitle_revision:
            return
        for position, (_language, path) in enumerate(found):
            async with self._subtitle_command_lock:
                if generation != self._generation or revision != self._subtitle_revision:
                    return
                success = await self._send("sub-add", path, "select" if position == 0 else "auto")
            if generation != self._generation or revision != self._subtitle_revision:
                return
            if not success:
                self.state.subtitles = "player could not load subtitles · Shift+C to retry"
                self._emit()
                return
        if found:
            self.state.subtitles = ", ".join(language for language, _ in found)
        elif failures:
            self.state.subtitles = f"unavailable ({failures[0]}) · Shift+C to choose"
        elif tracks:
            self.state.subtitles = "preferred language unavailable · Shift+C to choose another"
        else:
            self.state.subtitles = "no external tracks found (embedded subtitles may be available in VLC)"
        self._emit()

    @staticmethod
    def _language_matches(value: str, preference: str) -> bool:
        value = language_code(value)
        preference = language_code(preference)
        if not value or not preference:
            return False
        # A generic preference such as 'en' accepts regional variants. A regional
        # preference accepts the generic language, but not a different region.
        return value == preference or value == preference.split("-", 1)[0] or (
            "-" not in preference and value.split("-", 1)[0] == preference
        )

    # -- helpers -------------------------------------------------------------

    def _local_file(self, stream: Stream | None = None) -> str | None:
        stream = stream or self._stream
        return stream.url if stream is not None and is_local_file(stream.url) else None

    async def _send(self, *command: Any) -> bool:
        player = self._player
        if player is None:
            return False
        try:
            await player.command(*command)
        except PlayerError as exc:
            log.info("Player command %s failed: %s", command[0], exc)
            return False
        return True

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _cancel_tasks(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    def _message(self, text: str) -> None:
        self.state.message = text
        self._emit()

    def _emit(self) -> None:
        if self.listener is not None:
            try:
                self.listener(self.state)
            except Exception:
                log.exception("Playback listener failed")
