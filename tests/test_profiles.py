"""Profile matching, the shipped defaults, normalization and hot reload."""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import DEFAULT_PROFILES, sm  # noqa: E402


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


class ShippedDefaultsTest(unittest.TestCase):
    """The file the installer copies has to behave the way the README claims."""

    @classmethod
    def setUpClass(cls):
        cls.profiles = sm.ProfileSet(DEFAULT_PROFILES)

    def select(self, window_class):
        return self.profiles.select(window_class)

    def test_file_is_valid_json(self):
        with open(DEFAULT_PROFILES, "r", encoding="utf-8") as handle:
            json.load(handle)

    def test_cad_applications_are_native(self):
        for window_class in (
            "kicad",
            "pcbnew",
            "eeschema",
            "gerbview",
            "org.kicad.kicad",
            "FreeCAD",
            "Blender",
            "blender",
        ):
            profile = self.select(window_class)
            self.assertEqual(
                profile.type,
                "native",
                "%s should be left to spacenavd, got %s" % (window_class, profile.name),
            )

    def test_fusion_is_left_to_bifrost(self):
        profile = self.select("fusion360.exe")
        self.assertEqual(profile.name, "fusion-bifrost")
        self.assertEqual(profile.type, "native")

    def test_browsers_use_the_threejs_convention(self):
        for window_class in (
            "Opera",
            "chromium",
            "firefox",
            "Brave-browser",
            "google-chrome",
        ):
            profile = self.select(window_class)
            self.assertEqual(profile.name, "browser-threejs", window_class)
            self.assertEqual(profile.type, "mouse")
            gestures = dict((g.name, g) for g in profile.gestures)
            self.assertEqual(gestures["orbit"].hold, [sm.BTN_LEFT])
            self.assertEqual(gestures["pan"].hold, [sm.BTN_RIGHT])
            self.assertEqual(gestures["zoom"].axes["y"][0], "wheel")
            self.assertEqual(gestures["zoom"].hold, [])

    def test_unknown_application_falls_back_to_the_cad_convention(self):
        profile = self.select("SomeRandomViewer")
        self.assertEqual(profile.name, "default")
        self.assertEqual(profile.type, "mouse")
        gestures = dict((g.name, g) for g in profile.gestures)
        self.assertEqual(gestures["orbit"].hold, [sm.BTN_MIDDLE])
        self.assertEqual(
            gestures["pan"].hold, [sm.KEY_NAMES["KEY_LEFTSHIFT"], sm.BTN_MIDDLE]
        )
        self.assertEqual(gestures["zoom"].axes["y"][0], "wheel")

    def test_terminals_are_off_so_nothing_gets_pasted(self):
        for window_class in ("foot", "Alacritty", "kitty", "Spotify"):
            self.assertEqual(self.select(window_class).type, "off", window_class)

    def test_the_omarchy_tui_windows_are_off_too(self):
        # Omarchy launches its TUIs as org.omarchy.<name>, so the whole prefix
        # has to be covered, not just the terminal emulators by name.
        for window_class in ("org.omarchy.terminal", "org.omarchy.about",
                             "org.omarchy.btop", "org.omarchy.screensaver"):
            self.assertEqual(self.select(window_class).type, "off", window_class)

    def test_matching_is_case_insensitive(self):
        self.assertEqual(self.select("KiCad").type, "native")
        self.assertEqual(self.select("OPERA").name, "browser-threejs")

    def test_fit_button_is_bound_in_mouse_profiles(self):
        default = self.profiles.by_name("default")
        self.assertIn(5, default.buttons)
        self.assertEqual(default.buttons[5].action, "fit")
        self.assertEqual(default.fit_key, [sm.KEY_NAMES["KEY_HOME"]])

    def test_disabled_gestures_are_not_loaded(self):
        default = self.profiles.by_name("default")
        self.assertNotIn("twist", [gesture.name for gesture in default.gestures])

    def test_every_profile_has_a_unique_name(self):
        names = [profile.name for profile in self.profiles.profiles]
        self.assertEqual(len(names), len(set(names)))


class MatchingTest(unittest.TestCase):
    def build(self, specs, settings=None):
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        write_json(path, {"version": 1, "settings": settings or {}, "profiles": specs})
        return sm.ProfileSet(path)

    def test_first_match_wins(self):
        profiles = self.build(
            [
                {"name": "first", "match": "^foo", "type": "off"},
                {"name": "second", "match": "foobar", "type": "native"},
                {"name": "default", "match": None, "type": "mouse"},
            ]
        )
        self.assertEqual(profiles.select("foobar").name, "first")

    def test_fallback_is_used_when_nothing_matches(self):
        profiles = self.build(
            [
                {"name": "only", "match": "^nope$", "type": "off"},
                {"name": "fallback", "match": None, "type": "mouse"},
            ]
        )
        self.assertEqual(profiles.select("whatever").name, "fallback")
        self.assertEqual(profiles.select("").name, "fallback")

    def test_a_fallback_is_added_when_the_file_forgot_one(self):
        profiles = self.build([{"name": "only", "match": "^nope$", "type": "off"}])
        self.assertTrue(any(profile.is_fallback for profile in profiles.profiles))
        self.assertEqual(profiles.select("whatever").type, "mouse")

    def test_star_means_fallback(self):
        profiles = self.build([{"name": "everything", "match": "*", "type": "off"}])
        self.assertTrue(profiles.by_name("everything").is_fallback)

    def test_broken_profile_file_keeps_the_previous_set(self):
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        write_json(
            path,
            {
                "version": 1,
                "profiles": [
                    {"name": "good", "match": "^good$", "type": "off"},
                    {"name": "default", "match": None, "type": "mouse"},
                ],
            },
        )
        profiles = sm.ProfileSet(path)
        self.assertEqual(profiles.select("good").name, "good")

        # A profile with an unknown type is a hard error; the daemon must not
        # end up with an empty profile list because of a typo.
        write_json(
            path,
            {
                "version": 1,
                "profiles": [
                    {"name": "broken", "match": "^good$", "type": "teleport"},
                ],
            },
        )
        profiles.load()
        self.assertEqual(profiles.select("good").name, "good")
        self.assertTrue(profiles.error)

    def test_hot_reload_on_mtime(self):
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        write_json(
            path,
            {
                "version": 1,
                "profiles": [{"name": "before", "match": None, "type": "off"}],
            },
        )
        profiles = sm.ProfileSet(path)
        self.assertEqual(profiles.select("x").name, "before")
        self.assertFalse(profiles.reload_if_changed())

        time.sleep(0.01)
        write_json(
            path,
            {
                "version": 1,
                "profiles": [{"name": "after", "match": None, "type": "mouse"}],
            },
        )
        os.utime(path, (time.time() + 1, time.time() + 1))
        self.assertTrue(profiles.reload_if_changed())
        self.assertEqual(profiles.select("x").name, "after")

    def test_missing_file_falls_back_to_the_shipped_defaults(self):
        profiles = sm.ProfileSet("/nonexistent/profiles.json", DEFAULT_PROFILES)
        self.assertEqual(profiles.select("kicad").type, "native")

    def test_unknown_axis_in_a_gesture_is_refused(self):
        with self.assertRaises(ValueError):
            sm.Profile(
                {
                    "name": "bad",
                    "type": "mouse",
                    "gestures": {"orbit": {"axes": {"xyz": {"to": "dx"}}}},
                }
            )

    def test_unknown_target_in_a_gesture_is_refused(self):
        with self.assertRaises(ValueError):
            sm.Profile(
                {
                    "name": "bad",
                    "type": "mouse",
                    "gestures": {"orbit": {"axes": {"x": {"to": "teleport"}}}},
                }
            )

    def test_unknown_key_name_is_refused(self):
        with self.assertRaises(ValueError):
            sm.Profile(
                {
                    "name": "bad",
                    "type": "mouse",
                    "gestures": {
                        "orbit": {"hold": ["nosuchkey"], "axes": {"x": {"to": "dx"}}}
                    },
                }
            )


class KeyResolutionTest(unittest.TestCase):
    def test_mouse_buttons(self):
        self.assertEqual(sm.resolve_token("middle"), sm.BTN_MIDDLE)
        self.assertEqual(sm.resolve_token("LEFT"), sm.BTN_LEFT)
        self.assertEqual(sm.resolve_token("BTN_RIGHT"), sm.BTN_RIGHT)

    def test_modifier_aliases(self):
        self.assertEqual(sm.resolve_token("shift"), sm.KEY_NAMES["KEY_LEFTSHIFT"])
        self.assertEqual(sm.resolve_token("ctrl"), sm.KEY_NAMES["KEY_LEFTCTRL"])
        self.assertEqual(sm.resolve_token("super"), sm.KEY_NAMES["KEY_LEFTMETA"])

    def test_bare_and_prefixed_key_names(self):
        self.assertEqual(sm.resolve_token("home"), sm.KEY_NAMES["KEY_HOME"])
        self.assertEqual(sm.resolve_token("KEY_F5"), sm.KEY_NAMES["KEY_F5"])
        self.assertEqual(sm.resolve_token("f5"), sm.KEY_NAMES["KEY_F5"])

    def test_combinations_keep_their_order(self):
        self.assertEqual(
            sm.parse_combo("ctrl+shift+home"),
            [
                sm.KEY_NAMES["KEY_LEFTCTRL"],
                sm.KEY_NAMES["KEY_LEFTSHIFT"],
                sm.KEY_NAMES["KEY_HOME"],
            ],
        )
        self.assertEqual(
            sm.parse_combo(["shift", "middle"]),
            [sm.KEY_NAMES["KEY_LEFTSHIFT"], sm.BTN_MIDDLE],
        )

    def test_empty_combo(self):
        self.assertEqual(sm.parse_combo(None), [])
        self.assertEqual(sm.parse_combo(""), [])


class NormalizeTest(unittest.TestCase):
    def setUp(self):
        self.settings = dict(sm.DEFAULT_SETTINGS)
        self.settings["curve"] = 1.0
        self.settings["deadzone"] = 30.0
        self.settings["full_scale"] = 350.0

    def norm(self, raw):
        return sm.normalize_axes(raw, self.settings)

    def test_resting_noise_is_zero(self):
        # Measured resting values on this puck reach 20 counts, with up to 50
        # of cross-talk while another axis moves; the deadzone eats the former.
        out = self.norm([4, -18, 0, 20, 0, 0])
        self.assertEqual(set(out.values()), {0.0})

    def test_full_deflection_is_one(self):
        out = self.norm([350, 0, 0, 0, 0, 0])
        self.assertAlmostEqual(out["x"], 1.0, places=6)

    def test_sign_is_kept(self):
        out = self.norm([-350, 0, 0, 0, 0, 0])
        self.assertAlmostEqual(out["x"], -1.0, places=6)

    def test_beyond_full_scale_is_clamped(self):
        out = self.norm([900, 0, 0, 0, 0, 0])
        self.assertAlmostEqual(out["x"], 1.0, places=6)

    def test_curve_softens_small_deflections(self):
        self.settings["curve"] = 2.0
        soft = self.norm([190, 0, 0, 0, 0, 0])["x"]
        self.settings["curve"] = 1.0
        linear = self.norm([190, 0, 0, 0, 0, 0])["x"]
        self.assertLess(soft, linear)

    def test_invert_flips_one_axis_only(self):
        self.settings["axis_invert"] = dict(self.settings["axis_invert"])
        self.settings["axis_invert"]["y"] = True
        out = self.norm([350, 350, 0, 0, 0, 0])
        self.assertGreater(out["x"], 0)
        self.assertLess(out["y"], 0)

    def test_gain_and_sensitivity_scale(self):
        self.settings["axis_gain"] = dict(self.settings["axis_gain"])
        self.settings["axis_gain"]["x"] = 0.5
        out = self.norm([350, 0, 0, 0, 0, 0])
        self.assertAlmostEqual(out["x"], 0.5, places=6)


if __name__ == "__main__":
    unittest.main()


class FocusGraceTest(unittest.TestCase):
    """Nothing is armed until the focused window has actually been read."""

    def daemon(self, *extra):
        args = sm.build_parser().parse_args(
            ["--config", "/nonexistent/profiles.json", "--defaults", DEFAULT_PROFILES] + list(extra))
        return sm.SpaceMouseDaemon(args)

    def test_no_profile_is_applied_before_focus_is_known(self):
        daemon = self.daemon()
        self.assertFalse(daemon.focus_is_known())
        self.assertIs(daemon.select_for("kicad"), sm.OFF_PROFILE)
        self.assertIs(daemon.select_for("Opera"), sm.OFF_PROFILE)

    def test_the_first_focus_reading_arms_it(self):
        daemon = self.daemon()
        daemon.on_focus("kicad", "KiCad")
        self.assertTrue(daemon.focus_is_known())
        self.assertEqual(daemon.select_for("kicad").type, "native")

    def test_an_empty_class_still_counts_as_a_reading(self):
        daemon = self.daemon()
        daemon.on_focus("", "")
        self.assertTrue(daemon.focus_is_known())

    def test_the_grace_period_falls_back_when_hyprland_never_answers(self):
        daemon = self.daemon()
        daemon.started_at -= sm.FOCUS_GRACE_SECONDS + 1
        self.assertTrue(daemon.focus_is_known())
        self.assertEqual(daemon.select_for("whatever").name, "default")

    def test_no_focus_mode_is_armed_immediately(self):
        daemon = self.daemon("--no-focus")
        self.assertTrue(daemon.focus_is_known())
        self.assertEqual(daemon.select_for("Opera").name, "browser-threejs")

    def test_an_empty_workspace_emits_nothing(self):
        daemon = self.daemon()
        daemon.on_focus("", "")          # Hyprland's "activewindow>>," 
        self.assertIs(daemon.select_for(""), sm.OFF_PROFILE)

    def test_a_window_without_a_class_still_gets_the_fallback(self):
        # Some applications set no app_id at all, but they do have a title.
        daemon = self.daemon()
        daemon.on_focus("", "Oden Scope")
        self.assertEqual(daemon.select_for("").name, "default")

    def test_a_manual_override_wins_over_an_empty_workspace(self):
        daemon = self.daemon()
        daemon.on_focus("", "")
        daemon.manual_profile = "browser-threejs"
        self.assertEqual(daemon.select_for("").name, "browser-threejs")

    def test_disabled_beats_everything(self):
        daemon = self.daemon("--no-focus")
        daemon.enabled = False
        self.assertIs(daemon.select_for("Opera"), sm.OFF_PROFILE)
