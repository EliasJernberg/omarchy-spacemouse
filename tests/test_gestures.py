"""The gesture machine: which group wins, when it grabs, when it lets go.

Everything here runs against a TraceDevice and a hand-driven clock, so the
tests are exact rather than timing-dependent. The rule that matters most is the
last one in this file: no path may leave a button held.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import FakeClock, key_events, rel_events, sm  # noqa: E402

SHIFT = sm.KEY_NAMES["KEY_LEFTSHIFT"]
HOME = sm.KEY_NAMES["KEY_HOME"]

CAD_PROFILE = {
    "name": "cad",
    "type": "mouse",
    "match": None,
    "fit_key": "KEY_HOME",
    "gestures": {
        "orbit": {
            "hold": ["middle"],
            "axes": {"rx": {"to": "dy", "gain": 1.0}, "ry": {"to": "dx", "gain": -1.0}},
        },
        "pan": {
            "hold": ["shift", "middle"],
            "axes": {"x": {"to": "dx", "gain": 1.0}, "z": {"to": "dy", "gain": -1.0}},
        },
        "zoom": {"mode": "wheel", "axes": {"y": {"to": "wheel", "gain": 1.0}}},
    },
    "buttons": {"5": {"action": "fit"}},
}

BROWSER_PROFILE = {
    "name": "browser",
    "type": "mouse",
    "match": "^Opera$",
    "gestures": {
        "orbit": {
            "hold": ["left"],
            "axes": {"rx": {"to": "dy"}, "ry": {"to": "dx", "gain": -1.0}},
        },
        "pan": {
            "hold": ["right"],
            "axes": {"x": {"to": "dx"}, "z": {"to": "dy", "gain": -1.0}},
        },
    },
}


def axes(x=0, y=0, z=0, rx=0, ry=0, rz=0):
    return [x, y, z, rx, ry, rz]


class EngineFixture(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.device = sm.TraceDevice(stream=None)
        self.settings = dict(sm.DEFAULT_SETTINGS)
        self.settings["curve"] = 1.0
        self.engine = sm.GestureEngine(self.device, self.settings, clock=self.clock)

    def use(self, spec):
        profile = sm.Profile(spec)
        self.engine.set_profile(profile)
        return profile

    def tick(self, raw, dt=1 / 60.0, count=1):
        for _ in range(count):
            self.clock.advance(dt)
            self.engine.tick(raw, dt)

    def held(self):
        return list(self.device.pressed)


class GroupSelectionTest(EngineFixture):
    def test_rotation_starts_the_orbit_gesture(self):
        self.use(CAD_PROFILE)
        self.tick(axes(rx=300), count=3)
        self.assertEqual(self.engine.active_name, "orbit")
        self.assertEqual(self.held(), [sm.BTN_MIDDLE])

    def test_translation_starts_the_pan_gesture_with_its_modifier(self):
        self.use(CAD_PROFILE)
        self.tick(axes(x=300), count=3)
        self.assertEqual(self.engine.active_name, "pan")
        self.assertEqual(self.held(), [SHIFT, sm.BTN_MIDDLE])

    def test_lift_zooms_without_holding_anything(self):
        self.use(CAD_PROFILE)
        self.settings["wheel_speed"] = 12.0
        self.tick(axes(y=300), count=10)
        # The wheel is not a drag: it holds no button, so it never becomes
        # "the" gesture and never blocks one.
        self.assertEqual(self.engine.active_name, "")
        self.assertEqual(self.held(), [])
        self.assertTrue([v for c, v in rel_events(self.device) if c == sm.REL_WHEEL_HI_RES])

    def test_zoom_runs_at_the_same_time_as_a_drag(self):
        # 3Dconnexion's own driver lets you push into the model while turning
        # it; the puck has six axes for a reason.
        self.use(CAD_PROFILE)
        self.settings["wheel_speed"] = 12.0
        self.tick(axes(rx=300, y=300), count=10)
        self.assertEqual(self.engine.active_name, "orbit")
        self.assertEqual(self.held(), [sm.BTN_MIDDLE])
        rel = rel_events(self.device)
        self.assertTrue([v for c, v in rel if c == sm.REL_Y], "orbit should move")
        self.assertTrue([v for c, v in rel if c == sm.REL_WHEEL_HI_RES], "zoom should scroll")

    def test_the_dominant_axis_group_wins(self):
        self.use(CAD_PROFILE)
        # Real hardware cross-talks: pushing sideways also tilts a little.
        self.tick(axes(x=300, rx=45), count=3)
        self.assertEqual(self.engine.active_name, "pan")
        self.assertEqual(self.held(), [SHIFT, sm.BTN_MIDDLE])

    def test_only_one_group_is_ever_held(self):
        self.use(CAD_PROFILE)
        for raw in (axes(x=300), axes(rx=300), axes(y=300), axes(z=300, ry=200)):
            self.tick(raw, count=6)
            buttons = [code for code in self.held() if code in sm.MOUSE_BUTTON_CODES]
            self.assertLessEqual(len(buttons), 1, "held %s on %s" % (self.held(), raw))

    def test_switching_group_releases_the_old_one_first(self):
        self.use(CAD_PROFILE)
        self.tick(axes(x=300), count=3)
        self.assertEqual(self.held(), [SHIFT, sm.BTN_MIDDLE])
        self.device.records = []
        # Swing hard onto rotation: pan's magnitude collapses, orbit dominates.
        self.tick(axes(rx=300), count=3)
        self.assertEqual(self.engine.active_name, "orbit")
        events = key_events(self.device)
        # middle up, shift up, then middle down. Never two buttons at once.
        self.assertEqual(events[0], (sm.BTN_MIDDLE, 0))
        self.assertEqual(events[1], (SHIFT, 0))
        self.assertEqual(events[2], (sm.BTN_MIDDLE, 1))
        self.assertEqual(self.held(), [sm.BTN_MIDDLE])

    def test_a_weak_competitor_does_not_steal_the_gesture(self):
        self.use(CAD_PROFILE)
        self.tick(axes(x=300), count=3)
        self.assertEqual(self.engine.active_name, "pan")
        # Slightly stronger rotation, but not the 1.35x the switch demands.
        self.tick(axes(x=250, rx=260), count=3)
        self.assertEqual(self.engine.active_name, "pan")

    def test_resting_noise_never_starts_a_gesture(self):
        # These are the values this puck actually reports with a hand off it,
        # and rx=20 is above the deadzone. The engage gate is what stops it.
        self.use(CAD_PROFILE)
        self.tick(axes(x=4, y=-18, rx=20), count=30)
        self.assertEqual(self.engine.active_name, "")
        self.assertEqual(self.held(), [])
        self.assertEqual(key_events(self.device), [])

    def test_a_deliberate_nudge_past_the_engage_gate_does_start_one(self):
        self.use(CAD_PROFILE)
        self.tick(axes(rx=26), count=3)
        self.assertEqual(self.engine.active_name, "orbit")

    def test_a_running_gesture_survives_down_to_the_deadzone(self):
        # Hysteresis: engaging takes 24 counts, staying engaged takes 19.
        self.use(CAD_PROFILE)
        self.tick(axes(rx=300), count=3)
        self.assertEqual(self.engine.active_name, "orbit")
        self.tick(axes(rx=19), dt=0.01, count=20)
        self.assertEqual(self.engine.active_name, "orbit")
        self.assertEqual(self.held(), [sm.BTN_MIDDLE])

    def test_browser_profile_uses_plain_mouse_buttons(self):
        self.use(BROWSER_PROFILE)
        self.tick(axes(rx=300), count=3)
        self.assertEqual(self.held(), [sm.BTN_LEFT])
        self.tick(axes(x=300), count=6)
        self.assertEqual(self.held(), [sm.BTN_RIGHT])


class ReleaseTest(EngineFixture):
    def test_button_is_held_through_a_short_pause(self):
        self.use(CAD_PROFILE)
        self.tick(axes(rx=300), count=3)
        self.assertEqual(self.held(), [sm.BTN_MIDDLE])
        # Centred, but only for 50 ms: under the 80 ms idle timeout.
        self.tick(axes(), dt=0.025, count=2)
        self.assertEqual(self.held(), [sm.BTN_MIDDLE])
        self.assertEqual(self.engine.active_name, "orbit")

    def test_button_is_released_after_the_idle_timeout(self):
        self.use(CAD_PROFILE)
        self.tick(axes(rx=300), count=3)
        self.tick(axes(), dt=0.05, count=5)
        self.assertEqual(self.held(), [])
        self.assertEqual(self.engine.active_name, "")

    def test_profile_change_releases_everything_at_once(self):
        self.use(CAD_PROFILE)
        self.tick(axes(x=300), count=3)
        self.assertEqual(self.held(), [SHIFT, sm.BTN_MIDDLE])
        self.use(BROWSER_PROFILE)
        self.assertEqual(self.held(), [])
        self.assertEqual(self.engine.active_name, "")
        events = key_events(self.device)
        self.assertEqual(events[-2:], [(sm.BTN_MIDDLE, 0), (SHIFT, 0)])

    def test_switching_to_a_native_profile_releases_everything(self):
        self.use(CAD_PROFILE)
        self.tick(axes(rx=300), count=3)
        self.assertEqual(self.held(), [sm.BTN_MIDDLE])
        self.use({"name": "kicad", "type": "native", "match": "^kicad$"})
        self.assertEqual(self.held(), [])

    def test_native_profile_emits_nothing(self):
        self.use({"name": "kicad", "type": "native", "match": "^kicad$"})
        self.tick(axes(x=300, rx=300, y=300), count=10)
        self.assertEqual(self.device.records, [])

    def test_off_profile_emits_nothing(self):
        self.use({"name": "quiet", "type": "off", "match": None})
        self.tick(axes(x=300, rx=300, y=300), count=10)
        self.assertEqual(self.device.records, [])

    def test_release_all_is_idempotent(self):
        self.use(CAD_PROFILE)
        self.tick(axes(x=300), count=3)
        self.engine.release_all()
        before = len(self.device.records)
        self.engine.release_all()
        self.assertEqual(self.held(), [])
        self.assertEqual(len(self.device.records), before)

    def test_no_path_leaves_a_button_held(self):
        """The one that matters: a stuck middle button makes the desktop unusable."""
        self.use(CAD_PROFILE)
        script = [
            axes(x=300),
            axes(rx=300),
            axes(y=300),
            axes(z=-300),
            axes(x=200, rx=200),
            axes(),
            axes(ry=300),
            axes(x=-300),
        ]
        for raw in script:
            self.tick(raw, count=4)
        self.engine.set_profile(sm.OFF_PROFILE)
        self.assertEqual(self.held(), [])
        # Every press in the whole run has a matching release.
        depth = {}
        for code, value in key_events(self.device):
            depth[code] = depth.get(code, 0) + (1 if value else -1)
            self.assertIn(depth[code], (0, 1), "code %d went out of balance" % code)
        self.assertEqual(set(depth.values()) - {0}, set())


class MotionTest(EngineFixture):
    def test_pointer_moves_in_the_expected_direction(self):
        self.use(CAD_PROFILE)
        self.tick(axes(x=350), count=6)
        rel = rel_events(self.device)
        dx = sum(value for code, value in rel if code == sm.REL_X)
        self.assertGreater(dx, 0, "pushing the puck right must move the pointer right")

        self.engine.set_profile(sm.OFF_PROFILE)
        self.use(CAD_PROFILE)
        self.device.records = []
        self.tick(axes(z=350), count=6)
        rel = rel_events(self.device)
        dy = sum(value for code, value in rel if code == sm.REL_Y)
        self.assertLess(dy, 0, "pushing the puck away must move the pointer up")

    def test_motion_scales_with_deflection(self):
        self.use(CAD_PROFILE)
        self.tick(axes(x=350), count=10)
        hard = sum(v for c, v in rel_events(self.device) if c == sm.REL_X)

        self.engine.set_profile(sm.OFF_PROFILE)
        self.use(CAD_PROFILE)
        self.device.records = []
        self.tick(axes(x=150), count=10)
        soft = sum(v for c, v in rel_events(self.device) if c == sm.REL_X)
        self.assertGreater(hard, soft * 2)

    def test_sub_pixel_motion_is_accumulated_not_dropped(self):
        self.use(CAD_PROFILE)
        self.settings["pointer_speed"] = 30.0  # 0.5 px per 60 Hz tick
        self.tick(axes(x=350), count=30)
        dx = sum(v for c, v in rel_events(self.device) if c == sm.REL_X)
        self.assertGreater(dx, 10)

    def test_zoom_emits_high_resolution_wheel_and_whole_detents(self):
        self.use(CAD_PROFILE)
        self.settings["wheel_speed"] = 12.0
        self.tick(axes(y=350), count=30)
        rel = rel_events(self.device)
        hi_res = sum(v for c, v in rel if c == sm.REL_WHEEL_HI_RES)
        detents = sum(v for c, v in rel if c == sm.REL_WHEEL)
        self.assertGreater(hi_res, 0)
        self.assertGreater(detents, 0)
        # 120 high resolution units make one detent, the way real wheels report.
        self.assertAlmostEqual(detents, round(hi_res / 120.0), delta=1)

    def test_zoom_direction_follows_the_sign(self):
        self.use(CAD_PROFILE)
        self.settings["wheel_speed"] = 12.0
        self.tick(axes(y=-350), count=30)
        rel = rel_events(self.device)
        self.assertLess(sum(v for c, v in rel if c == sm.REL_WHEEL_HI_RES), 0)

    def test_legacy_wheel_only_when_high_resolution_is_off(self):
        self.use(CAD_PROFILE)
        self.settings["wheel_speed"] = 12.0
        self.settings["wheel_hi_res"] = False
        self.tick(axes(y=350), count=30)
        rel = rel_events(self.device)
        self.assertEqual([v for c, v in rel if c == sm.REL_WHEEL_HI_RES], [])
        self.assertGreater(sum(v for c, v in rel if c == sm.REL_WHEEL), 0)


class ButtonTest(EngineFixture):
    def test_fit_button_taps_the_profile_fit_key(self):
        self.use(CAD_PROFILE)
        self.engine.handle_button(5, True)
        self.assertEqual(key_events(self.device), [(HOME, 1), (HOME, 0)])

    def test_release_does_nothing(self):
        self.use(CAD_PROFILE)
        self.engine.handle_button(5, False)
        self.assertEqual(self.device.records, [])

    def test_unbound_button_is_ignored(self):
        self.use(CAD_PROFILE)
        self.assertIsNone(self.engine.handle_button(2, True))
        self.assertEqual(self.device.records, [])

    def test_fit_without_a_fit_key_is_a_no_op(self):
        self.use(BROWSER_PROFILE)
        self.engine.profile.buttons[5] = sm.ButtonAction({"action": "fit"})
        self.assertIsNone(self.engine.handle_button(5, True))
        self.assertEqual(self.device.records, [])

    def test_key_action_taps_a_combination_in_order(self):
        self.use(
            {
                "name": "p",
                "type": "mouse",
                "match": None,
                "buttons": {"3": {"action": "key", "key": "ctrl+shift+f"}},
            }
        )
        self.engine.handle_button(3, True)
        codes = key_events(self.device)
        ctrl = sm.KEY_NAMES["KEY_LEFTCTRL"]
        f = sm.KEY_NAMES["KEY_F"]
        self.assertEqual(
            codes, [(ctrl, 1), (SHIFT, 1), (f, 1), (f, 0), (SHIFT, 0), (ctrl, 0)]
        )

    def test_buttons_do_nothing_in_a_native_profile(self):
        self.use(
            {
                "name": "kicad",
                "type": "native",
                "match": "^kicad$",
                "buttons": {"5": {"action": "key", "key": "KEY_HOME"}},
            }
        )
        self.assertIsNone(self.engine.handle_button(5, True))
        self.assertEqual(self.device.records, [])

    def test_exec_action_runs_a_command(self):
        marker = os.path.join(
            os.environ.get("TMPDIR", "/tmp"), "omarchy-spacemouse-test-%d" % os.getpid()
        )
        if os.path.exists(marker):
            os.unlink(marker)
        self.use(
            {
                "name": "p",
                "type": "mouse",
                "match": None,
                "buttons": {"4": {"action": "exec", "command": "touch %s" % marker}},
            }
        )
        self.assertEqual(self.engine.handle_button(4, True), "exec")
        for child in self.engine._children:
            child.wait(timeout=10)
        self.assertTrue(os.path.exists(marker))
        os.unlink(marker)


class KeysProfileTest(EngineFixture):
    PROFILE = {
        "name": "keys",
        "type": "keys",
        "match": None,
        "bindings": [
            {
                "axis": "z",
                "dir": "+",
                "key": "KEY_UP",
                "threshold": 0.3,
                "repeat_hz": 10,
            },
            {
                "axis": "z",
                "dir": "-",
                "key": "KEY_DOWN",
                "threshold": 0.3,
                "repeat_hz": 10,
            },
            {"axis": "x", "dir": "+", "key": "shift", "hold": True, "threshold": 0.3},
        ],
    }

    def test_axis_repeats_a_key_at_the_configured_rate(self):
        self.use(self.PROFILE)
        up = sm.KEY_NAMES["KEY_UP"]
        self.tick(axes(z=350), dt=0.05, count=10)  # 0.5 s at 10 Hz -> 5 taps
        taps = [pair for pair in key_events(self.device) if pair == (up, 1)]
        self.assertGreaterEqual(len(taps), 4)
        self.assertLessEqual(len(taps), 7)
        self.assertEqual(self.held(), [])

    def test_the_other_direction_uses_the_other_key(self):
        self.use(self.PROFILE)
        down = sm.KEY_NAMES["KEY_DOWN"]
        self.tick(axes(z=-350), dt=0.05, count=4)
        self.assertIn((down, 1), key_events(self.device))

    def test_below_threshold_nothing_repeats(self):
        self.use(self.PROFILE)
        self.tick(axes(z=60), dt=0.05, count=10)
        self.assertEqual(key_events(self.device), [])

    def test_hold_binding_presses_and_releases_once(self):
        self.use(self.PROFILE)
        self.tick(axes(x=350), dt=0.05, count=5)
        self.assertEqual(self.held(), [SHIFT])
        self.tick(axes(), dt=0.05, count=2)
        self.assertEqual(self.held(), [])
        self.assertEqual(key_events(self.device), [(SHIFT, 1), (SHIFT, 0)])

    def test_profile_change_releases_a_held_key(self):
        self.use(self.PROFILE)
        self.tick(axes(x=350), dt=0.05, count=5)
        self.assertEqual(self.held(), [SHIFT])
        self.use(CAD_PROFILE)
        self.assertEqual(self.held(), [])


if __name__ == "__main__":
    unittest.main()
