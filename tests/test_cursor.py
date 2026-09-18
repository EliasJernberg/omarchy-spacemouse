"""Cursor handling: park it, clutch it, put it back.

This is the difference between a drag that feels like a 3D mouse and one that
feels like a mouse being shoved across a desk, so it is worth the tests.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import FakeClock, key_events, rel_events, sm  # noqa: E402

SHIFT = sm.KEY_NAMES["KEY_LEFTSHIFT"]


class FakeHypr(object):
    """Stands in for the compositor: remembers where the pointer went."""

    def __init__(self, position=(100, 100), geometry=("0xabc", 1000, 500, 800, 600)):
        self.position = position
        self.geometry = geometry
        self.warps = []
        self.focused = geometry[0] if geometry else ""
        self.focus_calls = []
        self.warp_fails = False
        self.steal_focus_on_warp = False

    def cursor_position(self):
        return self.position

    def warp(self, x, y):
        if self.warp_fails:
            return False
        self.warps.append((x, y))
        self.position = (x, y)
        if self.steal_focus_on_warp:
            self.focused = "0xsomeone-else"
        return True

    def geometry_of_focus(self):
        return self.geometry

    def active_address(self):
        return self.focused

    def focus(self, address):
        self.focus_calls.append(address)
        self.focused = address
        return True


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.hypr = FakeHypr()
        self.settings = {"cursor_warp": True, "clutch_fraction": 0.35}
        self.cursor = sm.CursorController(self.settings, hypr=self.hypr)

    def test_begin_parks_the_pointer_in_the_middle_of_the_window(self):
        self.assertTrue(self.cursor.begin())
        # window at (1000, 500), 800x600 -> centre (1400, 800)
        self.assertEqual(self.hypr.warps, [(1400, 800)])
        self.assertTrue(self.cursor.active)

    def test_end_puts_it_back_where_it_was(self):
        self.cursor.begin()
        self.cursor.end()
        self.assertEqual(self.hypr.warps[-1], (100, 100))
        self.assertFalse(self.cursor.active)

    def test_a_round_trip_leaves_the_pointer_exactly_where_it_started(self):
        start = self.hypr.position
        self.cursor.begin()
        self.cursor.moved(120, 40)
        self.cursor.end()
        self.assertEqual(self.hypr.position, start)

    def test_no_window_means_no_warping_at_all(self):
        self.hypr.geometry = None
        self.assertFalse(self.cursor.begin())
        self.assertEqual(self.hypr.warps, [])
        self.assertFalse(self.cursor.active)

    def test_disabled_by_settings(self):
        self.settings["cursor_warp"] = False
        self.assertFalse(self.cursor.begin())
        self.assertEqual(self.hypr.warps, [])

    def test_a_failed_warp_does_not_leave_a_half_started_session(self):
        self.hypr.warp_fails = True
        self.assertFalse(self.cursor.begin())
        self.assertFalse(self.cursor.active)
        # end() must then be a no-op rather than warping to a stale position.
        self.assertFalse(self.cursor.end())

    def test_focus_is_taken_back_if_the_warp_handed_it_away(self):
        # follow_mouse is on by default: landing the pointer back on another
        # window would otherwise move the focus with it.
        self.cursor.begin()
        self.hypr.steal_focus_on_warp = True
        self.cursor.end()
        self.assertEqual(self.hypr.focus_calls, ["0xabc"])

    def test_focus_is_left_alone_when_it_did_not_change(self):
        self.cursor.begin()
        self.cursor.end()
        self.assertEqual(self.hypr.focus_calls, [])


class ClutchTest(unittest.TestCase):
    def setUp(self):
        self.hypr = FakeHypr(geometry=("0xabc", 0, 0, 800, 600))
        self.settings = {"cursor_warp": True, "clutch_fraction": 0.35}
        self.cursor = sm.CursorController(self.settings, hypr=self.hypr)
        self.cursor.begin()

    def test_no_clutch_while_there_is_room(self):
        self.assertFalse(self.cursor.moved(100, 0))
        self.assertFalse(self.cursor.moved(100, 0))  # 200 of 280

    def test_clutch_when_the_drag_has_crossed_the_threshold(self):
        self.assertFalse(self.cursor.moved(200, 0))
        self.assertTrue(self.cursor.moved(100, 0))  # 300 > 0.35 * 800

    def test_the_vertical_axis_has_its_own_threshold(self):
        self.assertFalse(self.cursor.moved(0, 150))
        self.assertTrue(self.cursor.moved(0, 100))  # 250 > 0.35 * 600

    def test_travel_counts_in_both_directions(self):
        self.assertTrue(self.cursor.moved(-300, 0))

    def test_clutching_recentres_and_resets_the_travel(self):
        self.cursor.moved(300, 0)
        self.assertTrue(self.cursor.clutch())
        self.assertEqual(self.hypr.warps[-1], (400, 300))
        self.assertFalse(self.cursor.moved(100, 0))
        self.assertEqual(self.cursor.clutches, 1)

    def test_a_long_drag_clutches_repeatedly_instead_of_hitting_the_edge(self):
        clutches = 0
        for _ in range(40):
            if self.cursor.moved(50, 0):
                self.cursor.clutch()
                clutches += 1
        self.assertGreaterEqual(clutches, 6)

    def test_the_end_still_restores_after_clutching(self):
        self.cursor.moved(300, 0)
        self.cursor.clutch()
        self.cursor.end()
        self.assertEqual(self.hypr.position, (100, 100))


class EngineCursorTest(unittest.TestCase):
    """The engine driving the controller, which is where it has to hold up."""

    PROFILE = {
        "name": "cad",
        "type": "mouse",
        "match": None,
        "gestures": {
            "orbit": {
                "hold": ["middle"],
                "axes": {"rx": {"to": "dy"}, "ry": {"to": "dx", "gain": -1.0}},
            },
            "pan": {
                "hold": ["shift", "middle"],
                "axes": {"x": {"to": "dx"}, "z": {"to": "dy", "gain": -1.0}},
            },
        },
    }

    def setUp(self):
        self.clock = FakeClock()
        self.device = sm.TraceDevice(stream=None)
        self.hypr = FakeHypr(geometry=("0xabc", 0, 0, 800, 600))
        self.settings = dict(sm.DEFAULT_SETTINGS)
        self.settings["curve"] = 1.0
        self.settings["smoothing_ms"] = 0.0
        self.cursor = sm.CursorController(self.settings, hypr=self.hypr)
        self.engine = sm.GestureEngine(
            self.device, self.settings, clock=self.clock, cursor=self.cursor
        )
        self.engine.set_profile(sm.Profile(self.PROFILE))

    def tick(self, raw, dt=1 / 120.0, count=1):
        for _ in range(count):
            self.clock.advance(dt)
            self.engine.tick(raw, dt)

    def test_a_gesture_parks_the_pointer_before_pressing(self):
        self.tick([0, 0, 0, 300, 0, 0], count=3)
        self.assertEqual(self.hypr.warps[0], (400, 300))
        self.assertEqual(self.device.pressed, [sm.BTN_MIDDLE])
        # The warp is the first thing that happened, before any button.
        self.assertTrue(self.cursor.active)

    def test_the_pointer_goes_back_when_the_gesture_ends(self):
        self.tick([0, 0, 0, 300, 0, 0], count=3)
        self.tick([0, 0, 0, 0, 0, 0], dt=0.05, count=4)
        self.assertEqual(self.device.pressed, [])
        self.assertEqual(self.hypr.position, (100, 100))

    def test_switching_from_orbit_to_pan_does_not_move_the_pointer(self):
        # A change of button mid-drag is not a new drag: warping there would
        # make the view jump.
        self.tick([0, 0, 0, 300, 0, 0], count=3)
        warps = len(self.hypr.warps)
        self.tick([300, 0, 0, 0, 0, 0], count=6)
        self.assertEqual(self.engine.active_name, "pan")
        self.assertEqual(self.device.pressed, [SHIFT, sm.BTN_MIDDLE])
        self.assertEqual(len(self.hypr.warps), warps, "no warp on a button change")

    def test_a_long_drag_clutches_without_losing_the_button(self):
        self.settings["pointer_speed"] = 6000.0
        self.tick([0, 0, 0, 0, 350, 0], count=60)
        self.assertGreater(self.engine.clutches, 0)
        # Still dragging afterwards, and the button is down.
        self.assertEqual(self.device.pressed, [sm.BTN_MIDDLE])
        events = key_events(self.device)
        # Every clutch is a release followed by a press of the same button.
        for index, (code, value) in enumerate(events[:-1]):
            if value == 0 and code == sm.BTN_MIDDLE:
                self.assertEqual(events[index + 1], (sm.BTN_MIDDLE, 1))

    def test_motion_keeps_flowing_across_a_clutch(self):
        self.settings["pointer_speed"] = 6000.0
        self.tick([0, 0, 0, 0, 350, 0], count=60)
        self.assertGreater(self.engine.clutches, 0)
        moved = sum(abs(v) for c, v in rel_events(self.device) if c == sm.REL_X)
        self.assertGreater(moved, 500)

    def test_release_all_restores_the_pointer(self):
        self.tick([0, 0, 0, 300, 0, 0], count=3)
        self.engine.release_all()
        self.assertEqual(self.hypr.position, (100, 100))
        self.assertFalse(self.cursor.active)

    def test_a_profile_change_mid_drag_restores_the_pointer(self):
        self.tick([0, 0, 0, 300, 0, 0], count=3)
        self.engine.set_profile(sm.OFF_PROFILE)
        self.assertEqual(self.hypr.position, (100, 100))
        self.assertEqual(self.device.pressed, [])

    def test_without_a_window_the_drag_still_works(self):
        self.hypr.geometry = None
        self.tick([0, 0, 0, 300, 0, 0], count=3)
        self.assertEqual(self.device.pressed, [sm.BTN_MIDDLE])
        self.assertEqual(self.hypr.warps, [])

    def test_a_compositor_that_errors_does_not_break_the_gesture(self):
        class Broken(FakeHypr):
            def geometry_of_focus(self):
                raise RuntimeError("no compositor")

        self.cursor.hypr = Broken()
        self.tick([0, 0, 0, 300, 0, 0], count=3)
        self.assertEqual(self.device.pressed, [sm.BTN_MIDDLE])


if __name__ == "__main__":
    unittest.main()
