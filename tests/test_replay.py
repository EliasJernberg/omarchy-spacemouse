"""End to end: the real daemon process, a recorded capture, the whole pipeline.

These run the daemon exactly the way systemd does, only with --dry-run (so the
kernel is never touched) and --replay (so the puck does not have to be in
anyone's hand). XDG_RUNTIME_DIR is redirected into a temporary directory, so a
daemon that happens to be running for real is left alone.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import DEFAULT_PROFILES, FIXTURE_BIN, REPO, sm  # noqa: E402

DAEMON = os.path.join(REPO, "daemon", "spacemoused.py")
CTL = os.path.join(REPO, "bin", "spacemouse-ctl")


def parse_trace(path):
    """(type, code, value) for every event the trace recorded."""
    events = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("syn "):
                continue
            for token in line[4:].split():
                events.append(tuple(int(part) for part in token.split(":")))
    return events


class ReplayRunTest(unittest.TestCase):
    """One full pass of the capture, start to finish, in a throwaway session."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="spacemouse-test-")
        cls.trace = os.path.join(cls.tmp, "trace.txt")
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = cls.tmp
        result = subprocess.run(
            [
                sys.executable,
                DAEMON,
                "--dry-run",
                "--trace",
                cls.trace,
                "--no-focus",
                "--profile",
                "default",
                "--config",
                os.path.join(cls.tmp, "does-not-exist.json"),
                "--defaults",
                DEFAULT_PROFILES,
                "--replay",
                FIXTURE_BIN,
                "--replay-speed",
                "12",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        cls.result = result
        cls.events = parse_trace(cls.trace)
        cls.status_path = os.path.join(cls.tmp, "omarchy-spacemouse", "status.json")

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_daemon_exits_cleanly(self):
        self.assertEqual(
            self.result.returncode, 0, self.result.stderr.decode("utf-8", "replace")
        )

    def test_the_capture_produced_pointer_motion(self):
        moves = [
            event
            for event in self.events
            if event[0] == sm.EV_REL and event[1] in (sm.REL_X, sm.REL_Y)
        ]
        self.assertGreater(len(moves), 20)

    def test_the_capture_produced_wheel_zoom(self):
        wheel = [
            event
            for event in self.events
            if event[0] == sm.EV_REL and event[1] in (sm.REL_WHEEL, sm.REL_WHEEL_HI_RES)
        ]
        self.assertTrue(wheel, "the lift axis in the capture should have zoomed")

    def test_both_drag_gestures_were_used(self):
        keys = [
            (code, value) for etype, code, value in self.events if etype == sm.EV_KEY
        ]
        self.assertIn(
            (sm.BTN_MIDDLE, 1),
            keys,
            "orbit or pan should have grabbed the middle button",
        )
        self.assertIn(
            (sm.KEY_NAMES["KEY_LEFTSHIFT"], 1), keys, "pan should have used shift"
        )

    def test_the_fit_button_reached_the_keyboard(self):
        keys = [
            (code, value) for etype, code, value in self.events if etype == sm.EV_KEY
        ]
        home = sm.KEY_NAMES["KEY_HOME"]
        self.assertIn((home, 1), keys)
        self.assertEqual(keys.count((home, 1)), keys.count((home, 0)))
        # The capture ends with three presses of the FIT button.
        self.assertEqual(keys.count((home, 1)), 3)

    def test_nothing_is_left_held_when_the_daemon_stops(self):
        depth = {}
        for etype, code, value in self.events:
            if etype != sm.EV_KEY:
                continue
            depth[code] = depth.get(code, 0) + (1 if value else -1)
            self.assertIn(depth[code], (0, 1), "code %d went out of balance" % code)
        stuck = [code for code, count in depth.items() if count != 0]
        self.assertEqual(stuck, [], "still held: %s" % stuck)

    def test_the_trace_ends_with_the_device_closed(self):
        with open(self.trace, "r", encoding="utf-8") as handle:
            lines = [line.strip() for line in handle if line.strip()]
        self.assertEqual(lines[0], "open Omarchy SpaceMouse")
        self.assertEqual(lines[-1], "close")

    def test_the_status_file_describes_the_run(self):
        with open(self.status_path, "r", encoding="utf-8") as handle:
            status = json.load(handle)
        self.assertEqual(status["profile"], "default")
        self.assertEqual(status["profile_type"], "mouse")
        self.assertEqual(status["mode"], "manual")
        # The last write happens during shutdown, after the sink is closed.
        self.assertEqual(status["uinput"], "closed")
        self.assertIn("default", [profile["name"] for profile in status["profiles"]])
        self.assertGreater(status["last_event"], 0)


class ControlSocketTest(unittest.TestCase):
    """spacemouse-ctl against a live daemon, in its own runtime directory."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="spacemouse-ctl-test-")
        cls.env = dict(os.environ)
        cls.env["XDG_RUNTIME_DIR"] = cls.tmp
        # A pipe nobody drains would block the daemon once it fills, so the log
        # goes to a file the tests can read whenever they like.
        cls.log = open(os.path.join(cls.tmp, "daemon.log"), "w", encoding="utf-8")
        cls.proc = subprocess.Popen(
            [
                sys.executable,
                DAEMON,
                "--dry-run",
                "--trace",
                os.path.join(cls.tmp, "trace.txt"),
                "--no-focus",
                "--config",
                os.path.join(cls.tmp, "does-not-exist.json"),
                "--defaults",
                DEFAULT_PROFILES,
                "--replay",
                FIXTURE_BIN,
                "--replay-speed",
                "6",
                "--replay-loop",
            ],
            env=cls.env,
            stdout=subprocess.DEVNULL,
            stderr=cls.log,
        )
        socket_path = os.path.join(cls.tmp, "omarchy-spacemouse", "control.sock")
        deadline = time.time() + 20
        while time.time() < deadline and not os.path.exists(socket_path):
            time.sleep(0.1)
        cls.socket_path = socket_path

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.communicate(timeout=15)
        cls.log.close()
        import shutil

        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def ctl_raw(cls, *args):
        return subprocess.run(
            [sys.executable, CTL] + list(args),
            env=cls.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

    def ctl(self, *args):
        result = self.ctl_raw(*args)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result.stdout.decode("utf-8")

    def ctl_json(self, *args):
        return json.loads(self.ctl(*(args + ("--json",))))

    def test_socket_exists(self):
        # Never read the daemon's stderr pipe here: it is still running, so the
        # read would block until it exits.
        self.assertTrue(
            os.path.exists(self.socket_path),
            "the daemon did not create %s" % self.socket_path,
        )
        self.assertIsNone(self.proc.poll(), "the daemon exited early")

    def test_status_reports_a_live_replay(self):
        status = self.ctl_json("status")
        self.assertEqual(status["spnav"], "replay")
        self.assertTrue(status["enabled"])
        self.assertEqual(status["mode"], "auto")
        self.assertEqual(status["uinput"], "dry-run")
        self.assertEqual(status["hyprland"], "disconnected")

    def test_disable_and_enable_round_trip(self):
        self.assertFalse(self.ctl_json("disable")["enabled"])
        self.assertEqual(self.ctl_json("status")["profile"], "off")
        self.assertTrue(self.ctl_json("enable")["enabled"])

    def test_toggle_flips_the_state(self):
        first = self.ctl_json("toggle")["enabled"]
        second = self.ctl_json("toggle")["enabled"]
        self.assertNotEqual(first, second)

    def test_manual_profile_override_and_back_to_auto(self):
        status = self.ctl_json("profile", "browser-threejs")
        self.assertEqual(status["mode"], "manual")
        self.assertEqual(status["profile"], "browser-threejs")
        status = self.ctl_json("auto")
        self.assertEqual(status["mode"], "auto")

    def test_unknown_profile_is_refused(self):
        result = self.ctl_raw("profile", "no-such-profile")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"no profile named", result.stderr)

    def test_profiles_lists_the_shipped_set(self):
        listing = self.ctl("profiles")
        for name in ("cad-native", "fusion-bifrost", "browser-threejs", "default"):
            self.assertIn(name, listing)

    def test_reload_keeps_the_daemon_alive(self):
        self.ctl("reload")
        self.assertIsNone(self.proc.poll())
        self.assertTrue(self.ctl_json("status")["profiles"])

    def test_status_file_is_written_next_to_the_socket(self):
        path = os.path.join(self.tmp, "omarchy-spacemouse", "status.json")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as handle:
            status = json.load(handle)
        self.assertEqual(status["pid"], self.proc.pid)


if __name__ == "__main__":
    unittest.main()
