import contextlib
import io
import json
import os
import random
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from app import auto_click_chart as clicker


class FakeRunner:
    def __init__(self):
        self.commands = []

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)

        if command[:2] == ["adb", "connect"]:
            return subprocess.CompletedProcess(command, 0, stdout="connected\n", stderr="")
        if command[-3:] == ["shell", "wm", "size"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="Physical size: 1920x1080\n",
                stderr="",
            )
        if command[-3:] == ["shell", "getprop", "ro.product.cpu.abi"]:
            return subprocess.CompletedProcess(command, 0, stdout="x86_64\n", stderr="")
        if command[-3:] == ["shell", "getevent", "-p"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "add device 1: /dev/input/event6\n"
                    "  name: \"touchscreen\"\n"
                    "    KEY (0001): 0145 014a\n"
                    "    002f  : value 0, min 0, max 9, fuzz 0, flat 0, resolution 0\n"
                    "    0035  : value 0, min 0, max 1919, fuzz 0, flat 0, resolution 0\n"
                    "    0036  : value 0, min 0, max 1079, fuzz 0, flat 0, resolution 0\n"
                    "    0039  : value 0, min 0, max 65535, fuzz 0, flat 0, resolution 0\n"
                ),
                stderr="",
            )
        if command[-5:-2] == ["shell", "input", "tap"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[-6:-5] == ["swipe"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if len(command) >= 4 and command[3] == "push":
            return subprocess.CompletedProcess(command, 0, stdout="pushed\n", stderr="")
        if command[-4:-2] == ["shell", "chmod"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if len(command) >= 5 and command[-2] == "shell" and "& wait" in command[-1]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if len(command) >= 5 and command[-2] == "shell" and "input swipe" in command[-1]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        return subprocess.CompletedProcess(command, 99, stdout="", stderr="unexpected command")


class FakeStdin:
    def __init__(self):
        self.parts = []
        self.closed = False

    def write(self, value):
        self.parts.append(value)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeShellProcess:
    def __init__(self, command):
        self.command = list(command)
        self.stdin = FakeStdin()
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self.returncode = -15


class FakeProcessFactory:
    def __init__(self):
        self.processes = []

    def __call__(self, command):
        process = FakeShellProcess(command)
        self.processes.append(process)
        return process


@contextlib.contextmanager
def temporary_cwd(path):
    original = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(original)


def write_local_chart(root, song_id, difficulty, chart):
    path = Path(root) / "charts" / difficulty / f"{song_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(chart), encoding="utf-8")
    return path


class AutoClickChartTests(unittest.TestCase):
    def test_iter_long_notes_from_repo_chart(self):
        chart = [
            {
                "type": "Long",
                "connections": [
                    {"lane": 3, "beat": 168},
                    {"lane": 3, "beat": 174},
                ],
            }
        ]

        longs = clicker.iter_long_notes(chart)

        self.assertEqual(len(longs), 1)
        self.assertEqual(longs[0].start_lane, 3)
        self.assertEqual(longs[0].start_beat, 168)
        self.assertEqual(longs[0].end_lane, 3)
        self.assertEqual(longs[0].end_beat, 174)

    def test_iter_long_notes_preserves_tail_flick(self):
        chart = [
            {
                "type": "Long",
                "connections": [
                    {"lane": 2, "beat": 1},
                    {"lane": 2, "beat": 3, "flick": True},
                ],
            },
        ]

        longs = clicker.iter_long_notes(chart)

        self.assertTrue(longs[0].end_flick)

    def test_iter_long_notes_preserves_slide_connections(self):
        chart = [
            {
                "type": "Long",
                "connections": [
                    {"lane": 1, "beat": 1},
                    {"lane": 3, "beat": 2},
                    {"lane": 5, "beat": 3},
                ],
            },
        ]

        longs = clicker.iter_long_notes(chart)

        self.assertEqual([connection.lane for connection in longs[0].connections], [1, 3, 5])
        self.assertEqual([connection.beat for connection in longs[0].connections], [1, 2, 3])

    def test_iter_slide_notes_allows_fractional_hidden_lanes(self):
        chart = [
            {
                "type": "Slide",
                "connections": [
                    {"lane": 0, "beat": 8},
                    {"lane": 0.18, "beat": 8.0625, "hidden": True},
                    {"lane": 1.02, "beat": 8.6875, "hidden": True},
                    {"lane": 2, "beat": 9},
                ],
            },
        ]

        notes = clicker.iter_long_notes(chart)

        self.assertEqual([connection.lane for connection in notes[0].connections], [0.0, 0.18, 1.02, 2.0])

    def test_iter_slide_notes_allows_hidden_edge_control_lanes(self):
        chart = [
            {
                "type": "Slide",
                "connections": [
                    {"lane": 6, "beat": 1},
                    {"lane": 6.5, "beat": 2, "hidden": True},
                    {"lane": 6, "beat": 3},
                ],
            },
        ]

        notes = clicker.iter_long_notes(chart)

        self.assertEqual([connection.lane for connection in notes[0].connections], [6.0, 6.5, 6.0])

    def test_iter_slide_notes_rejects_visible_edge_control_lanes(self):
        chart = [
            {
                "type": "Slide",
                "connections": [
                    {"lane": 6, "beat": 1},
                    {"lane": 6.5, "beat": 2},
                    {"lane": 6, "beat": 3},
                ],
            },
        ]

        with self.assertRaises(ValueError):
            clicker.iter_long_notes(chart)

    def test_iter_long_notes_treats_slide_as_hold_path(self):
        chart = [
            {
                "type": "Slide",
                "connections": [
                    {"lane": 6, "beat": 47.5},
                    {"lane": 4, "beat": 48.5},
                    {"lane": 5, "beat": 49},
                ],
            },
        ]

        notes = clicker.iter_long_notes(chart)

        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].start_lane, 6)
        self.assertEqual(notes[0].end_lane, 5)
        self.assertEqual([connection.lane for connection in notes[0].connections], [6, 4, 5])

    def test_iter_tap_notes_includes_directional(self):
        chart = [
            {"type": "Single", "lane": 2, "beat": 1},
            {"type": "Directional", "lane": 5, "beat": 2, "direction": "Left", "width": 2},
        ]

        notes = clicker.iter_tap_notes(chart)

        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[1].lane, 5)
        self.assertEqual(notes[1].direction, "Left")
        self.assertEqual(notes[1].width, 2)

    def test_default_tap_position_matches_calibrated_screenshot(self):
        note = clicker.TapNote(lane=5, beat=12, source_index=2)

        position = clicker.tap_position_for_note(note, (1920, 1080))

        self.assertEqual(position, clicker.TapPosition(x=1404, y=887))

    def test_fractional_lane_position_interpolates_between_centers(self):
        self.assertEqual(
            clicker.position_for_lane(0.5, (1920, 1080)),
            clicker.TapPosition(x=407, y=887),
        )

    def test_directional_end_position_uses_width_as_lane_spacing(self):
        start = clicker.TapPosition(x=960, y=887)

        self.assertEqual(
            clicker.directional_end_position(start, "Right", 2, (1920, 1080)),
            clicker.TapPosition(x=1403, y=887),
        )

    def test_parse_wm_size_prefers_override_size_when_present(self):
        output = "Physical size: 1920x1080\nOverride size: 1600x900\n"

        self.assertEqual(clicker.parse_wm_size(output), (1600, 900))

    def test_parse_display_size_prefers_landscape_override(self):
        output = (
            'mBaseDisplayInfo=DisplayInfo{"Built-in Screen", real 1080 x 1920}\n'
            'mOverrideDisplayInfo=DisplayInfo{"Built-in Screen", real 1920 x 1080}\n'
        )

        self.assertEqual(clicker.parse_display_size(output), (1920, 1080))

    def test_parse_display_orientation_prefers_active_viewport(self):
        output = (
            "mViewports=[DisplayViewport{type=INTERNAL, valid=true, "
            "isActive=true, orientation=1, logicalFrame=Rect(0, 0 - 1920, 1080)}]\n"
            "mOverrideDisplayInfo=DisplayInfo{rotation 1, real 1920 x 1080}\n"
        )

        self.assertEqual(clicker.parse_display_orientation(output), 1)

    def test_parse_touch_devices_requires_standard_multitouch(self):
        output = (
            "add device 1: /dev/input/event4\n"
            "  name: \"Xiaomi Input\"\n"
            "    KEY (0001): 0145 014a\n"
            "    002f  : value 1, min 0, max 31, fuzz 0, flat 0, resolution 0\n"
            "    0035  : value 0, min 0, max 1080, fuzz 0, flat 0, resolution 0\n"
            "    0036  : value 0, min 0, max 1920, fuzz 0, flat 0, resolution 0\n"
            "    0039  : value 0, min 0, max 65535, fuzz 0, flat 0, resolution 0\n"
            "add device 2: /dev/input/event7\n"
            "  name: \"BlueStacks Virtual Touch\"\n"
            "    0035  : value 0, min 0, max 32767, fuzz 0, flat 0, resolution 0\n"
            "    0036  : value 0, min 0, max 32767, fuzz 0, flat 0, resolution 0\n"
        )

        devices = clicker.parse_touch_devices(output)

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].path, "/dev/input/event4")
        self.assertEqual(devices[0].name, "Xiaomi Input")
        self.assertTrue(devices[0].has_slot)
        self.assertTrue(devices[0].has_tracking_id)
        self.assertTrue(devices[0].has_btn_tool_finger)

    def test_display_orientation_one_infers_inverse_raw_touch_rotation(self):
        self.assertEqual(
            clicker.infer_touch_orientation((1920, 1080), 1080, 1920, display_orientation=1),
            3,
        )

    def test_orientation_three_scales_landscape_to_portrait_raw_touch(self):
        position = clicker.TapPosition(960, 887)
        device = clicker.TouchDevice(
            path="/dev/input/event4",
            max_x=1080,
            max_y=1920,
            has_slot=True,
            has_tracking_id=True,
            orientation=3,
        )

        self.assertEqual(
            clicker.scale_to_touch_device(position, device, (1920, 1080)),
            (192, 961),
        )

    def test_should_adb_connect_skips_emulator_serials(self):
        self.assertTrue(clicker.should_adb_connect("127.0.0.1:5555"))
        self.assertTrue(clicker.should_adb_connect("192.168.1.8"))
        self.assertFalse(clicker.should_adb_connect("emulator-5554"))

    def test_build_play_plan_includes_longs_relative_to_first_playable(self):
        chart = [
            {"type": "BPM", "bpm": 120, "beat": 0},
            {
                "type": "Long",
                "connections": [
                    {"lane": 3, "beat": 2},
                    {"lane": 3, "beat": 6},
                ],
            },
            {"type": "Single", "lane": 5, "beat": 4},
        ]

        plan = clicker.build_play_plan(chart, (1920, 1080))

        self.assertEqual(plan.first_beat, 2)
        self.assertEqual(len(plan.holds), 1)
        self.assertEqual(len(plan.tails), 1)
        self.assertEqual(len(plan.taps), 1)
        self.assertAlmostEqual(plan.holds[0].offset, 0.0)
        self.assertAlmostEqual(plan.holds[0].duration, 2.0)
        self.assertAlmostEqual(plan.tails[0].offset, 2.0)
        self.assertAlmostEqual(plan.taps[0].offset, 1.0)
        self.assertEqual(plan.holds[0].start_position, clicker.TapPosition(960, 887))

    def test_double_flicks_are_grouped_and_started_early(self):
        chart = [
            {"type": "BPM", "bpm": 60, "beat": 0},
            {"type": "Single", "lane": 0, "beat": 0},
            {"type": "Single", "lane": 5, "beat": 1, "flick": True},
            {"type": "Single", "lane": 6, "beat": 1, "flick": True},
        ]

        plan = clicker.build_play_plan(chart, (1920, 1080), flick_lead_seconds=0.05)
        groups = clicker.group_timed_actions(plan.taps, plan.holds, plan.tails)

        self.assertEqual(len(groups), 2)
        self.assertAlmostEqual(groups[1].offset, 0.95)
        self.assertEqual([tap.note.lane for tap in groups[1].taps], [5, 6])
        events = clicker.build_touch_lifecycle_events(
            groups[1].taps,
            groups[1].holds,
            groups[1].tails,
            flick_distance=150,
            flick_duration=0.045,
            tap_duration=0.024,
        )

        self.assertEqual([event.action for event in events], ["down", "down", "move", "move", "up", "up"])
        self.assertEqual(
            [event.position for event in events if event.action == "move"],
            [clicker.TapPosition(1404, 737), clicker.TapPosition(1624, 737)],
        )

    def test_long_flick_tail_shortens_hold_and_starts_tail_early(self):
        chart = [
            {"type": "BPM", "bpm": 60, "beat": 0},
            {
                "type": "Long",
                "connections": [
                    {"lane": 3, "beat": 0},
                    {"lane": 3, "beat": 2, "flick": True},
                ],
            },
        ]

        plan = clicker.build_play_plan(chart, (1920, 1080), flick_lead_seconds=0.05)

        self.assertAlmostEqual(plan.holds[0].offset, 0.0)
        self.assertAlmostEqual(plan.holds[0].duration, 1.95)
        self.assertAlmostEqual(plan.tails[0].offset, 1.95)
        self.assertAlmostEqual(plan.tails[0].arrive_offset, 1.95)
        self.assertTrue(plan.tails[0].flick)

    def test_slide_flick_tail_arrives_from_real_tail_beat_not_double_lead(self):
        chart = [
            {"type": "BPM", "bpm": 60, "beat": 0},
            {
                "type": "Slide",
                "connections": [
                    {"lane": 1, "beat": 0},
                    {"lane": 5, "beat": 2, "flick": True},
                ],
            },
        ]

        plan = clicker.build_play_plan(
            chart,
            (1920, 1080),
            flick_lead_seconds=0.005,
            slide_lead_seconds=0.04,
        )

        self.assertAlmostEqual(plan.tails[0].offset, 1.995)
        self.assertAlmostEqual(plan.tails[0].arrive_offset, 1.96)
        self.assertLessEqual(plan.tails[0].arrive_offset, plan.tails[0].offset)

        events = clicker.build_touch_lifecycle_events(
            plan.taps,
            plan.holds,
            plan.tails,
            flick_distance=150,
            flick_duration=0.045,
            tap_duration=0.024,
            slide_step=0.012,
        )
        tail_lane_moves = [
            event
            for event in events
            if event.contact_id == "hold:1"
            and event.action == "move"
            and event.position == clicker.TapPosition(1404, 887)
        ]
        self.assertTrue(tail_lane_moves)
        self.assertAlmostEqual(tail_lane_moves[0].offset, 1.96)

    def test_slide_long_connections_become_hold_moves(self):
        chart = [
            {"type": "BPM", "bpm": 60, "beat": 0},
            {
                "type": "Slide",
                "connections": [
                    {"lane": 1, "beat": 0},
                    {"lane": 3, "beat": 1},
                    {"lane": 5, "beat": 2},
                ],
            },
        ]

        plan = clicker.build_play_plan(chart, (1920, 1080), slide_lead_seconds=0.08)

        self.assertEqual(len(plan.holds[0].moves), 1)
        self.assertAlmostEqual(plan.holds[0].moves[0].offset, 0.92)
        self.assertEqual(plan.holds[0].moves[0].position, clicker.TapPosition(960, 887))
        self.assertAlmostEqual(plan.tails[0].offset, 2.0)
        self.assertAlmostEqual(plan.tails[0].arrive_offset, 1.92)

        events = clicker.build_touch_lifecycle_events(
            plan.taps,
            plan.holds,
            plan.tails,
            flick_distance=150,
            flick_duration=0.045,
            tap_duration=0.024,
            slide_step=0.05,
        )
        self.assertIn(
            (0.92, "move", "hold:1", clicker.TapPosition(960, 887)),
            [
                (event.offset, event.action, event.contact_id, event.position)
                for event in events
            ],
        )
        self.assertIn(
            (1.92, "move", "hold:1", clicker.TapPosition(1404, 887)),
            [
                (event.offset, event.action, event.contact_id, event.position)
                for event in events
            ],
        )

    def test_fast_slide_emits_continuous_move_points(self):
        chart = [
            {"type": "BPM", "bpm": 210, "beat": 0},
            {
                "type": "Slide",
                "connections": [
                    {"lane": 0, "beat": 55.5},
                    {"lane": 2, "beat": 56.5},
                    {"lane": 0, "beat": 57},
                    {"lane": 2, "beat": 57.5},
                    {"lane": 1, "beat": 58},
                ],
            },
        ]

        plan = clicker.build_play_plan(chart, (1920, 1080), slide_lead_seconds=0.08)
        events = clicker.build_touch_lifecycle_events(
            plan.taps,
            plan.holds,
            plan.tails,
            flick_distance=150,
            flick_duration=0.045,
            tap_duration=0.024,
            slide_step=0.012,
        )
        move_events = [event for event in events if event.action == "move"]

        self.assertGreater(len(move_events), 20)
        self.assertEqual(move_events[-1].position, clicker.TapPosition(518, 887))
        self.assertLessEqual(move_events[-1].offset, plan.tails[0].arrive_offset)

    def test_evdev_splits_simultaneous_up_and_down_into_separate_frames(self):
        events = [
            clicker.TouchLifecycleEvent(0.0, "down", "old", clicker.TapPosition(100, 887), 0),
            clicker.TouchLifecycleEvent(1.0, "up", "old", clicker.TapPosition(100, 887), 1),
            clicker.TouchLifecycleEvent(1.0, "down", "new", clicker.TapPosition(300, 887), 2),
        ]

        frames = clicker.build_evdev_frames(
            events,
            clicker.TouchDevice(
                path="/dev/input/event4",
                max_x=1080,
                max_y=1920,
                has_slot=True,
                has_tracking_id=True,
                has_btn_tool_finger=True,
                orientation=3,
            ),
            (1920, 1080),
        )
        same_offset_frames = [frame for frame in frames if frame.offset == 1.0]

        self.assertEqual(len(same_offset_frames), 2)
        first_tracking_ids = [
            value
            for event_type, code, value in same_offset_frames[0].events
            if event_type == clicker.EV_ABS and code == clicker.ABS_MT_TRACKING_ID
        ]
        second_tracking_ids = [
            value
            for event_type, code, value in same_offset_frames[1].events
            if event_type == clicker.EV_ABS and code == clicker.ABS_MT_TRACKING_ID
        ]
        self.assertEqual(first_tracking_ids, [-1])
        self.assertEqual(second_tracking_ids, [1])

    def test_plain_tap_releases_before_next_plain_tap_on_same_lane(self):
        taps = [
            clicker.TimedTap(
                note=clicker.TapNote(lane=3, beat=0, source_index=0),
                position=clicker.TapPosition(960, 887),
                offset=0.0,
            ),
            clicker.TimedTap(
                note=clicker.TapNote(lane=3, beat=1, source_index=1),
                position=clicker.TapPosition(960, 887),
                offset=0.020,
            ),
        ]

        events = clicker.build_touch_lifecycle_events(
            taps,
            [],
            [],
            flick_distance=150,
            flick_duration=0.045,
            tap_duration=0.024,
            same_lane_release_gap=0.008,
        )

        first_up = [
            event
            for event in events
            if event.contact_id == "tap:0" and event.action == "up"
        ][0]
        self.assertAlmostEqual(first_up.offset, 0.012)

    def test_directional_tap_emits_horizontal_flick(self):
        taps = [
            clicker.TimedTap(
                note=clicker.TapNote(
                    lane=3,
                    beat=0,
                    source_index=0,
                    direction="Right",
                    width=1,
                ),
                position=clicker.TapPosition(960, 887),
                end_position=clicker.TapPosition(1181, 887),
                offset=0.0,
            ),
        ]

        events = clicker.build_touch_lifecycle_events(
            taps,
            [],
            [],
            flick_distance=150,
            flick_duration=0.084,
            tap_duration=0.024,
        )

        self.assertEqual([event.action for event in events], ["down", "move", "up"])
        self.assertEqual(events[1].position, clicker.TapPosition(1181, 887))
        self.assertEqual(events[2].position, clicker.TapPosition(1181, 887))

    def test_timing_noise_shifts_each_contact_without_changing_contact_duration(self):
        events = [
            clicker.TouchLifecycleEvent(1.0, "down", "tap:0", clicker.TapPosition(100, 887), 0),
            clicker.TouchLifecycleEvent(1.024, "up", "tap:0", clicker.TapPosition(100, 887), 1),
            clicker.TouchLifecycleEvent(2.0, "down", "tap:1", clicker.TapPosition(200, 887), 2),
            clicker.TouchLifecycleEvent(2.024, "up", "tap:1", clicker.TapPosition(200, 887), 3),
        ]

        shifted = clicker.apply_timing_noise(events, 0.05, rng=random.Random(3))
        by_contact = {
            contact_id: [event for event in shifted if event.contact_id == contact_id]
            for contact_id in {"tap:0", "tap:1"}
        }

        self.assertAlmostEqual(by_contact["tap:0"][1].offset - by_contact["tap:0"][0].offset, 0.024)
        self.assertAlmostEqual(by_contact["tap:1"][1].offset - by_contact["tap:1"][0].offset, 0.024)
        self.assertNotAlmostEqual(
            by_contact["tap:0"][0].offset - events[0].offset,
            by_contact["tap:1"][0].offset - events[2].offset,
        )

    def test_position_noise_shifts_each_contact_without_changing_contact_shape(self):
        events = [
            clicker.TouchLifecycleEvent(1.0, "down", "tap:0", clicker.TapPosition(100, 887), 0),
            clicker.TouchLifecycleEvent(1.024, "up", "tap:0", clicker.TapPosition(100, 887), 1),
            clicker.TouchLifecycleEvent(2.0, "down", "tap:1", clicker.TapPosition(200, 887), 2),
            clicker.TouchLifecycleEvent(2.024, "up", "tap:1", clicker.TapPosition(200, 887), 3),
        ]

        shifted = clicker.apply_position_noise(events, 8, rng=random.Random(3))
        by_contact = {
            contact_id: [event for event in shifted if event.contact_id == contact_id]
            for contact_id in {"tap:0", "tap:1"}
        }

        self.assertEqual(by_contact["tap:0"][0].position, by_contact["tap:0"][1].position)
        self.assertEqual(by_contact["tap:1"][0].position, by_contact["tap:1"][1].position)
        self.assertNotEqual(by_contact["tap:0"][0].position, events[0].position)
        self.assertNotEqual(
            by_contact["tap:0"][0].position.x - events[0].position.x,
            by_contact["tap:1"][0].position.x - events[2].position.x,
        )

    def test_cli_exposes_chart_serial_and_noise_options(self):
        parser = clicker.build_parser()
        help_text = parser.format_help()

        self.assertIn("--serial", help_text)
        self.assertIn("--timing-noise-ms", help_text)
        self.assertIn("--position-noise-px", help_text)
        self.assertIn("--dynamic-adjust", help_text)
        self.assertIn("--ignore-last-note", help_text)
        self.assertIn("song_id", help_text)
        self.assertIn("difficulty", help_text)
        self.assertNotIn("--backend", help_text)
        self.assertNotIn("--adb", help_text)
        self.assertNotIn("--dry-run", help_text)

    def test_removed_backend_argument_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                clicker.build_parser().parse_args(["1", "expert", "--backend", "input"])

        self.assertEqual(raised.exception.code, 2)

    def test_omit_last_note_from_plan_removes_final_tap(self):
        chart = [
            {"type": "BPM", "bpm": 60, "beat": 0},
            {"type": "Single", "lane": 1, "beat": 0},
            {"type": "Single", "lane": 2, "beat": 1},
        ]

        plan = clicker.omit_last_note_from_plan(clicker.build_play_plan(chart, (1920, 1080)))

        self.assertEqual([tap.note.lane for tap in plan.taps], [1])

    def test_omit_last_note_from_plan_trims_final_long_tail(self):
        chart = [
            {"type": "BPM", "bpm": 60, "beat": 0},
            {
                "type": "Long",
                "connections": [
                    {"lane": 3, "beat": 0},
                    {"lane": 3, "beat": 2},
                ],
            },
        ]

        plan = clicker.omit_last_note_from_plan(clicker.build_play_plan(chart, (1920, 1080)))

        self.assertEqual(plan.tails, ())
        self.assertEqual(len(plan.holds), 1)
        self.assertAlmostEqual(plan.holds[0].duration, 1.992)

    def test_run_uses_evdev_native_writer_on_x86_64(self):
        fake_runner = FakeRunner()
        process_factory = FakeProcessFactory()
        waited = []

        with tempfile.TemporaryDirectory() as directory:
            write_local_chart(
                directory,
                65,
                "expert",
                [
                    {"type": "BPM", "bpm": 60000, "beat": 0},
                    {"type": "Single", "lane": 5, "beat": 0},
                ],
            )
            with temporary_cwd(directory):
                exit_code = clicker.run(
                    ["65", "expert", "--serial", "127.0.0.1:5555"],
                    runner=fake_runner,
                    wait_for_space_fn=lambda: waited.append(True),
                    process_factory=process_factory,
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(waited, [True])
        self.assertEqual(fake_runner.commands[:3], [
            ["adb", "connect", "127.0.0.1:5555"],
            ["adb", "-s", "127.0.0.1:5555", "shell", "wm", "size"],
            ["adb", "-s", "127.0.0.1:5555", "shell", "getevent", "-p"],
        ])
        self.assertIn(
            ["adb", "-s", "127.0.0.1:5555", "shell", "getprop", "ro.product.cpu.abi"],
            fake_runner.commands,
        )
        self.assertTrue(any(len(command) > 3 and command[3] == "push" for command in fake_runner.commands))
        self.assertIn(
            ["adb", "-s", "127.0.0.1:5555", "shell", "chmod", "755", clicker.EVDEV_NATIVE_HELPER_REMOTE_PATH],
            fake_runner.commands,
        )
        self.assertEqual(
            process_factory.processes[0].command,
            [
                "adb",
                "-s",
                "127.0.0.1:5555",
                "shell",
                "-T",
                clicker.EVDEV_NATIVE_HELPER_REMOTE_PATH,
                "/dev/input/event6",
                clicker.EVDEV_NATIVE_SCHEDULE_REMOTE_PATH,
            ],
        )
        self.assertTrue(
            any(
                len(command) > 4
                and command[3] == "push"
                and command[5] == clicker.EVDEV_NATIVE_SCHEDULE_REMOTE_PATH
                for command in fake_runner.commands
            )
        )

    def test_emulator_serial_skips_connect(self):
        fake_runner = FakeRunner()
        process_factory = FakeProcessFactory()

        with tempfile.TemporaryDirectory() as directory:
            write_local_chart(
                directory,
                65,
                "expert",
                [
                    {"type": "BPM", "bpm": 60000, "beat": 0},
                    {"type": "Single", "lane": 5, "beat": 0},
                ],
            )
            with temporary_cwd(directory):
                exit_code = clicker.run(
                    ["65", "expert", "--serial", "emulator-5554"],
                    runner=fake_runner,
                    wait_for_space_fn=lambda: None,
                    process_factory=process_factory,
                )

        self.assertEqual(exit_code, 0)
        self.assertNotIn(["adb", "connect", "emulator-5554"], fake_runner.commands)
        self.assertIn(["adb", "-s", "emulator-5554", "shell", "wm", "size"], fake_runner.commands)
        self.assertEqual(process_factory.processes[0].command[0:3], ["adb", "-s", "emulator-5554"])

    def test_build_evdev_schedule_uses_fixed_size_records(self):
        frames = [
            clicker.TimedEvdevFrame(
                offset=1.25,
                events=((clicker.EV_ABS, clicker.ABS_MT_SLOT, 0),),
            ),
            clicker.TimedEvdevFrame(
                offset=1.25,
                events=((clicker.EV_SYN, clicker.SYN_REPORT, 0),),
            ),
        ]

        schedule = clicker.build_evdev_schedule(frames)

        self.assertEqual(len(schedule), 32)
        self.assertEqual(
            schedule[:16],
            struct.pack("<QHHi", 1_250_000_000, clicker.EV_ABS, clicker.ABS_MT_SLOT, 0),
        )

    def test_adjusted_frame_chunk_applies_timeline_offset_to_unsent_frames(self):
        frames = [
            clicker.TimedEvdevFrame(offset=1.0, events=((clicker.EV_SYN, clicker.SYN_REPORT, 0),)),
            clicker.TimedEvdevFrame(offset=1.2, events=((clicker.EV_SYN, clicker.SYN_REPORT, 0),)),
            clicker.TimedEvdevFrame(offset=2.0, events=((clicker.EV_SYN, clicker.SYN_REPORT, 0),)),
        ]

        chunk, next_index = clicker.adjusted_frame_chunk(
            frames,
            start_index=0,
            elapsed=0.9,
            timeline_adjust=-0.05,
            chunk_seconds=0.3,
        )

        self.assertEqual(next_index, 2)
        self.assertEqual([round(frame.offset, 3) for frame in chunk], [0.05, 0.25])

    def test_release_frames_clear_all_slots_and_touch_state(self):
        frames = clicker.build_release_frames(
            clicker.TouchDevice(
                path="/dev/input/event4",
                max_x=1080,
                max_y=1920,
                has_slot=True,
                has_tracking_id=True,
                has_btn_tool_finger=True,
            )
        )

        events = frames[0].events
        self.assertEqual(events.count((clicker.EV_ABS, clicker.ABS_MT_TRACKING_ID, -1)), 10)
        self.assertIn((clicker.EV_KEY, clicker.BTN_TOUCH, 0), events)
        self.assertIn((clicker.EV_KEY, clicker.BTN_TOOL_FINGER, 0), events)

    def test_evdev_tracking_ids_are_reused_from_small_slot_pool(self):
        taps = [
            clicker.TimedTap(
                note=clicker.TapNote(lane=i % 7, beat=i, source_index=i),
                position=clicker.TapPosition(100 + i, 887),
                offset=i * 0.05,
            )
            for i in range(120)
        ]
        events = clicker.build_touch_lifecycle_events(
            taps,
            [],
            [],
            flick_distance=150,
            flick_duration=0.045,
            tap_duration=0.01,
        )
        frames = clicker.build_evdev_frames(
            events,
            clicker.TouchDevice(
                path="/dev/input/event4",
                max_x=1080,
                max_y=1920,
                has_slot=True,
                has_tracking_id=True,
                has_btn_tool_finger=True,
                orientation=3,
            ),
            (1920, 1080),
        )

        tracking_ids = [
            value
            for frame in frames
            for event_type, code, value in frame.events
            if event_type == clicker.EV_ABS
            and code == clicker.ABS_MT_TRACKING_ID
            and value >= 0
        ]
        self.assertTrue(tracking_ids)
        self.assertLessEqual(max(tracking_ids), 10)

    def test_missing_chart_reports_chart_error_not_adb_error(self):
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as directory:
            with temporary_cwd(directory), contextlib.redirect_stderr(stderr):
                exit_code = clicker.run(
                    ["531", "expert"],
                    runner=FakeRunner(),
                    wait_for_space_fn=lambda: None,
                )

        self.assertEqual(exit_code, 1)
        output = stderr.getvalue()
        self.assertIn("Error: chart file not found:", output)
        self.assertIn("charts", output)
        self.assertIn("expert", output)
        self.assertIn("531.json", output)
        self.assertNotIn("executable not found", output)


if __name__ == "__main__":
    unittest.main()
