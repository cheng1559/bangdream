from __future__ import annotations

import argparse
import json
import os
import random
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence


LANE_COUNT = 7
DEFAULT_ADB = os.environ.get("ADB", "adb")
DEFAULT_SERIAL = "127.0.0.1:7555"
DIFFICULTIES = ("easy", "normal", "hard", "expert", "special")

# Calibrated from adb-screen.png in this repo: 1920x1080, judge-line centers.
DEFAULT_LANE_X_RATIOS = (
    296 / 1920,
    518 / 1920,
    740 / 1920,
    960 / 1920,
    1182 / 1920,
    1404 / 1920,
    1624 / 1920,
)
DEFAULT_TAP_Y_RATIO = 887 / 1080
DEFAULT_FLICK_DISTANCE_RATIO = 150 / 1080
DEFAULT_FLICK_DURATION_MS = 85
DEFAULT_FLICK_LEAD_MS = 0
DEFAULT_SLIDE_LEAD_MS = 0
DEFAULT_SLIDE_STEP_MS = 5
DEFAULT_TAP_DURATION_MS = 24
DEFAULT_SAME_LANE_RELEASE_GAP_MS = 8
DEFAULT_TIMELINE_ADJUST_MS = 10
DEFAULT_PLAYBACK_CHUNK_MS = 1000
CONTROL_POLL_SECONDS = 0.01
MIN_TAP_DURATION_MS = 15
DEFAULT_EVDEV_WORD_SIZE = 8
EVDEV_NATIVE_HELPER_REMOTE_PATH = "/data/local/tmp/bangdream_evdev_writer_x86_64"
EVDEV_NATIVE_SCHEDULE_REMOTE_PATH = "/data/local/tmp/bangdream_evdev_schedule.bin"

EV_SYN = 0
EV_KEY = 1
EV_ABS = 3
SYN_REPORT = 0
BTN_TOOL_FINGER = 325
BTN_TOUCH = 330
ABS_MT_SLOT = 47
ABS_MT_TOUCH_MAJOR = 48
ABS_MT_POSITION_X = 53
ABS_MT_POSITION_Y = 54
ABS_MT_TRACKING_ID = 57
ABS_MT_PRESSURE = 58

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
ProcessFactory = Callable[[Sequence[str]], subprocess.Popen[str]]
PlaybackControlsFactory = Callable[[bool], "PlaybackControls"]


class AdbError(RuntimeError):
    pass


class PlaybackReset(RuntimeError):
    pass


class PlaybackStop(RuntimeError):
    pass


@dataclass(frozen=True)
class AutoClickConfig:
    song_id: int
    difficulty: str
    serial: str = DEFAULT_SERIAL
    timing_noise_ms: float = 0.0
    position_noise_px: float = 0.0
    dynamic_adjust: bool = False
    ignore_last_note: bool = False

    def to_namespace(self) -> argparse.Namespace:
        return argparse.Namespace(
            song_id=self.song_id,
            difficulty=self.difficulty,
            serial=self.serial,
            timing_noise_ms=self.timing_noise_ms,
            position_noise_px=self.position_noise_px,
            dynamic_adjust=self.dynamic_adjust,
            ignore_last_note=self.ignore_last_note,
        )


@dataclass(frozen=True)
class TapNote:
    lane: int
    beat: float
    source_index: int
    flick: bool = False
    direction: str = ""
    width: float = 1.0


@dataclass(frozen=True)
class TapPosition:
    x: int
    y: int


@dataclass(frozen=True)
class HoldNote:
    start_lane: float
    start_beat: float
    end_lane: float
    end_beat: float
    source_index: int
    end_flick: bool = False
    connections: tuple["LongConnection", ...] = ()


@dataclass(frozen=True)
class LongConnection:
    lane: float
    beat: float
    flick: bool = False


@dataclass(frozen=True)
class BpmEvent:
    beat: float
    bpm: float
    time: float


@dataclass(frozen=True)
class TimedTap:
    note: TapNote
    position: TapPosition
    offset: float
    end_position: TapPosition | None = None


@dataclass(frozen=True)
class TimedHold:
    note: HoldNote
    start_position: TapPosition
    end_position: TapPosition
    offset: float
    duration: float
    moves: tuple["TimedHoldMove", ...] = ()


@dataclass(frozen=True)
class TimedHoldMove:
    position: TapPosition
    offset: float


@dataclass(frozen=True)
class TimedTail:
    note: HoldNote
    position: TapPosition
    offset: float
    arrive_offset: float
    flick: bool = False


@dataclass(frozen=True)
class TimedActionGroup:
    taps: tuple[TimedTap, ...]
    holds: tuple[TimedHold, ...]
    tails: tuple[TimedTail, ...]
    offset: float


@dataclass(frozen=True)
class PlayPlan:
    taps: tuple[TimedTap, ...]
    holds: tuple[TimedHold, ...]
    tails: tuple[TimedTail, ...]
    first_beat: float
    first_time: float


@dataclass(frozen=True)
class TouchDevice:
    path: str
    max_x: int
    max_y: int
    has_slot: bool = False
    has_tracking_id: bool = False
    has_btn_tool_finger: bool = False
    name: str = ""
    orientation: int = 0


@dataclass(frozen=True)
class TouchLifecycleEvent:
    offset: float
    action: str
    contact_id: str
    position: TapPosition
    order: int


@dataclass(frozen=True)
class TimedEvdevFrame:
    offset: float
    events: tuple[tuple[int, int, int], ...]


class PlaybackControls:
    def __init__(
        self,
        adjust_step: float = DEFAULT_TIMELINE_ADJUST_MS / 1000.0,
        enable_timeline_adjust: bool = False,
    ):
        self.adjust_step = adjust_step
        self.enable_timeline_adjust = enable_timeline_adjust
        self._start_event = threading.Event()
        self._reset_event = threading.Event()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._timeline_adjust = 0.0
        self._listener: Any = None
        self._fallback_start = False

    def __enter__(self) -> "PlaybackControls":
        try:
            from pynput import keyboard
        except Exception as exc:
            print(f"Global keyboard listener unavailable ({exc}); only Space start is available.")
            self._fallback_start = True
            return self

        def on_press(key: Any) -> None:
            if key == keyboard.Key.space:
                self._start_event.set()
                return
            char = getattr(key, "char", None)
            if char is None:
                return
            char = char.lower()
            if char == "r":
                self._reset_event.set()
                return
            if not self.enable_timeline_adjust:
                return
            if char == "w":
                self.adjust_timeline(-self.adjust_step)
                return
            if char == "s":
                self.adjust_timeline(self.adjust_step)

        self._listener = keyboard.Listener(on_press=on_press)
        self._listener.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self._listener is not None:
            self._listener.stop()

    def wait_for_start(self) -> None:
        controls = "r resets"
        if self.enable_timeline_adjust:
            controls += ", w/s adjust timing"
        print(f"Ready. Press Space to start. During playback: {controls}.")
        if self._fallback_start:
            wait_for_terminal_space()
        else:
            self._start_event.wait()
            self._start_event.clear()
        if self._stop_event.is_set():
            raise PlaybackStop()
        self._reset_event.clear()
        with self._lock:
            self._timeline_adjust = 0.0

    def request_start_for_tests(self) -> None:
        self._start_event.set()

    def request_start(self) -> None:
        self._start_event.set()

    def request_reset(self) -> None:
        self._reset_event.set()

    def request_stop(self) -> None:
        self._stop_event.set()
        self._start_event.set()

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def reset_requested(self) -> bool:
        return self._reset_event.is_set()

    def consume_reset(self) -> bool:
        if not self._reset_event.is_set():
            return False
        self._reset_event.clear()
        return True

    def timeline_adjust(self) -> float:
        with self._lock:
            return self._timeline_adjust

    def adjust_timeline(self, delta: float) -> None:
        with self._lock:
            self._timeline_adjust += delta
            current = self._timeline_adjust
        direction = "earlier" if delta < 0 else "later"
        print(f"Timeline {direction} by {abs(delta) * 1000:.0f}ms; total adjustment={current * 1000:+.0f}ms")


def run_subprocess(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, check=False, text=True)


def open_evdev_native_writer(
    adb_path: str,
    serial: str,
    helper_path: str,
    device_path: str,
    schedule_path: str,
    process_factory: ProcessFactory | None = None,
) -> subprocess.Popen[bytes]:
    command = [adb_path, "-s", serial, "shell", "-T", helper_path, device_path, schedule_path]
    if process_factory is not None:
        return process_factory(command)  # type: ignore[return-value]
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=False,
        bufsize=0,
    )


def chart_path_for(song_id: int, difficulty: str) -> Path:
    return Path("charts") / difficulty / f"{song_id}.json"


def load_chart(song_id: int, difficulty: str) -> list[dict[str, Any]]:
    path = chart_path_for(song_id, difficulty)
    with path.open("r", encoding="utf-8") as file:
        chart = json.load(file)
    if not isinstance(chart, list):
        raise ValueError("Chart JSON top-level value must be a list.")
    return chart


def is_chart_file_not_found(exc: FileNotFoundError, song_id: int, difficulty: str) -> bool:
    if exc.filename is None:
        return False
    missing_path = Path(exc.filename)
    expected_path = chart_path_for(song_id, difficulty)
    return os.path.normcase(os.path.abspath(missing_path)) == os.path.normcase(
        os.path.abspath(expected_path)
    )


def chart_file_not_found_message(song_id: int, difficulty: str) -> str:
    return f"Error: chart file not found: {chart_path_for(song_id, difficulty)}."


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_lane(value: Any, note_type: str, index: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{note_type} note at index {index} has invalid lane: {value!r}")
    if value < 0 or value >= LANE_COUNT:
        raise ValueError(f"{note_type} note at index {index} lane must be 0..6: {value}")
    return value


def _validate_connection_lane(
    value: Any,
    note_type: str,
    index: int,
    allow_edge_control_point: bool = False,
) -> float:
    if not _number(value):
        raise ValueError(f"{note_type} note at index {index} has invalid lane: {value!r}")
    lane = float(value)
    minimum = -0.5 if allow_edge_control_point else 0.0
    maximum = (LANE_COUNT - 1) + (0.5 if allow_edge_control_point else 0.0)
    if lane < minimum or lane > maximum:
        raise ValueError(f"{note_type} note at index {index} lane must be 0..6: {value}")
    return lane


def _validate_beat(value: Any, note_type: str, index: int) -> float:
    if not _number(value):
        raise ValueError(f"{note_type} note at index {index} has invalid beat: {value!r}")
    return float(value)


def _validate_direction(value: Any, note_type: str, index: int) -> str:
    if value not in {"Left", "Right"}:
        raise ValueError(f"{note_type} note at index {index} has invalid direction: {value!r}")
    return str(value)


def _validate_directional_width(value: Any, note_type: str, index: int) -> float:
    if value is None:
        return 1.0
    if not _number(value):
        raise ValueError(f"{note_type} note at index {index} has invalid width: {value!r}")
    width = float(value)
    if width <= 0:
        raise ValueError(f"{note_type} note at index {index} width must be positive: {value}")
    return width


def is_flick_like_tap(note: TapNote) -> bool:
    return note.flick or bool(note.direction)


def iter_single_notes(chart: Sequence[dict[str, Any]]) -> list[TapNote]:
    notes: list[TapNote] = []

    for index, item in enumerate(chart):
        if not isinstance(item, dict):
            continue
        if item.get("type") != "Single":
            continue

        notes.append(
            TapNote(
                lane=_validate_lane(item.get("lane"), "Single", index),
                beat=_validate_beat(item.get("beat"), "Single", index),
                source_index=index,
                flick=bool(item.get("flick")),
            )
        )

    return notes


def iter_tap_notes(chart: Sequence[dict[str, Any]]) -> list[TapNote]:
    notes: list[TapNote] = []

    for index, item in enumerate(chart):
        if not isinstance(item, dict):
            continue
        note_type = item.get("type")
        if note_type == "Single":
            notes.append(
                TapNote(
                    lane=_validate_lane(item.get("lane"), "Single", index),
                    beat=_validate_beat(item.get("beat"), "Single", index),
                    source_index=index,
                    flick=bool(item.get("flick")),
                )
            )
        elif note_type == "Directional":
            notes.append(
                TapNote(
                    lane=_validate_lane(item.get("lane"), "Directional", index),
                    beat=_validate_beat(item.get("beat"), "Directional", index),
                    source_index=index,
                    direction=_validate_direction(item.get("direction"), "Directional", index),
                    width=_validate_directional_width(item.get("width"), "Directional", index),
                )
            )

    return notes


def iter_long_notes(chart: Sequence[dict[str, Any]]) -> list[HoldNote]:
    notes: list[HoldNote] = []

    for index, item in enumerate(chart):
        if not isinstance(item, dict):
            continue
        note_type = item.get("type")
        if note_type not in {"Long", "Slide"}:
            continue

        connections = item.get("connections")
        if not isinstance(connections, list) or len(connections) < 2:
            raise ValueError(f"{note_type} note at index {index} must have at least two connections.")
        parsed_connections: list[LongConnection] = []
        for connection_index, connection in enumerate(connections):
            if not isinstance(connection, dict):
                raise ValueError(f"{note_type} note at index {index} connections must be objects.")
            parsed_connections.append(
                LongConnection(
                    lane=_validate_connection_lane(
                        connection.get("lane"),
                        f"{note_type} connection {connection_index}",
                        index,
                        allow_edge_control_point=bool(connection.get("hidden")),
                    ),
                    beat=_validate_beat(
                        connection.get("beat"),
                        f"{note_type} connection {connection_index}",
                        index,
                    ),
                    flick=bool(connection.get("flick")),
                )
            )

        start = parsed_connections[0]
        end = parsed_connections[-1]
        start_beat = start.beat
        end_beat = end.beat
        if end_beat <= start_beat:
            raise ValueError(
                f"{note_type} note at index {index} must end after it starts: "
                f"{start_beat:g} -> {end_beat:g}"
            )
        for previous, current in zip(parsed_connections, parsed_connections[1:]):
            if current.beat <= previous.beat:
                raise ValueError(
                    f"{note_type} note at index {index} connections must be beat-ascending: "
                    f"{previous.beat:g} -> {current.beat:g}"
                )

        notes.append(
            HoldNote(
                start_lane=start.lane,
                start_beat=start_beat,
                end_lane=end.lane,
                end_beat=end_beat,
                source_index=index,
                end_flick=end.flick,
                connections=tuple(parsed_connections),
            )
        )

    return notes


def build_bpm_events(chart: Sequence[dict[str, Any]]) -> list[BpmEvent]:
    raw_events: list[tuple[float, float]] = []

    for index, item in enumerate(chart):
        if not isinstance(item, dict) or item.get("type") != "BPM":
            continue

        beat = item.get("beat", 0)
        bpm = item.get("bpm")
        if not _number(beat) or not _number(bpm) or bpm <= 0:
            raise ValueError(f"BPM event at index {index} is invalid: beat={beat!r}, bpm={bpm!r}")
        raw_events.append((float(beat), float(bpm)))

    raw_events.sort(key=lambda event: event[0])
    if not raw_events:
        raw_events.append((0.0, 120.0))
    elif raw_events[0][0] > 0:
        raw_events.insert(0, (0.0, raw_events[0][1]))

    events: list[BpmEvent] = []
    elapsed = 0.0
    for index, (beat, bpm) in enumerate(raw_events):
        events.append(BpmEvent(beat=beat, bpm=bpm, time=elapsed))
        next_event = raw_events[index + 1] if index + 1 < len(raw_events) else None
        if next_event is not None:
            next_beat = next_event[0]
            elapsed += ((next_beat - beat) * 60.0) / bpm

    return events


def beat_to_seconds(beat: float, bpm_events: Sequence[BpmEvent]) -> float:
    current = bpm_events[0] if bpm_events else BpmEvent(beat=0.0, bpm=120.0, time=0.0)
    for event in bpm_events:
        if event.beat <= beat:
            current = event
        else:
            break
    return current.time + ((beat - current.beat) * 60.0) / current.bpm


def build_play_plan(
    chart: Sequence[dict[str, Any]],
    screen_size: tuple[int, int],
    flick_lead_seconds: float = 0.0,
    slide_lead_seconds: float = DEFAULT_SLIDE_LEAD_MS / 1000.0,
) -> PlayPlan:
    tap_notes = iter_tap_notes(chart)
    long_notes = iter_long_notes(chart)
    if not tap_notes and not long_notes:
        raise ValueError("No Single, Directional, Long, or Slide notes found.")

    bpm_events = build_bpm_events(chart)
    first_beat = min(
        [note.beat for note in tap_notes]
        + [note.start_beat for note in long_notes]
    )
    first_time = beat_to_seconds(first_beat, bpm_events)

    taps = tuple(
        TimedTap(
            note=note,
            position=tap_position_for_note(note, screen_size),
            offset=max(
                0.0,
                beat_to_seconds(note.beat, bpm_events)
                - first_time
                - (flick_lead_seconds if is_flick_like_tap(note) else 0.0),
            ),
            end_position=directional_end_position(
                tap_position_for_note(note, screen_size),
                note.direction,
                note.width,
                screen_size,
            )
            if note.direction
            else None,
        )
        for note in tap_notes
    )
    holds = tuple(
        _timed_hold_for_long(
            note,
            screen_size,
            bpm_events,
            first_time,
            flick_lead_seconds=flick_lead_seconds,
            slide_lead_seconds=slide_lead_seconds,
        )
        for note in long_notes
    )
    tails = tuple(
        _timed_tail_for_long(
            note,
            screen_size,
            bpm_events,
            first_time,
            flick_lead_seconds=flick_lead_seconds,
            slide_lead_seconds=slide_lead_seconds,
        )
        for note in long_notes
    )

    return PlayPlan(
        taps=tuple(sorted(taps, key=lambda tap: (tap.offset, tap.note.source_index))),
        holds=tuple(sorted(holds, key=lambda hold: (hold.offset, hold.note.source_index))),
        tails=tuple(sorted(tails, key=lambda tail: (tail.offset, tail.note.source_index))),
        first_beat=first_beat,
        first_time=first_time,
    )


def _timed_hold_for_long(
    note: HoldNote,
    screen_size: tuple[int, int],
    bpm_events: Sequence[BpmEvent],
    first_time: float,
    flick_lead_seconds: float = 0.0,
    slide_lead_seconds: float = DEFAULT_SLIDE_LEAD_MS / 1000.0,
) -> TimedHold:
    start_seconds = beat_to_seconds(note.start_beat, bpm_events)
    end_seconds = beat_to_seconds(note.end_beat, bpm_events)
    offset = max(0.0, start_seconds - first_time)
    hold_until = end_seconds - first_time
    if note.end_flick:
        hold_until = effective_tail_offset(
            note,
            bpm_events,
            first_time,
            flick_lead_seconds=flick_lead_seconds,
        )
    moves = tuple(
        TimedHoldMove(
            position=position_for_lane(connection.lane, screen_size),
            offset=slide_arrive_offset(
                offset,
                beat_to_seconds(connection.beat, bpm_events) - first_time,
                slide_lead_seconds,
            ),
        )
        for connection in note.connections[1:-1]
        if offset < beat_to_seconds(connection.beat, bpm_events) - first_time < hold_until
    )

    return TimedHold(
        note=note,
        start_position=position_for_lane(note.start_lane, screen_size),
        end_position=position_for_lane(note.end_lane, screen_size),
        offset=offset,
        duration=max(0.001, hold_until - offset),
        moves=moves,
    )


def _timed_tail_for_long(
    note: HoldNote,
    screen_size: tuple[int, int],
    bpm_events: Sequence[BpmEvent],
    first_time: float,
    flick_lead_seconds: float = 0.0,
    slide_lead_seconds: float = DEFAULT_SLIDE_LEAD_MS / 1000.0,
) -> TimedTail:
    tail_offset = effective_tail_offset(
        note,
        bpm_events,
        first_time,
        flick_lead_seconds=flick_lead_seconds,
    )
    start_offset = max(0.0, beat_to_seconds(note.start_beat, bpm_events) - first_time)
    end_offset = max(0.0, beat_to_seconds(note.end_beat, bpm_events) - first_time)
    arrive_offset = slide_arrive_offset(start_offset, end_offset, slide_lead_seconds)
    if note.end_flick:
        arrive_offset = min(arrive_offset, tail_offset)
    return TimedTail(
        note=note,
        position=position_for_lane(note.end_lane, screen_size),
        offset=tail_offset,
        arrive_offset=arrive_offset,
        flick=note.end_flick,
    )


def slide_arrive_offset(start_offset: float, target_offset: float, slide_lead_seconds: float) -> float:
    if target_offset <= start_offset:
        return target_offset
    return max(start_offset + 0.001, target_offset - max(0.0, slide_lead_seconds))


def effective_tail_offset(
    note: HoldNote,
    bpm_events: Sequence[BpmEvent],
    first_time: float,
    flick_lead_seconds: float = 0.0,
) -> float:
    start_offset = max(0.0, beat_to_seconds(note.start_beat, bpm_events) - first_time)
    end_offset = max(0.0, beat_to_seconds(note.end_beat, bpm_events) - first_time)
    if not note.end_flick:
        return end_offset
    return max(start_offset + 0.001, end_offset - flick_lead_seconds)


def group_timed_actions(
    taps: Sequence[TimedTap],
    holds: Sequence[TimedHold],
    tails: Sequence[TimedTail],
    chord_window: float = 0.003,
) -> list[TimedActionGroup]:
    events = [
        ("tap", tap.offset, tap.note.source_index, tap)
        for tap in taps
    ]
    events.extend(
        ("hold", hold.offset, hold.note.source_index, hold)
        for hold in holds
    )
    events.extend(
        ("tail", tail.offset, tail.note.source_index, tail)
        for tail in tails
    )
    events.sort(key=lambda event: (event[1], event[2], event[0]))

    groups: list[TimedActionGroup] = []
    current_taps: list[TimedTap] = []
    current_holds: list[TimedHold] = []
    current_tails: list[TimedTail] = []
    group_offset: float | None = None

    for event_type, offset, _source_index, event in events:
        if group_offset is None:
            group_offset = offset
        elif offset - group_offset > chord_window:
            groups.append(
                TimedActionGroup(
                    taps=tuple(current_taps),
                    holds=tuple(current_holds),
                    tails=tuple(current_tails),
                    offset=group_offset,
                )
            )
            current_taps = []
            current_holds = []
            current_tails = []
            group_offset = offset

        if event_type == "tap":
            current_taps.append(event)  # type: ignore[arg-type]
        elif event_type == "hold":
            current_holds.append(event)  # type: ignore[arg-type]
        else:
            current_tails.append(event)  # type: ignore[arg-type]

    if group_offset is not None:
        groups.append(
            TimedActionGroup(
                taps=tuple(current_taps),
                holds=tuple(current_holds),
                tails=tuple(current_tails),
                offset=group_offset,
            )
        )

    return groups


def parse_wm_size(output: str) -> tuple[int, int]:
    matches = re.findall(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", output)
    if not matches:
        raise ValueError(f"Could not parse adb wm size output: {output!r}")
    width, height = matches[-1]
    return int(width), int(height)


def tap_position_for_note(
    note: TapNote,
    screen_size: tuple[int, int],
) -> TapPosition:
    return position_for_lane(note.lane, screen_size)


def position_for_lane(
    lane: float,
    screen_size: tuple[int, int],
) -> TapPosition:
    width, height = screen_size
    lane = max(0.0, min(float(lane), LANE_COUNT - 1))
    left_lane = int(lane)
    right_lane = min(left_lane + 1, LANE_COUNT - 1)
    progress = lane - left_lane
    left_x = width * DEFAULT_LANE_X_RATIOS[left_lane]
    right_x = width * DEFAULT_LANE_X_RATIOS[right_lane]
    x = round(left_x + (right_x - left_x) * progress)
    y = round(height * DEFAULT_TAP_Y_RATIO)
    return TapPosition(x=x, y=y)


def _ensure_success(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode == 0:
        return
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    detail = stderr or stdout or f"exit code {result.returncode}"
    raise AdbError(f"{action} failed: {detail}")


def connect_device(adb_path: str, serial: str, runner: CommandRunner) -> None:
    result = runner([adb_path, "connect", serial])
    _ensure_success(result, f"adb connect {serial}")


def should_adb_connect(serial: str) -> bool:
    if serial.startswith("emulator-"):
        return False
    return ":" in serial or re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", serial) is not None


def maybe_connect_device(adb_path: str, serial: str, runner: CommandRunner) -> None:
    if not should_adb_connect(serial):
        print(f"Skipping adb connect for serial {serial}; using existing adb device.")
        return
    print(f"Connecting adb device {serial} ...")
    connect_device(adb_path, serial, runner)


def query_device_abi(adb_path: str, serial: str, runner: CommandRunner = run_subprocess) -> str:
    result = runner([adb_path, "-s", serial, "shell", "getprop", "ro.product.cpu.abi"])
    _ensure_success(result, "adb getprop ro.product.cpu.abi")
    return result.stdout.strip()


def x86_64_evdev_writer_code() -> bytes:
    return bytes.fromhex(
        "4c8b24244983fc030f8c480100004c8b6424104c8b6c24184881"
        "ec800000004989e6b801010000bf9cffffff4c89e6ba01000000"
        "4531d20f054885c00f881b0100004989c7b801010000bf9cffff"
        "ff4c89ee31d24531d20f054885c00f88020100004989c5b8e4"
        "000000bf01000000498d76380f054885c00f88ed0000004531e4"
        "b8000000004c89ef4b8d3426ba100000004c29e20f054885c0"
        "0f88d20000000f84a70000004901c44983fc1075d2498b0631"
        "d2b900ca9a3b48f7f14d8b46384901c04d8b4e404901d149"
        "81f900ca9a3b7c0a4981e900ca9a3b49ffc04d8946284d89"
        "4e30b8e6000000bf01000000be01000000498d56284531d20f"
        "054883f8fc74e24885c0787f49c746100000000049c7461800"
        "000000498b4608498946204531e4b8010000004c89ff4b8d74"
        "2610ba180000004c29e20f054885c07e3a4901c44983fc1875"
        "dbe931ffffff4d85e4752e31ffeb36bf02000000eb2fbf0300"
        "0000eb28bf04000000eb21bf05000000eb1abf06000000eb13"
        "bf07000000eb0cbf08000000eb05bf09000000b83c0000000f"
        "0590909090909090909090909090"
    )


def build_x86_64_evdev_writer_elf() -> bytes:
    code = x86_64_evdev_writer_code()
    header_size = 64
    program_header_size = 56
    code_offset = header_size + program_header_size
    base_address = 0x400000
    entry = base_address + code_offset
    file_size = code_offset + len(code)
    elf_header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        b"\x7fELF\x02\x01\x01" + b"\x00" * 9,
        2,
        62,
        1,
        entry,
        header_size,
        0,
        0,
        header_size,
        program_header_size,
        1,
        0,
        0,
        0,
    )
    program_header = struct.pack(
        "<IIQQQQQQ",
        1,
        5,
        0,
        base_address,
        base_address,
        file_size,
        file_size,
        0x1000,
    )
    return elf_header + program_header + code


def install_evdev_native_helper(
    adb_path: str,
    serial: str,
    runner: CommandRunner = run_subprocess,
) -> str:
    abi = query_device_abi(adb_path, serial, runner)
    if abi != "x86_64":
        raise AdbError(f"evdev-native currently supports x86_64 only, device ABI is {abi!r}")

    local_path = Path(tempfile.gettempdir()) / "bangdream_evdev_writer_x86_64"
    local_path.write_bytes(build_x86_64_evdev_writer_elf())
    try:
        push = runner([adb_path, "-s", serial, "push", str(local_path), EVDEV_NATIVE_HELPER_REMOTE_PATH])
        _ensure_success(push, "adb push evdev helper")
        chmod = runner([adb_path, "-s", serial, "shell", "chmod", "755", EVDEV_NATIVE_HELPER_REMOTE_PATH])
        _ensure_success(chmod, "chmod evdev helper")
    finally:
        try:
            local_path.unlink()
        except OSError:
            pass
    return EVDEV_NATIVE_HELPER_REMOTE_PATH


def build_evdev_schedule(frames: Sequence[TimedEvdevFrame]) -> bytes:
    records: list[bytes] = []
    for frame in frames:
        offset_ns = max(0, round(frame.offset * 1_000_000_000))
        for event_type, code, value in frame.events:
            records.append(struct.pack("<QHHi", offset_ns, event_type, code, value))
    return b"".join(records)


def build_release_frames(touch_device: TouchDevice) -> list[TimedEvdevFrame]:
    events: list[tuple[int, int, int]] = []
    for slot in range(10):
        events.extend(
            [
                evdev_event(EV_ABS, ABS_MT_SLOT, slot),
                evdev_event(EV_ABS, ABS_MT_TRACKING_ID, -1),
            ]
        )
    events.append(evdev_event(EV_KEY, BTN_TOUCH, 0))
    if touch_device.has_btn_tool_finger:
        events.append(evdev_event(EV_KEY, BTN_TOOL_FINGER, 0))
    events.append(evdev_event(EV_SYN, SYN_REPORT, 0))
    return [TimedEvdevFrame(offset=0.0, events=tuple(events))]


def install_evdev_native_schedule(
    adb_path: str,
    serial: str,
    frames: Sequence[TimedEvdevFrame],
    runner: CommandRunner = run_subprocess,
) -> str:
    local_path = Path(tempfile.gettempdir()) / "bangdream_evdev_schedule.bin"
    local_path.write_bytes(build_evdev_schedule(frames))
    try:
        push = runner([adb_path, "-s", serial, "push", str(local_path), EVDEV_NATIVE_SCHEDULE_REMOTE_PATH])
        _ensure_success(push, "adb push evdev schedule")
    finally:
        try:
            local_path.unlink()
        except OSError:
            pass
    return EVDEV_NATIVE_SCHEDULE_REMOTE_PATH


def query_screen_size(adb_path: str, serial: str, runner: CommandRunner) -> tuple[int, int]:
    result = runner([adb_path, "-s", serial, "shell", "wm", "size"])
    _ensure_success(result, "adb shell wm size")
    try:
        wm_size = parse_wm_size(result.stdout or "")
    except ValueError as exc:
        raise AdbError(str(exc)) from exc

    if wm_size[1] > wm_size[0]:
        display_result = runner([adb_path, "-s", serial, "shell", "dumpsys", "display"])
        if display_result.returncode == 0:
            display_size = parse_display_size(display_result.stdout or "")
            if display_size is not None:
                return display_size

    return wm_size


def query_display_orientation(adb_path: str, serial: str, runner: CommandRunner) -> int | None:
    result = runner([adb_path, "-s", serial, "shell", "dumpsys", "display"])
    if result.returncode != 0:
        return None
    return parse_display_orientation(result.stdout or "")


def parse_display_size(output: str) -> tuple[int, int] | None:
    patterns = [
        r"mOverrideDisplayInfo=.*?\breal\s+(\d+)\s+x\s+(\d+)",
        r"mViewports=.*?\bdeviceWidth=(\d+),\s*deviceHeight=(\d+)",
        r"Viewport INTERNAL:.*?\bdeviceSize=\[(\d+),\s*(\d+)\]",
        r"logicalFrame=Rect\(0,\s*0\s*-\s*(\d+),\s*(\d+)\)",
    ]
    for pattern in patterns:
        match = re.search(pattern, output, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def parse_display_orientation(output: str) -> int | None:
    patterns = [
        r"DisplayViewport\{[^}]*\borientation=(\d)",
        r"Viewport INTERNAL:.*?\borientation=(\d)",
        r"\bmCurrentOrientation=(\d)",
        r"\bSurfaceOrientation:\s*(\d)",
        r"\brotation\s+(\d)",
    ]
    for pattern in patterns:
        match = re.search(pattern, output, flags=re.IGNORECASE | re.DOTALL)
        if match:
            orientation = int(match.group(1))
            if orientation in (0, 1, 2, 3):
                return orientation
    return None


def query_touch_device(adb_path: str, serial: str, runner: CommandRunner) -> TouchDevice:
    result = runner([adb_path, "-s", serial, "shell", "getevent", "-p"])
    _ensure_success(result, "adb shell getevent -p")
    devices = parse_touch_devices(result.stdout or "")
    if not devices:
        raise AdbError("Could not find a standard multitouch input device from getevent -p.")
    return devices[0]


def parse_touch_devices(output: str) -> list[TouchDevice]:
    devices: list[TouchDevice] = []
    current_path: str | None = None
    current_name = ""
    max_x: int | None = None
    max_y: int | None = None
    has_slot = False
    has_tracking_id = False
    has_btn_tool_finger = False

    def flush_current() -> None:
        nonlocal current_path, current_name, max_x, max_y
        nonlocal has_slot, has_tracking_id, has_btn_tool_finger
        if (
            current_path
            and max_x is not None
            and max_y is not None
            and has_slot
            and has_tracking_id
        ):
            devices.append(
                TouchDevice(
                    path=current_path,
                    max_x=max_x,
                    max_y=max_y,
                    has_slot=has_slot,
                    has_tracking_id=has_tracking_id,
                    has_btn_tool_finger=has_btn_tool_finger,
                    name=current_name,
                )
            )
        current_path = None
        current_name = ""
        max_x = None
        max_y = None
        has_slot = False
        has_tracking_id = False
        has_btn_tool_finger = False

    for line in output.splitlines():
        device_match = re.search(r"add device \d+:\s+(\S+)", line)
        if device_match:
            flush_current()
            current_path = device_match.group(1)
            continue

        if current_path is None:
            continue

        name_match = re.search(r'name:\s+"([^"]+)"', line)
        if name_match:
            current_name = name_match.group(1)
            continue

        if re.search(r"\b002f\b", line, flags=re.IGNORECASE):
            has_slot = True
            continue

        if re.search(r"\b0145\b", line, flags=re.IGNORECASE):
            has_btn_tool_finger = True
            continue

        event_match = re.search(r"\b0035\b.*\bmax\s+(\d+)", line, flags=re.IGNORECASE)
        if event_match:
            max_x = int(event_match.group(1))
            continue

        event_match = re.search(r"\b0036\b.*\bmax\s+(\d+)", line, flags=re.IGNORECASE)
        if event_match:
            max_y = int(event_match.group(1))
            continue

        if re.search(r"\b0039\b", line, flags=re.IGNORECASE):
            has_tracking_id = True

    flush_current()
    return devices


def flick_end_position(position: TapPosition, flick_distance: int) -> TapPosition:
    return TapPosition(x=position.x, y=max(0, position.y - max(1, flick_distance)))


def average_lane_spacing(screen_size: tuple[int, int]) -> float:
    width, _ = screen_size
    left_x = width * DEFAULT_LANE_X_RATIOS[0]
    right_x = width * DEFAULT_LANE_X_RATIOS[-1]
    return (right_x - left_x) / (LANE_COUNT - 1)


def directional_end_position(
    position: TapPosition,
    direction: str,
    width_value: float,
    screen_size: tuple[int, int],
) -> TapPosition:
    screen_width, _ = screen_size
    sign = -1 if direction == "Left" else 1
    distance = average_lane_spacing(screen_size) * width_value
    x = clamp(round(position.x + sign * distance), 0, screen_width - 1)
    return TapPosition(x=x, y=position.y)


def next_plain_tap_offsets_by_source(taps: Sequence[TimedTap]) -> dict[int, float]:
    next_offsets: dict[int, float] = {}
    next_by_lane: dict[int, float] = {}
    for tap in sorted(taps, key=lambda item: item.offset, reverse=True):
        if not is_flick_like_tap(tap.note) and tap.note.lane in next_by_lane:
            next_offsets[tap.note.source_index] = next_by_lane[tap.note.lane]
        if not is_flick_like_tap(tap.note):
            next_by_lane[tap.note.lane] = tap.offset
    return next_offsets


def plain_tap_up_offset(
    tap: TimedTap,
    tap_duration: float,
    next_same_lane_offset: float | None,
    same_lane_release_gap: float,
) -> float:
    up_offset = tap.offset + tap_duration
    if next_same_lane_offset is None:
        return up_offset

    latest_up = next_same_lane_offset - same_lane_release_gap
    minimum_up = tap.offset + MIN_TAP_DURATION_MS / 1000.0
    target = min(up_offset, latest_up)
    if target >= minimum_up:
        return target
    if latest_up > tap.offset:
        return max(tap.offset + 0.001, latest_up)
    return tap.offset + 0.001


def build_touch_lifecycle_events(
    taps: Sequence[TimedTap],
    holds: Sequence[TimedHold],
    tails: Sequence[TimedTail],
    flick_distance: int,
    flick_duration: float,
    tap_duration: float,
    same_lane_release_gap: float = DEFAULT_SAME_LANE_RELEASE_GAP_MS / 1000.0,
    slide_step: float = DEFAULT_SLIDE_STEP_MS / 1000.0,
    timing_noise: float = 0.0,
    position_noise: float = 0.0,
    rng: random.Random | None = None,
) -> list[TouchLifecycleEvent]:
    events: list[TouchLifecycleEvent] = []
    next_same_lane_offsets = next_plain_tap_offsets_by_source(taps)

    for tap in taps:
        contact_id = f"tap:{tap.note.source_index}"
        if is_flick_like_tap(tap.note):
            end = tap.end_position if tap.note.direction and tap.end_position is not None else flick_end_position(
                tap.position,
                flick_distance,
            )
            events.extend(
                [
                    TouchLifecycleEvent(tap.offset, "down", contact_id, tap.position, tap.note.source_index * 10),
                    TouchLifecycleEvent(
                        tap.offset + flick_duration / 2,
                        "move",
                        contact_id,
                        end,
                        tap.note.source_index * 10 + 1,
                    ),
                    TouchLifecycleEvent(
                        tap.offset + flick_duration,
                        "up",
                        contact_id,
                        end,
                        tap.note.source_index * 10 + 2,
                    ),
                ]
            )
        else:
            up_offset = plain_tap_up_offset(
                tap,
                tap_duration,
                next_same_lane_offsets.get(tap.note.source_index),
                same_lane_release_gap,
            )
            events.extend(
                [
                    TouchLifecycleEvent(tap.offset, "down", contact_id, tap.position, tap.note.source_index * 10),
                    TouchLifecycleEvent(
                        up_offset,
                        "up",
                        contact_id,
                        tap.position,
                        tap.note.source_index * 10 + 1,
                    ),
                ]
            )

    tails_by_source = {tail.note.source_index: tail for tail in tails}
    for hold in holds:
        contact_id = f"hold:{hold.note.source_index}"
        tail = tails_by_source.get(hold.note.source_index)
        order_base = hold.note.source_index * 100
        events.append(
            TouchLifecycleEvent(
                hold.offset,
                "down",
                contact_id,
                hold.start_position,
                order_base,
            )
        )
        current_position = hold.start_position
        current_offset = hold.offset
        next_order = order_base + 1
        for move in hold.moves:
            slide_events = interpolated_slide_moves(
                contact_id,
                current_position,
                current_offset,
                move.position,
                move.offset,
                next_order,
                slide_step,
            )
            events.extend(slide_events)
            next_order += len(slide_events)
            current_position = move.position
            current_offset = move.offset
        if tail is not None:
            slide_events = interpolated_slide_moves(
                contact_id,
                current_position,
                current_offset,
                tail.position,
                tail.arrive_offset,
                next_order,
                slide_step,
            )
            events.extend(slide_events)
            next_order += len(slide_events)
            current_position = tail.position
            current_offset = tail.arrive_offset

        if tail is None:
            events.append(
                TouchLifecycleEvent(
                    hold.offset + hold.duration,
                    "up",
                    contact_id,
                    hold.end_position,
                    next_order,
                )
            )
            continue

        if tail.flick:
            end = flick_end_position(tail.position, flick_distance)
            events.extend(
                [
                    TouchLifecycleEvent(
                        tail.offset + flick_duration / 2,
                        "move",
                        contact_id,
                        end,
                        next_order,
                    ),
                    TouchLifecycleEvent(
                        tail.offset + flick_duration,
                        "up",
                        contact_id,
                        end,
                        next_order + 1,
                    ),
                ]
            )
        else:
            events.extend(
                [
                    TouchLifecycleEvent(tail.offset, "up", contact_id, tail.position, next_order),
                ]
            )

    if timing_noise > 0:
        events = apply_timing_noise(events, timing_noise, rng=rng)
    if position_noise > 0:
        events = apply_position_noise(events, position_noise, rng=rng)
    return sorted(events, key=lambda event: (event.offset, event.order, event.action))


def apply_timing_noise(
    events: Sequence[TouchLifecycleEvent],
    max_noise: float,
    rng: random.Random | None = None,
) -> list[TouchLifecycleEvent]:
    if max_noise <= 0:
        return list(events)

    random_source = rng if rng is not None else random
    min_offset_by_contact: dict[str, float] = {}
    for event in events:
        current = min_offset_by_contact.get(event.contact_id)
        if current is None or event.offset < current:
            min_offset_by_contact[event.contact_id] = event.offset

    jitter_by_contact = {
        contact_id: random_source.uniform(-min(max_noise, min_offset), max_noise)
        for contact_id, min_offset in min_offset_by_contact.items()
    }
    return [
        TouchLifecycleEvent(
            offset=max(0.0, event.offset + jitter_by_contact[event.contact_id]),
            action=event.action,
            contact_id=event.contact_id,
            position=event.position,
            order=event.order,
        )
        for event in events
    ]


def apply_position_noise(
    events: Sequence[TouchLifecycleEvent],
    max_noise: float,
    rng: random.Random | None = None,
) -> list[TouchLifecycleEvent]:
    if max_noise <= 0:
        return list(events)

    random_source = rng if rng is not None else random
    jitter_by_contact: dict[str, tuple[float, float]] = {}
    for event in events:
        if event.contact_id not in jitter_by_contact:
            jitter_by_contact[event.contact_id] = (
                random_source.uniform(-max_noise, max_noise),
                random_source.uniform(-max_noise, max_noise),
            )

    return [
        TouchLifecycleEvent(
            offset=event.offset,
            action=event.action,
            contact_id=event.contact_id,
            position=TapPosition(
                x=round(event.position.x + jitter_by_contact[event.contact_id][0]),
                y=round(event.position.y + jitter_by_contact[event.contact_id][1]),
            ),
            order=event.order,
        )
        for event in events
    ]


def interpolated_slide_moves(
    contact_id: str,
    start_position: TapPosition,
    start_offset: float,
    target_position: TapPosition,
    target_offset: float,
    first_order: int,
    step: float,
) -> list[TouchLifecycleEvent]:
    if target_offset <= start_offset:
        if target_position == start_position:
            return []
        return [
            TouchLifecycleEvent(
                target_offset,
                "move",
                contact_id,
                target_position,
                first_order,
            )
        ]
    if target_position == start_position:
        return []

    duration = target_offset - start_offset
    interval = max(0.001, step)
    step_count = max(1, int(duration / interval + 0.999999))
    moves: list[TouchLifecycleEvent] = []
    for index in range(1, step_count + 1):
        progress = index / step_count
        moves.append(
            TouchLifecycleEvent(
                start_offset + duration * progress,
                "move",
                contact_id,
                TapPosition(
                    x=round(start_position.x + (target_position.x - start_position.x) * progress),
                    y=round(start_position.y + (target_position.y - start_position.y) * progress),
                ),
                first_order + index - 1,
            )
        )
    return moves


def evdev_event(event_type: int, code: int, value: int) -> tuple[int, int, int]:
    return event_type, code, value


def build_evdev_frames(
    events: Sequence[TouchLifecycleEvent],
    touch_device: TouchDevice,
    screen_size: tuple[int, int],
    timing_window: float = 0.001,
) -> list[TimedEvdevFrame]:
    frames: list[TimedEvdevFrame] = []
    slot_by_contact: dict[str, int] = {}
    free_slots = list(range(10))

    for event_group in group_lifecycle_events(events, timing_window=timing_window):
        down_events = [event for event in event_group if event.action == "down"]
        move_events = [event for event in event_group if event.action == "move"]
        up_events = [event for event in event_group if event.action == "up"]

        move_frame: list[tuple[int, int, int]] = []
        for event in move_events:
            slot = slot_by_contact.get(event.contact_id)
            if slot is None:
                continue
            x, y = scale_to_touch_device(event.position, touch_device, screen_size)
            move_frame.extend(
                [
                    evdev_event(EV_ABS, ABS_MT_SLOT, slot),
                    evdev_event(EV_ABS, ABS_MT_POSITION_X, x),
                    evdev_event(EV_ABS, ABS_MT_POSITION_Y, y),
                ]
            )
        append_evdev_frame(frames, event_group[0].offset, move_frame)

        active_before_up = len(slot_by_contact)
        up_frame: list[tuple[int, int, int]] = []
        for event in up_events:
            slot = slot_by_contact.pop(event.contact_id, None)
            if slot is None:
                continue
            free_slots.append(slot)
            free_slots.sort()
            up_frame.extend(
                [
                    evdev_event(EV_ABS, ABS_MT_SLOT, slot),
                    evdev_event(EV_ABS, ABS_MT_TRACKING_ID, -1),
                ]
            )
        if up_frame and active_before_up > 0 and not slot_by_contact:
            up_frame.append(evdev_event(EV_KEY, BTN_TOUCH, 0))
            if touch_device.has_btn_tool_finger:
                up_frame.append(evdev_event(EV_KEY, BTN_TOOL_FINGER, 0))
        append_evdev_frame(frames, event_group[0].offset, up_frame)

        active_before_down = len(slot_by_contact)
        down_frame: list[tuple[int, int, int]] = []
        for event in down_events:
            if not free_slots:
                raise ValueError("Too many simultaneous contacts for the configured slot pool.")
            slot = free_slots.pop(0)
            slot_by_contact[event.contact_id] = slot
            tracking_id = slot + 1
            x, y = scale_to_touch_device(event.position, touch_device, screen_size)
            down_frame.extend(
                [
                    evdev_event(EV_ABS, ABS_MT_SLOT, slot),
                    evdev_event(EV_ABS, ABS_MT_TRACKING_ID, tracking_id),
                    evdev_event(EV_ABS, ABS_MT_POSITION_X, x),
                    evdev_event(EV_ABS, ABS_MT_POSITION_Y, y),
                ]
            )
        if down_frame and active_before_down == 0:
            down_frame.append(evdev_event(EV_KEY, BTN_TOUCH, 1))
            if touch_device.has_btn_tool_finger:
                down_frame.append(evdev_event(EV_KEY, BTN_TOOL_FINGER, 1))
        append_evdev_frame(frames, event_group[0].offset, down_frame)

    return frames


def append_evdev_frame(
    frames: list[TimedEvdevFrame],
    offset: float,
    events: list[tuple[int, int, int]],
) -> None:
    if not events:
        return
    events.append(evdev_event(EV_SYN, SYN_REPORT, 0))
    frames.append(TimedEvdevFrame(offset=offset, events=tuple(events)))


def group_lifecycle_events(
    events: Sequence[TouchLifecycleEvent],
    timing_window: float,
) -> list[tuple[TouchLifecycleEvent, ...]]:
    if not events:
        return []

    sorted_events = sorted(events, key=lambda event: (event.offset, event.order, event.action))
    groups: list[tuple[TouchLifecycleEvent, ...]] = []
    current: list[TouchLifecycleEvent] = [sorted_events[0]]
    group_offset = sorted_events[0].offset

    for event in sorted_events[1:]:
        if event.offset - group_offset <= timing_window:
            current.append(event)
            continue
        groups.append(tuple(current))
        current = [event]
        group_offset = event.offset

    groups.append(tuple(current))
    return groups


def scale_to_touch_device(
    position: TapPosition,
    touch_device: TouchDevice,
    screen_size: tuple[int, int],
) -> tuple[int, int]:
    width, height = screen_size
    if touch_device.orientation == 1:
        x = round(position.y * touch_device.max_x / max(1, height - 1))
        y = round((width - 1 - position.x) * touch_device.max_y / max(1, width - 1))
    elif touch_device.orientation == 2:
        x = round((width - 1 - position.x) * touch_device.max_x / max(1, width - 1))
        y = round((height - 1 - position.y) * touch_device.max_y / max(1, height - 1))
    elif touch_device.orientation == 3:
        x = round((height - 1 - position.y) * touch_device.max_x / max(1, height - 1))
        y = round(position.x * touch_device.max_y / max(1, width - 1))
    else:
        x = round(position.x * touch_device.max_x / max(1, width - 1))
        y = round(position.y * touch_device.max_y / max(1, height - 1))
    return clamp(x, 0, touch_device.max_x), clamp(y, 0, touch_device.max_y)


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def _evdev_process_error(process: subprocess.Popen[bytes]) -> str:
    stderr = ""
    if process.stderr is not None:
        try:
            stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
        except OSError:
            stderr = ""
    return stderr or f"exit code {process.returncode}"


def close_evdev_writer(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.terminate()


def wait_for_terminal_space() -> None:
    if sys.platform.startswith("win"):
        import msvcrt

        while True:
            key = msvcrt.getwch()
            if key == " ":
                return
            if key == "\x03":
                raise KeyboardInterrupt

    if not sys.stdin.isatty():
        input("stdin is not a TTY; press Enter to continue.")
        return

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            key = sys.stdin.read(1)
            if key == " ":
                return
            if key == "\x03":
                raise KeyboardInterrupt
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def wait_for_space() -> None:
    print("Ready. Press Space anywhere to start auto tapping.")
    try:
        from pynput import keyboard
    except Exception as exc:
        print(f"Global keyboard listener unavailable ({exc}); falling back to terminal input.")
        wait_for_terminal_space()
        return

    def on_press(key: Any) -> bool | None:
        if key == keyboard.Key.space:
            return False
        return None

    try:
        with keyboard.Listener(on_press=on_press) as listener:
            listener.join()
    except Exception as exc:
        print(f"Global keyboard listener failed ({exc}); falling back to terminal input.")
        wait_for_terminal_space()


def play_evdev_native_schedule(process: subprocess.Popen[bytes]) -> float:
    started = time.perf_counter()
    return_code = process.wait()
    if return_code:
        raise AdbError(f"evdev native scheduler failed: {_evdev_process_error(process)}")
    return time.perf_counter() - started


def play_evdev_native_resettable_schedule(
    process: subprocess.Popen[bytes],
    controls: PlaybackControls,
) -> float:
    return wait_for_evdev_process(process, controls)


def wait_for_evdev_process(
    process: subprocess.Popen[bytes],
    controls: PlaybackControls | None = None,
) -> float:
    started = time.perf_counter()
    while process.poll() is None:
        if controls is not None and controls.stop_requested():
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise PlaybackStop()
        if controls is not None and controls.reset_requested():
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise PlaybackReset()
        time.sleep(CONTROL_POLL_SECONDS)
    if process.returncode:
        raise AdbError(f"evdev native scheduler failed: {_evdev_process_error(process)}")
    return time.perf_counter() - started


def adjusted_frame_chunk(
    frames: Sequence[TimedEvdevFrame],
    start_index: int,
    elapsed: float,
    timeline_adjust: float,
    chunk_seconds: float,
) -> tuple[list[TimedEvdevFrame], int]:
    chunk: list[TimedEvdevFrame] = []
    index = start_index
    chunk_end = elapsed + chunk_seconds
    while index < len(frames):
        due = frames[index].offset + timeline_adjust
        if due > chunk_end and chunk:
            break
        if due > chunk_end:
            break
        chunk.append(
            TimedEvdevFrame(
                offset=max(0.0, due - elapsed),
                events=frames[index].events,
            )
        )
        index += 1
    return chunk, index


def wait_until_next_chunk(
    next_frame: TimedEvdevFrame,
    started: float,
    timeline_adjust: float,
    chunk_seconds: float,
    controls: PlaybackControls,
) -> None:
    while True:
        if controls.stop_requested():
            raise PlaybackStop()
        if controls.reset_requested():
            raise PlaybackReset()
        elapsed = time.perf_counter() - started
        due = next_frame.offset + timeline_adjust
        if due <= elapsed + chunk_seconds:
            return
        time.sleep(min(CONTROL_POLL_SECONDS, due - elapsed - chunk_seconds))


def send_release_events(
    adb_path: str,
    serial: str,
    helper_path: str,
    touch_device: TouchDevice,
    runner: CommandRunner,
    process_factory: ProcessFactory | None = None,
) -> None:
    schedule_path = install_evdev_native_schedule(
        adb_path,
        serial,
        build_release_frames(touch_device),
        runner,
    )
    process = open_evdev_native_writer(
        adb_path,
        serial,
        helper_path,
        touch_device.path,
        schedule_path,
        process_factory=process_factory,
    )
    play_evdev_native_schedule(process)


def play_evdev_native_interactive(
    frames: Sequence[TimedEvdevFrame],
    adb_path: str,
    serial: str,
    helper_path: str,
    touch_device: TouchDevice,
    controls: PlaybackControls,
    runner: CommandRunner,
    process_factory: ProcessFactory | None = None,
    initial_schedule_path: str | None = None,
    initial_next_index: int = 0,
) -> float:
    chunk_seconds = DEFAULT_PLAYBACK_CHUNK_MS / 1000.0
    started = time.perf_counter()
    next_index = initial_next_index

    if initial_schedule_path is not None:
        process = open_evdev_native_writer(
            adb_path,
            serial,
            helper_path,
            touch_device.path,
            initial_schedule_path,
            process_factory=process_factory,
        )
        wait_for_evdev_process(process, controls)

    while next_index < len(frames):
        wait_until_next_chunk(
            frames[next_index],
            started,
            controls.timeline_adjust(),
            chunk_seconds,
            controls,
        )
        elapsed = time.perf_counter() - started
        chunk, next_index_after_chunk = adjusted_frame_chunk(
            frames,
            next_index,
            elapsed,
            controls.timeline_adjust(),
            chunk_seconds,
        )
        if not chunk:
            continue

        schedule_path = install_evdev_native_schedule(adb_path, serial, chunk, runner)
        process = open_evdev_native_writer(
            adb_path,
            serial,
            helper_path,
            touch_device.path,
            schedule_path,
            process_factory=process_factory,
        )
        wait_for_evdev_process(process, controls)
        next_index = next_index_after_chunk

    return time.perf_counter() - started


def describe_action_groups(groups: Sequence[TimedActionGroup]) -> str:
    chord_sizes = [
        len(group.taps) + len(group.holds) + len(group.tails)
        for group in groups
        if len(group.taps) + len(group.holds) + len(group.tails) > 1
    ]
    if not chord_sizes:
        return "no simultaneous groups"
    return f"{len(chord_sizes)} simultaneous group(s), max group={max(chord_sizes)}"


def trim_hold_before_ignored_tail(hold: TimedHold, tail: TimedTail) -> TimedHold:
    release_offset = max(
        hold.offset + 0.001,
        tail.offset - DEFAULT_SAME_LANE_RELEASE_GAP_MS / 1000.0,
    )
    kept_moves = tuple(move for move in hold.moves if move.offset < release_offset)
    end_position = kept_moves[-1].position if kept_moves else hold.start_position
    return TimedHold(
        note=hold.note,
        start_position=hold.start_position,
        end_position=end_position,
        offset=hold.offset,
        duration=release_offset - hold.offset,
        moves=kept_moves,
    )


def omit_last_note_from_plan(plan: PlayPlan) -> PlayPlan:
    candidates: list[tuple[float, int, str, TimedTap | TimedTail]] = [
        (tap.offset, tap.note.source_index, "tap", tap)
        for tap in plan.taps
    ]
    candidates.extend(
        (tail.offset, tail.note.source_index, "tail", tail)
        for tail in plan.tails
    )
    if not candidates:
        return plan

    _offset, source_index, action_type, action = max(candidates, key=lambda item: (item[0], item[1], item[2]))
    if action_type == "tap":
        return PlayPlan(
            taps=tuple(tap for tap in plan.taps if tap.note.source_index != source_index),
            holds=plan.holds,
            tails=plan.tails,
            first_beat=plan.first_beat,
            first_time=plan.first_time,
        )

    ignored_tail = action
    if not isinstance(ignored_tail, TimedTail):
        return plan
    adjusted_holds = tuple(
        trim_hold_before_ignored_tail(hold, ignored_tail)
        if hold.note.source_index == source_index
        else hold
        for hold in plan.holds
    )
    return PlayPlan(
        taps=plan.taps,
        holds=adjusted_holds,
        tails=tuple(tail for tail in plan.tails if tail.note.source_index != source_index),
        first_beat=plan.first_beat,
        first_time=plan.first_time,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Auto tap Single/Directional notes and hold Long/Slide notes from a BanG Dream chart through adb."
    )
    parser.add_argument(
        "song_id",
        type=int,
        help="Bestdori song ID.",
    )
    parser.add_argument(
        "difficulty",
        choices=DIFFICULTIES,
        help="Chart difficulty.",
    )
    parser.add_argument(
        "--serial",
        default=DEFAULT_SERIAL,
        help=f"adb device serial. Defaults to {DEFAULT_SERIAL}.",
    )
    parser.add_argument(
        "--timing-noise-ms",
        type=float,
        default=0.0,
        help="Maximum random per-contact timing noise in milliseconds. Defaults to 0.",
    )
    parser.add_argument(
        "--position-noise-px",
        type=float,
        default=0.0,
        help="Maximum random per-contact position noise in screen pixels. Defaults to 0.",
    )
    parser.add_argument(
        "--dynamic-adjust",
        action="store_true",
        help="Enable runtime controls: r resets playback, w/s adjust timing. Disabled by default for best timing accuracy.",
    )
    parser.add_argument(
        "--ignore-last-note",
        action="store_true",
        help="Skip the final tap or long-tail judgment to intentionally avoid an all-perfect result.",
    )
    return parser


def config_from_args(args: argparse.Namespace) -> AutoClickConfig:
    return AutoClickConfig(
        song_id=args.song_id,
        difficulty=args.difficulty,
        serial=args.serial,
        timing_noise_ms=args.timing_noise_ms,
        position_noise_px=args.position_noise_px,
        dynamic_adjust=args.dynamic_adjust,
        ignore_last_note=args.ignore_last_note,
    )


def _resolve_screen_size(serial: str, runner: CommandRunner) -> tuple[int, int]:
    return query_screen_size(DEFAULT_ADB, serial, runner)


def _resolve_touch_device(
    serial: str,
    screen_size: tuple[int, int],
    runner: CommandRunner,
) -> TouchDevice:
    device = query_touch_device(DEFAULT_ADB, serial, runner)
    display_orientation = _query_display_orientation_for_touch(
        serial,
        screen_size,
        device.max_x,
        device.max_y,
        runner,
    )
    return TouchDevice(
        path=device.path,
        max_x=device.max_x,
        max_y=device.max_y,
        has_slot=device.has_slot,
        has_tracking_id=device.has_tracking_id,
        has_btn_tool_finger=device.has_btn_tool_finger,
        name=device.name,
        orientation=infer_touch_orientation(
            screen_size,
            device.max_x,
            device.max_y,
            display_orientation,
        ),
    )


def _touch_dimensions_are_swapped(
    screen_size: tuple[int, int],
    touch_max_x: int,
    touch_max_y: int,
) -> bool:
    width, height = screen_size
    return (
        (width > height and touch_max_x <= height and touch_max_y >= width)
        or (height > width and touch_max_x >= height and touch_max_y <= width)
    )


def _query_display_orientation_for_touch(
    serial: str,
    screen_size: tuple[int, int],
    touch_max_x: int,
    touch_max_y: int,
    runner: CommandRunner,
) -> int | None:
    if not _touch_dimensions_are_swapped(screen_size, touch_max_x, touch_max_y):
        return None
    return query_display_orientation(DEFAULT_ADB, serial, runner)


def infer_touch_orientation(
    screen_size: tuple[int, int],
    touch_max_x: int,
    touch_max_y: int,
    display_orientation: int | None = None,
) -> int:
    if display_orientation in (0, 1, 2, 3):
        if _touch_dimensions_are_swapped(screen_size, touch_max_x, touch_max_y):
            return (-display_orientation) % 4
        if display_orientation == 2:
            return 2
        return 0

    width, height = screen_size
    if width > height and touch_max_x <= height and touch_max_y >= width:
        return 3
    if height > width and touch_max_x >= height and touch_max_y <= width:
        return 1
    return 0


def execute(
    args: argparse.Namespace,
    runner: CommandRunner = run_subprocess,
    wait_for_space_fn: Callable[[], None] = wait_for_space,
    process_factory: ProcessFactory | None = None,
    controls_factory: PlaybackControlsFactory | None = None,
    reset_to_ready: bool = True,
) -> int:
    chart = load_chart(args.song_id, args.difficulty)

    maybe_connect_device(DEFAULT_ADB, args.serial, runner)

    screen_size = _resolve_screen_size(args.serial, runner)
    flick_distance = round(screen_size[1] * DEFAULT_FLICK_DISTANCE_RATIO)
    flick_duration_ms = max(1, DEFAULT_FLICK_DURATION_MS)
    flick_lead_seconds = max(0.0, DEFAULT_FLICK_LEAD_MS / 1000.0)
    slide_lead_seconds = max(0.0, DEFAULT_SLIDE_LEAD_MS / 1000.0)
    slide_step_seconds = max(0.001, DEFAULT_SLIDE_STEP_MS / 1000.0)
    flick_duration_seconds = flick_duration_ms / 1000.0
    tap_duration_seconds = max(1, DEFAULT_TAP_DURATION_MS) / 1000.0
    same_lane_release_gap_seconds = max(0.0, DEFAULT_SAME_LANE_RELEASE_GAP_MS / 1000.0)
    timing_noise_seconds = max(0.0, args.timing_noise_ms / 1000.0)
    position_noise_pixels = max(0.0, args.position_noise_px)
    plan = build_play_plan(
        chart,
        screen_size,
        flick_lead_seconds=flick_lead_seconds,
        slide_lead_seconds=slide_lead_seconds,
    )
    if args.ignore_last_note:
        plan = omit_last_note_from_plan(plan)
    taps = plan.taps
    holds = plan.holds
    tails = plan.tails
    action_groups = group_timed_actions(
        taps,
        holds,
        tails,
        chord_window=0.003,
    )

    first_group = action_groups[0]
    first_parts = []
    first_parts.extend(
        (
            f"Directional lane={tap.note.lane} beat={tap.note.beat:g} "
            f"direction={tap.note.direction} width={tap.note.width:g}"
        )
        if tap.note.direction
        else f"Single lane={tap.note.lane} beat={tap.note.beat:g}"
        for tap in first_group.taps
    )
    first_parts.extend(
        f"Long lane={hold.note.start_lane} beat={hold.note.start_beat:g}"
        for hold in first_group.holds
    )
    first_parts.extend(
        f"LongTail lane={tail.note.end_lane} beat={tail.note.end_beat:g}"
        for tail in first_group.tails
    )
    final_offset = max(
        [tap.offset for tap in taps]
        + [hold.offset + hold.duration for hold in holds]
        + [tail.offset for tail in tails]
        + [0.0]
    )
    directional_count = sum(1 for tap in taps if tap.note.direction)
    flick_count = sum(1 for tap in taps if is_flick_like_tap(tap.note)) + sum(1 for tail in tails if tail.flick)
    print(
        "First playable group: "
        f"{'; '.join(first_parts)}, "
        f"screen={screen_size[0]}x{screen_size[1]}"
    )
    print(
        f"Play plan: {len(taps)} tap note(s), "
        f"{directional_count} Directional note(s), "
        f"{len(holds)} Long note(s), "
        f"{len(tails)} Long tail(s), "
        f"{flick_count} flick action(s), "
        f"{len(action_groups)} timing group(s), "
        f"{describe_action_groups(action_groups)}, "
        f"final offset={final_offset:.3f}s"
    )

    evdev_process: subprocess.Popen[bytes] | None = None
    try:
        touch_device = _resolve_touch_device(args.serial, screen_size, runner)
        lifecycle_events = build_touch_lifecycle_events(
            taps,
            holds,
            tails,
            flick_distance=flick_distance,
            flick_duration=flick_duration_seconds,
            tap_duration=tap_duration_seconds,
            same_lane_release_gap=same_lane_release_gap_seconds,
            slide_step=slide_step_seconds,
            timing_noise=timing_noise_seconds,
            position_noise=position_noise_pixels,
        )
        evdev_frames = build_evdev_frames(lifecycle_events, touch_device, screen_size)
        print(
            "evdev-native backend: "
            f"{touch_device.path} max={touch_device.max_x}x{touch_device.max_y}, "
            f"orientation={touch_device.orientation}, "
            f"tool_finger={touch_device.has_btn_tool_finger}, "
            f"{len(evdev_frames)} event frame(s), "
            f"word_size={DEFAULT_EVDEV_WORD_SIZE}, helper=native"
        )

        evdev_native_helper = install_evdev_native_helper(DEFAULT_ADB, args.serial, runner)
        print(f"evdev-native helper is ready: {evdev_native_helper}")

        if args.dynamic_adjust and wait_for_space_fn is wait_for_space and process_factory is None:
            create_controls = controls_factory or (
                lambda enable: PlaybackControls(enable_timeline_adjust=enable)
            )
            with create_controls(True) as controls:
                while True:
                    first_chunk, first_next_index = adjusted_frame_chunk(
                        evdev_frames,
                        start_index=0,
                        elapsed=0.0,
                        timeline_adjust=0.0,
                        chunk_seconds=DEFAULT_PLAYBACK_CHUNK_MS / 1000.0,
                    )
                    first_schedule = install_evdev_native_schedule(
                        DEFAULT_ADB,
                        args.serial,
                        first_chunk,
                        runner,
                    )
                    try:
                        controls.wait_for_start()
                        elapsed = play_evdev_native_interactive(
                            evdev_frames,
                            DEFAULT_ADB,
                            args.serial,
                            evdev_native_helper,
                            touch_device,
                            controls,
                            runner,
                            process_factory=process_factory,
                            initial_schedule_path=first_schedule,
                            initial_next_index=first_next_index,
                        )
                    except PlaybackReset:
                        print("Reset requested. Releasing touches and returning to ready state.")
                        send_release_events(
                            DEFAULT_ADB,
                            args.serial,
                            evdev_native_helper,
                            touch_device,
                            runner,
                            process_factory=process_factory,
                        )
                        controls.consume_reset()
                        if not reset_to_ready:
                            return 0
                        continue
                    except PlaybackStop:
                        print("Stop requested. Releasing touches and returning to idle state.")
                        send_release_events(
                            DEFAULT_ADB,
                            args.serial,
                            evdev_native_helper,
                            touch_device,
                            runner,
                            process_factory=process_factory,
                        )
                        return 0
                    break
        elif wait_for_space_fn is wait_for_space and process_factory is None:
            create_controls = controls_factory or (
                lambda enable: PlaybackControls(enable_timeline_adjust=enable)
            )
            with create_controls(False) as controls:
                while True:
                    evdev_native_schedule = install_evdev_native_schedule(
                        DEFAULT_ADB,
                        args.serial,
                        evdev_frames,
                        runner,
                    )
                    print(
                        "evdev-native scheduler is ready: "
                        f"{evdev_native_helper}, schedule={evdev_native_schedule}"
                    )
                    try:
                        controls.wait_for_start()
                        evdev_process = open_evdev_native_writer(
                            DEFAULT_ADB,
                            args.serial,
                            evdev_native_helper,
                            touch_device.path,
                            evdev_native_schedule,
                            process_factory=process_factory,
                        )
                        elapsed = play_evdev_native_resettable_schedule(evdev_process, controls)
                    except PlaybackReset:
                        evdev_process = None
                        print("Reset requested. Releasing touches and returning to ready state.")
                        send_release_events(
                            DEFAULT_ADB,
                            args.serial,
                            evdev_native_helper,
                            touch_device,
                            runner,
                            process_factory=process_factory,
                        )
                        controls.consume_reset()
                        if not reset_to_ready:
                            return 0
                        continue
                    except PlaybackStop:
                        evdev_process = None
                        print("Stop requested. Releasing touches and returning to idle state.")
                        send_release_events(
                            DEFAULT_ADB,
                            args.serial,
                            evdev_native_helper,
                            touch_device,
                            runner,
                            process_factory=process_factory,
                        )
                        return 0
                    evdev_process = None
                    break
        else:
            evdev_native_schedule = install_evdev_native_schedule(
                DEFAULT_ADB,
                args.serial,
                evdev_frames,
                runner,
            )
            print(
                "evdev-native scheduler is ready: "
                f"{evdev_native_helper}, schedule={evdev_native_schedule}"
            )

            wait_for_space_fn()

            evdev_process = open_evdev_native_writer(
                DEFAULT_ADB,
                args.serial,
                evdev_native_helper,
                touch_device.path,
                evdev_native_schedule,
                process_factory=process_factory,
            )
            elapsed = play_evdev_native_schedule(evdev_process)

        print(
            f"Sent {len(taps)} tap(s), {len(holds)} hold(s), "
            f"and {len(tails)} long tail(s) in {elapsed:.3f}s via evdev-native."
        )
        return 0
    finally:
        if evdev_process is not None and evdev_process.poll() is None:
            close_evdev_writer(evdev_process)


def play_auto_click_chart(
    config: AutoClickConfig,
    runner: CommandRunner = run_subprocess,
    wait_for_space_fn: Callable[[], None] = wait_for_space,
    process_factory: ProcessFactory | None = None,
    controls_factory: PlaybackControlsFactory | None = None,
    reset_to_ready: bool = True,
) -> int:
    return execute(
        config.to_namespace(),
        runner=runner,
        wait_for_space_fn=wait_for_space_fn,
        process_factory=process_factory,
        controls_factory=controls_factory,
        reset_to_ready=reset_to_ready,
    )


class AutoClickChartService:
    def __init__(
        self,
        runner: CommandRunner = run_subprocess,
        process_factory: ProcessFactory | None = None,
    ):
        self.runner = runner
        self.process_factory = process_factory

    def play(
        self,
        config: AutoClickConfig,
        controls_factory: PlaybackControlsFactory | None = None,
        wait_for_space_fn: Callable[[], None] = wait_for_space,
        reset_to_ready: bool = True,
    ) -> int:
        return play_auto_click_chart(
            config,
            runner=self.runner,
            wait_for_space_fn=wait_for_space_fn,
            process_factory=self.process_factory,
            controls_factory=controls_factory,
            reset_to_ready=reset_to_ready,
        )


def run(
    argv: Sequence[str] | None = None,
    runner: CommandRunner = run_subprocess,
    wait_for_space_fn: Callable[[], None] = wait_for_space,
    process_factory: ProcessFactory | None = None,
    controls_factory: PlaybackControlsFactory | None = None,
    reset_to_ready: bool = True,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = config_from_args(args)
    try:
        return play_auto_click_chart(
            config,
            runner=runner,
            wait_for_space_fn=wait_for_space_fn,
            process_factory=process_factory,
            controls_factory=controls_factory,
            reset_to_ready=reset_to_ready,
        )
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except FileNotFoundError as exc:
        if is_chart_file_not_found(exc, args.song_id, args.difficulty):
            print(chart_file_not_found_message(args.song_id, args.difficulty), file=sys.stderr)
            return 1
        missing = exc.filename or DEFAULT_ADB
        print(
            f"Error: executable not found: {missing}. "
            "Install Android platform-tools and add adb.exe to PATH.",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, AdbError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
