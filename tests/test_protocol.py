"""The spacenavd wire format, checked against a real hardware recording.

The fixture is a capture from the SpaceMouse Pro on this machine: slow sweeps
of every axis followed by three presses of the FIT button. The .txt sidecar was
written at the same time and holds the decoded values, so it is an independent
answer key for the decoder.
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import FIXTURE_BIN, FIXTURE_TXT, sm  # noqa: E402


def read_sidecar():
    """Parse "  13.230  (0, 4, -18, 0, 20, 0, 0, 1031508)" lines."""
    rows = []
    with open(FIXTURE_TXT, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or "(" not in line:
                continue
            body = line[line.index("(") + 1 : line.rindex(")")]
            rows.append(tuple(int(part) for part in body.split(",")))
    return rows


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        with open(FIXTURE_BIN, "rb") as handle:
            self.blob = handle.read()
        self.rows = read_sidecar()

    def test_fixture_is_whole_frames(self):
        self.assertEqual(len(self.blob) % sm.FRAME_SIZE, 0)
        self.assertEqual(len(self.blob) // sm.FRAME_SIZE, len(self.rows))
        self.assertEqual(sm.FRAME_SIZE, 32)
        self.assertEqual(struct.calcsize(sm.FRAME_FMT), 32)

    def test_every_frame_decodes_to_the_recorded_values(self):
        events = list(sm.iter_frames(self.blob))
        self.assertEqual(len(events), len(self.rows))
        for index, (event, row) in enumerate(zip(events, self.rows)):
            if row[0] == sm.UEV_MOTION:
                self.assertEqual(event.kind, "motion", "frame %d" % index)
                self.assertEqual(event.axes, row[1:7], "frame %d" % index)
                self.assertEqual(event.period, row[7], "frame %d" % index)
            elif row[0] == sm.UEV_PRESS:
                self.assertEqual(event.kind, "press")
                self.assertEqual(event.button, row[1])
            elif row[0] == sm.UEV_RELEASE:
                self.assertEqual(event.kind, "release")
                self.assertEqual(event.button, row[1])

    def test_fit_button_is_button_five(self):
        presses = [
            event.button for event in sm.iter_frames(self.blob) if event.kind == "press"
        ]
        releases = [
            event.button
            for event in sm.iter_frames(self.blob)
            if event.kind == "release"
        ]
        self.assertTrue(presses, "the capture should contain button presses")
        self.assertEqual(set(presses), {5})
        self.assertEqual(len(presses), len(releases))

    def test_axis_ranges_match_the_hardware(self):
        peaks = dict((axis, 0) for axis in sm.AXES)
        for event in sm.iter_frames(self.blob):
            if event.kind != "motion":
                continue
            for axis, value in zip(sm.AXES, event.axes):
                peaks[axis] = max(peaks[axis], abs(value))
        # Every axis was swept in this capture, and the puck saturates near 350.
        for axis in sm.AXES:
            self.assertGreater(peaks[axis], 50, "axis %s barely moved" % axis)
            self.assertLessEqual(
                peaks[axis], 400, "axis %s went past full scale" % axis
            )

    def test_short_frame_is_refused(self):
        with self.assertRaises(ValueError):
            sm.decode_frame(self.blob[:16])

    def test_unknown_event_types_are_not_fatal(self):
        frame = struct.pack(sm.FRAME_FMT, sm.UEV_CFG, 1, 2, 3, 4, 5, 6, 7)
        event = sm.decode_frame(frame)
        self.assertEqual(event.kind, "other")
        self.assertEqual(event.raw[0], sm.UEV_CFG)

    def test_press_and_release_round_trip(self):
        press = sm.decode_frame(
            struct.pack(sm.FRAME_FMT, sm.UEV_PRESS, 5, 1, 0, 0, 0, 0, 0)
        )
        release = sm.decode_frame(
            struct.pack(sm.FRAME_FMT, sm.UEV_RELEASE, 5, 0, 0, 0, 0, 0, 0)
        )
        self.assertEqual((press.kind, press.button), ("press", 5))
        self.assertEqual((release.kind, release.button), ("release", 5))

    def test_axis_order_is_x_y_z_rx_ry_rz(self):
        frame = struct.pack(sm.FRAME_FMT, sm.UEV_MOTION, 1, 2, 3, 4, 5, 6, 16)
        event = sm.decode_frame(frame)
        self.assertEqual(
            dict(zip(sm.AXES, event.axes)),
            {"x": 1, "y": 2, "z": 3, "rx": 4, "ry": 5, "rz": 6},
        )
        self.assertEqual(event.period, 16)


if __name__ == "__main__":
    unittest.main()
