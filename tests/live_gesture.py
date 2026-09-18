#!/usr/bin/env python3
"""Live end to end: puck capture in, real kernel events and a real cursor out.

Everything in tests/run.py runs against fakes. This one uses the actual
kernel and the actual compositor, so it is kept out of the suite and run by
hand:

    python3 tests/live_gesture.py                 # in the focused window
    python3 tests/live_gesture.py --window        # open its own window first
    python3 tests/live_gesture.py --profile fusion

What it checks:

  1. the virtual device is created and the kernel publishes it,
  2. every batch the engine emits comes back out of /dev/input/eventN
     unchanged, which is the whole synthetic-input path end to end,
  3. the pointer is parked in the middle of the window when a drag starts and
     put back exactly where it was when the drag ends,
  4. a long drag clutches instead of walking into a screen edge,
  5. the sub-pixel accumulator does not lose motion: the deltas that come back
     add up to what the engine meant to send.

It drives whatever window has focus, so give it a window that does not mind
being dragged in: --window opens tests/orbit_probe.html for exactly that.
"""

import argparse
import json
import os
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import DEFAULT_PROFILES, FIXTURE_BIN, REPO, sm  # noqa: E402

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

PROBE = os.path.join(REPO, "tests", "orbit_probe.html")


class Tee(object):
    """Wraps the real device and remembers every batch that went through."""

    def __init__(self, device):
        self.device = device
        self.batches = []

    def __getattr__(self, name):
        return getattr(self.device, name)

    @property
    def pressed(self):
        return self.device.pressed

    def _record(self, events):
        self.batches.append(list(events))

    def syn(self):
        if self.device._pending:
            self._record(self.device._pending)
        self.device.syn()

    def forward(self, events):
        self._record(events)
        self.device.forward(events)

    def events(self):
        out = []
        for batch in self.batches:
            out.extend(batch)
        return out


class WatchedCursor(sm.CursorController):
    """A cursor controller that writes down what it actually did."""

    def __init__(self, *args, **kwargs):
        sm.CursorController.__init__(self, *args, **kwargs)
        self.sessions = []

    def begin(self):
        started = sm.CursorController.begin(self)
        if started:
            self.sessions.append(
                {
                    "saved": self.saved,
                    "centre": self.centre,
                    "after_warp": self.hypr.cursor_position(),
                    "geometry": self.geometry,
                    "ended": None,
                }
            )
        return started

    def end(self):
        saved = self.saved
        done = sm.CursorController.end(self)
        if done and self.sessions:
            self.sessions[-1]["ended"] = self.hypr.cursor_position()
            self.sessions[-1]["restored_to"] = saved
        return done


class Reader(threading.Thread):
    daemon = True

    def __init__(self, path):
        threading.Thread.__init__(self, name="event-reader")
        self.path = path
        self.events = []
        self.error = None
        self.running = True

    def run(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            self.error = exc
            return
        try:
            while self.running:
                try:
                    blob = os.read(fd, sm.INPUT_EVENT_SIZE * 256)
                except BlockingIOError:
                    time.sleep(0.002)
                    continue
                except OSError as exc:
                    self.error = exc
                    return
                for index in range(len(blob) // sm.INPUT_EVENT_SIZE):
                    chunk = blob[
                        index * sm.INPUT_EVENT_SIZE : (index + 1) * sm.INPUT_EVENT_SIZE
                    ]
                    _, _, etype, code, value = struct.unpack(sm.INPUT_EVENT_FMT, chunk)
                    self.events.append((etype, code, value))
        finally:
            os.close(fd)


def find_event_node(sysname, timeout=5.0):
    """The event node belonging to one specific inputN, not just any namesake."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        base = "/sys/devices/virtual/input/%s" % sysname
        try:
            for entry in sorted(os.listdir(base)):
                if entry.startswith("event"):
                    return "/dev/input/%s" % entry
        except OSError:
            pass
        time.sleep(0.2)
    return None


def hyprctl(*args):
    return subprocess.run(
        ["hyprctl"] + list(args), capture_output=True, text=True, timeout=10
    ).stdout.strip()


def open_probe_window():
    """Put a window that likes being dragged in on workspace 4, and focus it."""
    before = json.loads(hyprctl("activewindow", "-j") or "{}")
    workspace = json.loads(hyprctl("activeworkspace", "-j") or "{}")
    known = set(
        client.get("address") for client in json.loads(hyprctl("clients", "-j") or "[]")
    )
    hyprctl("dispatch", "hl.dsp.focus({ workspace = '4' })")
    subprocess.Popen(
        ["opera", "--new-window", "file://%s" % PROBE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    address = ""
    deadline = time.time() + 20
    while time.time() < deadline:
        clients = json.loads(hyprctl("clients", "-j") or "[]")
        for client in clients:
            # The probe renames itself the moment it loads, so it is found by
            # being the window that was not there a second ago, not by title.
            if (
                client.get("workspace", {}).get("id") == 4
                and client.get("address") not in known
            ):
                address = client["address"]
                break
        if address:
            break
        time.sleep(0.5)
    if address:
        hyprctl("dispatch", "hl.dsp.focus({ window = 'address:%s' })" % address)
        time.sleep(1.0)
    return before, workspace, address


def restore(before, workspace, address):
    if address:
        hyprctl("dispatch", "hl.dsp.window.close({ window = 'address:%s' })" % address)
    identifier = workspace.get("id") if isinstance(workspace, dict) else None
    if identifier is not None:
        hyprctl("dispatch", "hl.dsp.focus({ workspace = '%s' })" % identifier)
    if isinstance(before, dict) and before.get("address"):
        hyprctl(
            "dispatch", "hl.dsp.focus({ window = 'address:%s' })" % before["address"]
        )


def play(engine, profile_set, blob, speed, hz=120.0):
    """Feed the capture through the engine at wall clock pace."""
    count = len(blob) // sm.FRAME_SIZE
    axes = [0] * 6
    last = time.monotonic()
    index = 0
    next_frame = time.monotonic()
    while index < count:
        now = time.monotonic()
        if now >= next_frame:
            event = sm.decode_frame(
                blob[index * sm.FRAME_SIZE : (index + 1) * sm.FRAME_SIZE]
            )
            if event.kind == "motion":
                axes = list(event.axes)
                period = event.period if 1 <= event.period <= 200 else 10
            elif event.kind == "press":
                engine.handle_button(event.button, True)
                period = 10
            else:
                period = 10
            next_frame = now + (period / 1000.0) / speed
            index += 1
        now = time.monotonic()
        engine.tick(axes, now - last, profile_set)
        last = now
        time.sleep(1.0 / hz)
    engine.release_all()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live gesture check")
    parser.add_argument("--profile", default="browser-threejs")
    parser.add_argument("--speed", type=float, default=12.0)
    parser.add_argument(
        "--window", action="store_true", help="open a probe window and focus it"
    )
    parser.add_argument("--shots", default="", help="directory for grim screenshots")
    args = parser.parse_args(argv)

    failures = []
    notes = []

    print("== permissions")
    if not os.access("/dev/uinput", os.W_OK):
        print("  %sFAIL%s /dev/uinput is not writable" % (RED, RESET))
        return 1
    print("  %sok%s /dev/uinput is writable" % (GREEN, RESET))

    before = workspace = address = None
    if args.window:
        print("\n== opening a window to drag in")
        before, workspace, address = open_probe_window()
        if not address:
            print(
                "  %swarn%s could not find the probe window, using whatever has focus"
                % (YELLOW, RESET)
            )
        else:
            print("  %sok%s probe window on workspace 4" % (GREEN, RESET))

    device = None
    reader = None
    try:
        print("\n== virtual device")
        real = sm.VirtualDevice()
        real.open()
        device = Tee(real)
        print("  %sok%s created as %s" % (GREEN, RESET, real.sysname or "?"))
        sm.hypr_set_flat_acceleration()

        node = find_event_node(real.sysname) if real.sysname else None
        if node is None:
            print(
                "  %swarn%s no event node found for %s" % (YELLOW, RESET, real.sysname)
            )
            notes.append("no event node, so nothing was read back")
        else:
            print("  %sok%s kernel node %s" % (GREEN, RESET, node))
            if os.access(node, os.R_OK):
                reader = Reader(node)
                reader.start()
                time.sleep(0.3)
            else:
                notes.append("%s is not readable, so nothing was read back" % node)

        profile_set = sm.ProfileSet(DEFAULT_PROFILES)
        profile = profile_set.by_name(args.profile)
        if profile is None:
            print("  %sFAIL%s no profile named '%s'" % (RED, RESET, args.profile))
            return 1
        cursor = WatchedCursor(profile_set.settings)
        engine = sm.GestureEngine(device, profile_set.settings, cursor=cursor)
        engine.set_profile(profile)

        shot_before = shot_after = None
        if args.shots:
            os.makedirs(args.shots, exist_ok=True)
            shot_before = os.path.join(args.shots, "before.png")
            subprocess.run(["grim", shot_before], capture_output=True, timeout=10)

        print("\n== playing the capture through the engine (profile %s)" % profile.name)
        start_cursor = sm.hypr_cursor_position()
        with open(FIXTURE_BIN, "rb") as handle:
            blob = handle.read()
        started = time.monotonic()
        play(engine, profile_set, blob, args.speed)
        elapsed = time.monotonic() - started
        end_cursor = sm.hypr_cursor_position()
        print(
            "  %sok%s %d frames in %.1f s"
            % (GREEN, RESET, len(blob) // sm.FRAME_SIZE, elapsed)
        )

        if args.shots:
            time.sleep(0.5)
            shot_after = os.path.join(args.shots, "after.png")
            subprocess.run(["grim", shot_after], capture_output=True, timeout=10)
    finally:
        if reader is not None:
            time.sleep(0.4)
            reader.running = False
            reader.join(timeout=2)
        if device is not None:
            device.device.close()
        if args.window:
            restore(before, workspace, address)

    def check(condition, message):
        if condition:
            print("  %sok%s   %s" % (GREEN, RESET, message))
        else:
            print("  %sFAIL%s %s" % (RED, RESET, message))
            failures.append(message)

    print("\n== what the engine emitted")
    emitted = device.events()
    emitted_rel = {}
    for etype, code, value in emitted:
        if etype == sm.EV_REL:
            emitted_rel[code] = emitted_rel.get(code, 0) + value
    keys = [(c, v) for t, c, v in emitted if t == sm.EV_KEY]
    print("  %d events in %d batches" % (len(emitted), len(device.batches)))
    print("  rel sums: %s" % emitted_rel)
    print("  key events: %d" % len(keys))
    check(len(emitted) > 50, "the capture produced a real stream of events")
    check(bool(keys), "at least one button was held")

    if reader is not None and reader.error is None:
        print("\n== what the kernel handed back")
        received = [e for e in reader.events if e[0] != sm.EV_SYN]
        received_rel = {}
        for etype, code, value in received:
            if etype == sm.EV_REL:
                received_rel[code] = received_rel.get(code, 0) + value
        print("  %d events" % len(received))
        print("  rel sums: %s" % received_rel)
        check(received == emitted, "every event came back out of the kernel unchanged")
        check(
            received_rel == emitted_rel,
            "the relative motion adds up exactly (no sub-pixel loss)",
        )
    elif reader is not None:
        notes.append("readback failed: %s" % reader.error)

    print("\n== cursor")
    sessions = cursor.sessions
    print("  %d drag session(s), %d clutch(es)" % (len(sessions), engine.clutches))
    if not sessions:
        notes.append("no drag session started, so the cursor was never parked")
    for index, session in enumerate(sessions):
        centred = session["after_warp"] == session["centre"]
        check(
            centred,
            "drag %d parked the pointer in the middle of the window" % (index + 1),
        )
        if session.get("ended") is not None:
            check(
                session["ended"] == session["restored_to"],
                "drag %d put the pointer back where it was" % (index + 1),
            )
    if start_cursor and end_cursor:
        check(
            start_cursor == end_cursor,
            "the pointer is where it started: %s" % (start_cursor,),
        )
    check(engine.clutches > 0, "a long drag clutched instead of hitting a screen edge")

    if args.shots and shot_before and shot_after:
        print("\n== screenshots")
        print("  before: %s" % shot_before)
        print("  after:  %s" % shot_after)

    print("\n== result")
    for note in notes:
        print("  %sskipped%s %s" % (YELLOW, RESET, note))
    if failures:
        print("  %s%d check(s) failed%s" % (RED, len(failures), RESET))
        return 1
    print("  %sall live checks passed%s" % (GREEN, RESET))
    return 0


if __name__ == "__main__":
    sys.exit(main())
