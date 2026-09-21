"""The gesture machine: which group wins, when it grabs, when it lets go.

Everything here runs against a TraceDevice and a hand-driven clock, so the
tests are exact rather than timing-dependent. The rule that matters most is the
last one in this file: no path may leave a button held.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (  # noqa: E402
    FakeClock,
    FakeSleep,
    key_batches,
    key_events,
    rel_events,
    sm,
)

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
        self.sleep = FakeSleep()
        self.engine = sm.GestureEngine(
            self.device, self.settings, clock=self.clock, sleep=self.sleep
        )

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
        self.assertTrue(
            [v for c, v in rel_events(self.device) if c == sm.REL_WHEEL_HI_RES]
        )

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
        self.assertTrue(
            [v for c, v in rel if c == sm.REL_WHEEL_HI_RES], "zoom should scroll"
        )

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

    def test_with_high_resolution_off_only_whole_clicks_go_out(self):
        # And they still carry their 120 units each. The device declares
        # REL_WHEEL_HI_RES, and libinput then reads the wheel from that axis
        # alone and drops plain REL_WHEEL, so a classic-only frame scrolls
        # nothing at all: five such detents moved Fusion by zero pixels.
        # What wheel_hi_res: false buys is whole clicks instead of a smooth
        # stream, not a different event.
        self.use(CAD_PROFILE)
        self.settings["wheel_speed"] = 12.0
        self.settings["wheel_hi_res"] = False
        self.tick(axes(y=350), count=30)
        rel = rel_events(self.device)
        detents = [v for c, v in rel if c == sm.REL_WHEEL]
        units = [v for c, v in rel if c == sm.REL_WHEEL_HI_RES]
        self.assertGreater(sum(detents), 0)
        self.assertEqual(units, [120 * detent for detent in detents])


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


class WheelReachTest(EngineFixture):
    """What the wheel actually delivers, and to whom.

    A smooth-scrolling consumer acts on every high resolution unit. Everything
    on Xwayland acts on whole clicks only, and a click is 120 units, so the
    same emission that zooms a browser can reach Wine as literally nothing.
    That is what happened to Fusion's zoom, and these are the numbers.
    """

    ZOOM = {
        "name": "zoomer",
        "type": "mouse",
        "match": None,
        "gestures": {
            "zoom": {"mode": "wheel", "axes": {"y": {"to": "wheel", "gain": 1.0}}},
        },
    }

    def clicks(self):
        return sum(abs(v) for c, v in rel_events(self.device) if c == sm.REL_WHEEL)

    def units(self):
        return sum(
            abs(v) for c, v in rel_events(self.device) if c == sm.REL_WHEEL_HI_RES
        )

    def test_a_short_push_at_wheel_speed_three_is_not_one_whole_click(self):
        # 0.4 s at a comfortable deflection, which is what a hand actually
        # does. The units add up and never reach 120, so a click consumer is
        # handed nothing at all while a browser zooms smoothly.
        self.use(self.ZOOM)
        self.settings["wheel_speed"] = 3.0
        self.tick(axes(y=150), dt=1 / 120.0, count=48)
        self.assertEqual(self.clicks(), 0)
        self.assertGreater(self.units(), 0)

    def test_the_same_push_at_twelve_reaches_a_click(self):
        self.use(self.ZOOM)
        self.settings["wheel_speed"] = 12.0
        self.tick(axes(y=150), dt=1 / 120.0, count=48)
        self.assertGreaterEqual(self.clicks(), 1)

    def test_a_profile_may_set_its_own_wheel_speed(self):
        spec = dict(self.ZOOM)
        spec["wheel_speed"] = 12.0
        self.use(spec)
        self.settings["wheel_speed"] = 3.0
        self.tick(axes(y=150), dt=1 / 120.0, count=48)
        self.assertGreaterEqual(self.clicks(), 1)

    def test_the_shipped_slicer_fallback_reaches_a_click_on_a_short_push(self):
        # The profile that still zooms with the wheel. Fusion's does not any
        # more: under the SolidWorks preset it drags instead, which is the
        # only way to get away from whole clicks under Xwayland.
        import json

        with open(sm.os.path.join(sm.repo_root(), "profiles.default.json")) as handle:
            spec = json.load(handle)
        fallback = [p for p in spec["profiles"] if p["name"] == "bambu-mouse-fallback"][
            0
        ]
        self.settings.update(spec["settings"])  # the shipped curve, not the fixture's
        self.settings["wheel_speed"] = 12.0
        self.use(fallback)
        self.tick(axes(y=150), dt=1 / 120.0, count=48)
        self.assertGreaterEqual(self.clicks(), 1)

    def test_scrolling_is_logged_with_both_numbers(self):
        lines = []
        engine = sm.GestureEngine(
            self.device,
            self.settings,
            clock=self.clock,
            sleep=self.sleep,
            log=lines.append,
        )
        self.settings["wheel_speed"] = 12.0
        engine.set_profile(sm.Profile(self.ZOOM))
        for _ in range(48):
            self.clock.advance(1 / 120.0)
            engine.tick(axes(y=150), 1 / 120.0)
        self.assertEqual(lines, [], "nothing is said while it is still scrolling")
        for _ in range(3):
            self.clock.advance(1 / 120.0)
            engine.tick(axes(), 1 / 120.0)
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^gesture zoom scrolled \d+ click\(s\), \d+ unit")

    def test_the_wheel_can_have_a_gentler_curve_than_the_pointer(self):
        # The pointer keeps its fractions and the wheel cannot: below a click
        # a wheel does nothing at all, so the range where the pointer merely
        # crawls is the range where the wheel is dead. A straighter curve for
        # the wheel alone lifts that range without touching full deflection.
        self.settings["curve"] = 1.3
        self.settings["wheel_speed"] = 12.0
        spec = dict(self.ZOOM)
        self.use(spec)
        self.tick(axes(y=100), dt=1 / 120.0, count=240)
        steep = self.clicks()
        self.setUp()
        self.settings["curve"] = 1.3
        self.settings["wheel_speed"] = 12.0
        spec = dict(self.ZOOM)
        spec["wheel_curve"] = 1.0
        self.use(spec)
        self.tick(axes(y=100), dt=1 / 120.0, count=240)
        self.assertGreater(self.clicks(), steep)

    def test_full_deflection_is_the_same_whatever_the_wheel_curve(self):
        # The curve may only change the shape of the travel, not its end.
        counts = []
        for wheel_curve in (1.3, 1.0):
            self.setUp()
            self.settings["curve"] = 1.3
            self.settings["wheel_speed"] = 12.0
            spec = dict(self.ZOOM)
            spec["wheel_curve"] = wheel_curve
            self.use(spec)
            self.tick(axes(y=350), dt=1 / 120.0, count=240)
            counts.append(self.clicks())
        self.assertEqual(counts[0], counts[1])

    def test_reshape_undoes_one_curve_and_applies_another(self):
        # 0.3 under curve 1.3 came from a deflection of 0.3 ** (1/1.3), and
        # that deflection under curve 1.0 is itself.
        self.assertAlmostEqual(sm.reshape(0.3, 1.3, 1.0), 0.3 ** (1 / 1.3), places=6)
        self.assertEqual(sm.reshape(0.4, 1.3, 1.3), 0.4, "same curve, same value")
        self.assertEqual(sm.reshape(0.0, 1.3, 1.0), 0.0)
        self.assertLess(sm.reshape(-0.3, 1.3, 1.0), 0, "the sign survives")
        self.assertAlmostEqual(sm.reshape(1.0, 1.3, 1.0), 1.0, places=9)

    def test_the_clicks_come_out_evenly_spaced(self):
        # Measured against a real X client at 338 ms, 210 ms and 153 ms per
        # click with a jitter of 3 ms or less. This is the same property in
        # the fake: with a steady deflection the gaps between clicks are
        # constant, so there is nothing bursty to smooth out in here.
        self.settings["wheel_speed"] = 12.0
        spec = dict(self.ZOOM)
        spec["wheel_curve"] = 1.0
        self.use(spec)
        at = []
        for tick in range(600):
            before = self.clicks()
            self.tick(axes(y=150), dt=1 / 120.0)
            if self.clicks() != before:
                at.append(tick)
        gaps = [b - a for a, b in zip(at, at[1:])]
        self.assertGreater(len(gaps), 4)
        self.assertLessEqual(max(gaps) - min(gaps), 1, "gaps: %s" % gaps)

    def test_a_brush_against_the_deadzone_is_not_worth_a_line(self):
        lines = []
        engine = sm.GestureEngine(
            self.device,
            self.settings,
            clock=self.clock,
            sleep=self.sleep,
            log=lines.append,
        )
        engine.set_profile(sm.Profile(self.ZOOM))
        # Just past the deadzone for a single tick: nothing is delivered, and
        # at 120 Hz saying so would drown the log it is meant to explain.
        self.clock.advance(1 / 120.0)
        engine.tick(axes(y=19), 1 / 120.0)
        self.clock.advance(1 / 120.0)
        engine.tick(axes(), 1 / 120.0)
        self.assertEqual(lines, [])

    def test_release_all_closes_an_open_scroll(self):
        lines = []
        engine = sm.GestureEngine(
            self.device,
            self.settings,
            clock=self.clock,
            sleep=self.sleep,
            log=lines.append,
        )
        engine.set_profile(sm.Profile(self.ZOOM))
        for _ in range(10):
            self.clock.advance(1 / 120.0)
            engine.tick(axes(y=350), 1 / 120.0)
        engine.release_all()
        self.assertEqual(len(lines), 1)
        self.assertIn("gesture zoom scrolled", lines[0])
        # And it does not say it twice.
        engine.release_all()
        self.assertEqual(len(lines), 1)


class GatedGroupTest(EngineFixture):
    """One axis, two zooms: `when` is what keeps them out of each other's way.

    A pointer can hold one drag. So zooming while orbiting cannot be a second
    drag, and the same puck axis has to be a smooth drag on its own and a
    stepped wheel on top of another gesture. Two gates do it: the drag zoom
    may only start from idle, and the wheel zoom may only run while a drag is.
    """

    HYBRID = {
        "name": "hybrid",
        "type": "mouse",
        "match": None,
        "wheel_speed": 12.0,
        "gestures": {
            "orbit": {
                "hold": ["middle"],
                "axes": {"rx": {"to": "dy"}, "ry": {"to": "dx"}},
            },
            "zoom": {
                "hold": ["shift", "middle"],
                "when": "idle",
                "speed": 0.1,
                "axes": {"y": {"to": "dy", "gain": 1.0}},
            },
            "zoom-step": {
                "mode": "wheel",
                "when": "drag",
                "axes": {"y": {"to": "wheel", "gain": -1.0}},
            },
        },
    }

    def wheeled(self):
        return sum(
            abs(v) for c, v in rel_events(self.device) if c == sm.REL_WHEEL_HI_RES
        )

    def test_lift_alone_is_the_smooth_drag_zoom(self):
        self.use(self.HYBRID)
        self.tick(axes(y=300), count=6)
        self.assertEqual(self.engine.active_name, "zoom")
        self.assertEqual(self.held(), [SHIFT, sm.BTN_MIDDLE])
        self.assertEqual(self.wheeled(), 0, "no wheel while it is the only gesture")

    def test_lift_during_an_orbit_becomes_the_wheel_instead(self):
        self.use(self.HYBRID)
        self.tick(axes(ry=300), count=3)
        self.assertEqual(self.engine.active_name, "orbit")
        self.device.records = []
        self.tick(axes(ry=300, y=300), count=12)
        self.assertEqual(self.engine.active_name, "orbit", "the orbit is not lost")
        self.assertEqual(self.held(), [sm.BTN_MIDDLE])
        self.assertGreater(self.wheeled(), 0, "and the lift zooms by wheel")

    def test_a_hard_lift_never_steals_a_running_orbit(self):
        # The one that matters: the hand is turning the model and lifts hard
        # to come closer. Losing the orbit there would be the worst outcome.
        self.use(self.HYBRID)
        self.tick(axes(ry=200), count=3)
        self.assertEqual(self.engine.active_name, "orbit")
        self.tick(axes(ry=60, y=350), count=40)
        self.assertEqual(self.engine.active_name, "orbit")
        self.assertEqual(self.held(), [sm.BTN_MIDDLE])

    def test_the_smooth_zoom_still_gives_way_to_a_deliberate_turn(self):
        # The gate is about not being stolen from, not about being immovable:
        # a dominant orbit may still take over from a running zoom.
        self.use(self.HYBRID)
        self.tick(axes(y=300), count=4)
        self.assertEqual(self.engine.active_name, "zoom")
        self.tick(axes(y=20, ry=350), count=10)
        self.assertEqual(self.engine.active_name, "orbit")

    def test_the_wheel_is_silent_while_nothing_is_dragging(self):
        self.use(self.HYBRID)
        # y alone starts the drag zoom, so drive an axis no group claims.
        spec = dict(self.HYBRID)
        spec["gestures"] = dict(self.HYBRID["gestures"])
        spec["gestures"].pop("zoom")
        self.use(spec)
        self.tick(axes(y=300), count=10)
        self.assertEqual(self.engine.active_name, "")
        self.assertEqual(self.wheeled(), 0, "gated to drags, and none is running")

    def test_a_wheel_group_needs_the_engage_gate_to_start(self):
        # Cross-talk: a hard twist puts something on the lift axis too, and a
        # wheel group that ran during another gesture would zoom on its own.
        # Same hysteresis as the drags: 24 counts to start, 18 to keep going.
        self.use(self.HYBRID)
        self.tick(axes(ry=300), count=3)
        self.device.records = []
        self.tick(axes(ry=300, y=21), count=20)
        self.assertEqual(self.wheeled(), 0, "below the engage gate, no zoom")
        self.tick(axes(ry=300, y=40), count=20)
        self.assertGreater(self.wheeled(), 0)
        self.device.records = []
        self.tick(axes(ry=300, y=21), count=20)
        self.assertGreater(
            self.wheeled(), 0, "once running it holds down to the deadzone"
        )

    def test_when_is_checked_when_the_profile_is_read(self):
        spec = dict(self.HYBRID)
        spec["gestures"] = {"zoom": {"mode": "wheel", "when": "sometimes", "axes": {}}}
        with self.assertRaises(ValueError):
            sm.Profile(spec)

    def test_an_ordinary_wheel_group_still_runs_whenever_it_likes(self):
        self.use(CAD_PROFILE)
        self.settings["wheel_speed"] = 12.0
        self.tick(axes(y=300), count=10)
        self.assertEqual(self.engine.active_name, "")
        self.assertGreater(
            sum(abs(v) for c, v in rel_events(self.device) if c == sm.REL_WHEEL_HI_RES),
            0,
        )


class ModifierOrderTest(EngineFixture):
    """A modifier has to be down, and seen to be down, before its button.

    Fusion under Wine is the reason this is not just tidiness: a shift+middle
    combo emitted in one frame never reached it as an orbit (0 of 9 live
    attempts), the same combo split into two frames 20 ms apart reached it
    every time (8 of 8). Everything below is that rule, written down.
    """

    def test_the_modifier_goes_out_in_a_frame_of_its_own(self):
        self.use(CAD_PROFILE)
        self.tick(axes(x=300), count=3)
        frames = key_batches(self.device)
        self.assertEqual(frames[0], [(SHIFT, 1)])
        self.assertEqual(frames[1], [(sm.BTN_MIDDLE, 1)])

    def test_the_lead_is_waited_out_between_the_two_frames(self):
        self.use(CAD_PROFILE)
        self.tick(axes(x=300), count=3)
        self.assertEqual(self.sleep.waits, [0.02])

    def test_the_lead_is_configurable_and_zero_only_drops_the_wait(self):
        self.settings["modifier_lead_ms"] = 0.0
        self.use(CAD_PROFILE)
        self.tick(axes(x=300), count=3)
        self.assertEqual(self.sleep.waits, [])
        frames = key_batches(self.device)
        self.assertEqual(frames[0], [(SHIFT, 1)])
        self.assertEqual(frames[1], [(sm.BTN_MIDDLE, 1)])

    def test_a_profile_may_set_its_own_lead(self):
        spec = dict(CAD_PROFILE)
        spec["modifier_lead_ms"] = 45
        self.use(spec)
        self.tick(axes(x=300), count=3)
        self.assertEqual(self.sleep.waits, [0.045])

    def test_a_gesture_without_a_modifier_waits_for_nothing(self):
        self.use(CAD_PROFILE)
        self.tick(axes(rx=300), count=3)
        self.assertEqual(self.sleep.waits, [])
        self.assertEqual(key_batches(self.device), [[(sm.BTN_MIDDLE, 1)]])

    def test_the_release_is_the_mirror_of_the_press(self):
        # The button lets go first and the modifier outlives it, because a
        # single frame would deliver the key first and end the drag as a plain
        # middle drag in the application's eyes.
        self.use(CAD_PROFILE)
        self.tick(axes(x=300), count=3)
        self.device.records = []
        self.sleep.waits = []
        self.tick(axes(), dt=0.05, count=4)
        self.assertEqual(self.held(), [])
        frames = key_batches(self.device)
        self.assertEqual(frames[0], [(sm.BTN_MIDDLE, 0)])
        self.assertEqual(frames[1], [(SHIFT, 0)])
        self.assertEqual(self.sleep.waits, [0.02])

    def test_a_switch_between_groups_keeps_both_halves_in_order(self):
        self.use(CAD_PROFILE)
        self.tick(axes(x=300), count=3)
        self.device.records = []
        self.tick(axes(rx=300), count=3)
        self.assertEqual(self.engine.active_name, "orbit")
        frames = key_batches(self.device)
        # pan lets go button-first, then shift, then orbit takes the button.
        self.assertEqual(frames[0], [(sm.BTN_MIDDLE, 0)])
        self.assertEqual(frames[1], [(SHIFT, 0)])
        self.assertEqual(frames[2], [(sm.BTN_MIDDLE, 1)])

    def test_a_clutch_keeps_the_modifier_down_across_the_recentre(self):
        # A clutch is one drag, not two, so the modifier must not blink: an
        # application that re-reads the world on a button change would see a
        # plain middle press in the middle of a shift+middle drag.
        class Cursor(object):
            def __init__(self):
                self.clutches = 0

            def begin(self, **kwargs):
                return True

            def moved(self, dx, dy):
                return self.clutches < 1

            def clutch(self):
                self.clutches += 1
                return True

            def end(self):
                return True

        cursor = Cursor()
        engine = sm.GestureEngine(
            self.device,
            self.settings,
            clock=self.clock,
            sleep=self.sleep,
            cursor=cursor,
        )
        engine.set_profile(sm.Profile(CAD_PROFILE))
        for _ in range(4):
            self.clock.advance(1 / 60.0)
            engine.tick(axes(x=300), 1 / 60.0)
        self.assertEqual(engine.clutches, 1)
        frames = key_batches(self.device)
        # shift down, middle down, then the clutch: middle alone, both ways.
        self.assertEqual(frames[0], [(SHIFT, 1)])
        self.assertEqual(frames[1], [(sm.BTN_MIDDLE, 1)])
        self.assertEqual(frames[2], [(sm.BTN_MIDDLE, 0)])
        self.assertEqual(frames[3], [(sm.BTN_MIDDLE, 1)])
        self.assertEqual(self.sleep.waits, [0.02], "a clutch waits for nothing")
        self.assertEqual(self.device.pressed, [SHIFT, sm.BTN_MIDDLE])

    def test_release_all_still_drops_everything_in_one_go(self):
        # The emergency path is not the place for ceremony: a stuck button is
        # worse than a modifier that leaves in the wrong order.
        self.use(CAD_PROFILE)
        self.tick(axes(x=300), count=3)
        self.device.records = []
        self.engine.release_all()
        self.assertEqual(self.held(), [])
        self.assertEqual(key_batches(self.device), [[(sm.BTN_MIDDLE, 0), (SHIFT, 0)]])

    def test_split_combo_keeps_each_side_in_its_own_order(self):
        codes = sm.parse_combo("ctrl+shift+middle")
        modifiers, buttons = sm.split_combo(codes)
        self.assertEqual(
            modifiers,
            [sm.KEY_NAMES["KEY_LEFTCTRL"], sm.KEY_NAMES["KEY_LEFTSHIFT"]],
        )
        self.assertEqual(buttons, [sm.BTN_MIDDLE])

    def test_combo_name_reads_back_what_a_profile_wrote(self):
        self.assertEqual(sm.combo_name(sm.parse_combo("shift+middle")), "shift+middle")
        self.assertEqual(sm.combo_name(sm.parse_combo(["ctrl", "left"])), "ctrl+left")
        self.assertEqual(sm.combo_name(sm.parse_combo("KEY_HOME")), "home")
        self.assertEqual(sm.combo_name([]), "")

    def test_the_active_gesture_is_named_in_the_log(self):
        lines = []
        engine = sm.GestureEngine(
            self.device,
            self.settings,
            clock=self.clock,
            sleep=self.sleep,
            log=lines.append,
        )
        engine.set_profile(sm.Profile(CAD_PROFILE))
        for _ in range(3):
            self.clock.advance(1 / 60.0)
            engine.tick(axes(x=300), 1 / 60.0)
        self.assertIn("gesture pan holding shift+middle", lines)


if __name__ == "__main__":
    unittest.main()
