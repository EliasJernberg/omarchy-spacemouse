#!/usr/bin/env python3
"""Live: does the modifier in a gesture's combo reach an Xwayland client?

Every live test so far drove a profile whose gestures hold nothing but a mouse
button, so the modifier path had never been watched end to end. Fusion's orbit
is shift plus the middle button, and when Fusion reads the modifier as up the
orbit silently becomes a pan: the object slides sideways instead of turning.

This opens a small X window (tests/modifier_probe.c, built on the fly), points
the compositor at it, plays one real gesture through a real GestureEngine on a
real /dev/uinput device, and reads back what the client actually got:

    python3 tests/live_modifier.py                  # the shipped lead
    python3 tests/live_modifier.py --lead 0         # everything in one batch
    python3 tests/live_modifier.py --profile default --gesture pan

What it checks:

  1. a Shift KeyPress arrives at all (the virtual device has a keyboard seat),
  2. it arrives before the ButtonPress, not after or alongside it,
  3. the ButtonPress itself carries ShiftMask, which is the bit Wine copies
     into its own key state, and therefore the bit Fusion reads,
  4. the release mirrors it: the button goes up while shift is still down.

It borrows the focus for about three seconds and puts the pointer and the
focused window back where it found them.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import DEFAULT_PROFILES, REPO, sm  # noqa: E402

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

PROBE_SOURCE = os.path.join(REPO, "tests", "modifier_probe.c")
PROBE_CLASS = "spacemouse-probe"

# X11 keycode 50 is Shift_L on a standard keymap, but the probe prints the
# keysym name, so the name is what this matches on.
SHIFT_SYMS = ("Shift_L", "Shift_R")
SHIFT_MASK = 0x0001

EVENT_RE = re.compile(
    r"^\s*(?P<t>[-\d.]+)\s+(?P<kind>\w+)\s+(?P<rest>.*)$",
)


def hyprctl(*args):
    return subprocess.run(
        ["hyprctl"] + list(args), capture_output=True, text=True, timeout=10
    ).stdout.strip()


def build_probe(workdir):
    binary = os.path.join(workdir, "modifier_probe")
    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        return None, "no C compiler (cc/gcc) to build the probe with"
    result = subprocess.run(
        [compiler, "-O2", "-o", binary, PROBE_SOURCE, "-lX11"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        return None, "the probe did not build: %s" % result.stderr.strip()
    return binary, ""


def find_probe_window(timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for client in json.loads(hyprctl("clients", "-j") or "[]"):
            if str(client.get("class", "")).lower() == PROBE_CLASS:
                return client
        time.sleep(0.2)
    return None


def parse_events(text):
    """The probe's lines as dicts, keeping the fields this test asks about."""
    events = []
    for line in text.splitlines():
        match = EVENT_RE.match(line)
        if not match:
            continue
        kind = match.group("kind")
        if kind not in (
            "KeyPress",
            "KeyRelease",
            "ButtonPress",
            "ButtonRelease",
            "MotionNotify",
            "FocusIn",
            "FocusOut",
        ):
            continue
        rest = match.group("rest")
        event = {"t": float(match.group("t")), "kind": kind}
        for field in ("keycode", "button", "state"):
            found = re.search(r"\b%s=(\w+)" % field, rest)
            if found:
                event[field] = int(found.group(1), 0)
        sym = re.search(r"\bsym=(\S+)", rest)
        if sym:
            event["sym"] = sym.group(1)
        events.append(event)
    return events


def play(engine, profile_set, axes, seconds, hz=120.0):
    """Hold the puck at one deflection, then let it go the way a hand does.

    The letting go matters as much as the taking hold: the gesture has to end
    through the idle release, which is the path the daemon takes when a hand
    comes off the puck, and not through release_all(), which is the emergency
    path and deliberately drops everything in one frame.
    """
    zero = [0] * len(axes)
    idle = engine.profile.number("idle_release_ms", engine.settings, 80.0) / 1000.0
    last = time.monotonic()
    deadline = last + seconds
    while time.monotonic() < deadline:
        now = time.monotonic()
        engine.tick(axes, now - last, profile_set)
        last = now
        time.sleep(1.0 / hz)
    deadline = time.monotonic() + idle + 0.3
    while time.monotonic() < deadline and engine.active is not None:
        now = time.monotonic()
        engine.tick(zero, now - last, profile_set)
        last = now
        time.sleep(1.0 / hz)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live modifier check")
    parser.add_argument("--profile", default="fusion")
    parser.add_argument(
        "--gesture",
        default="orbit",
        help="which gesture of the profile to drive (it needs a modifier)",
    )
    parser.add_argument(
        "--lead",
        type=float,
        default=None,
        help="modifier_lead_ms for this run; 0 reproduces the one-batch press",
    )
    parser.add_argument("--hold", type=float, default=0.6, help="seconds to drag")
    parser.add_argument(
        "--pointer-speed",
        type=float,
        default=60.0,
        help="pixels per second per unit of deflection; the default keeps the"
        " drag inside the probe window, the shipped 900 walks out of it",
    )
    parser.add_argument("--keep-log", default="", help="copy the probe log here")
    args = parser.parse_args(argv)

    if not os.access("/dev/uinput", os.W_OK):
        print("  %sFAIL%s /dev/uinput is not writable" % (RED, RESET))
        return 1
    if not os.environ.get("DISPLAY"):
        print("  %sFAIL%s no DISPLAY, so there is no Xwayland to ask" % (RED, RESET))
        return 1

    profile_set = sm.ProfileSet(DEFAULT_PROFILES)
    profile = profile_set.by_name(args.profile)
    if profile is None:
        print("  %sFAIL%s no profile named '%s'" % (RED, RESET, args.profile))
        return 1
    gesture = None
    for candidate in profile.drag_gestures:
        if candidate.name == args.gesture:
            gesture = candidate
    if gesture is None:
        print(
            "  %sFAIL%s profile '%s' has no drag gesture '%s'"
            % (RED, RESET, profile.name, args.gesture)
        )
        return 1
    modifiers = [code for code in gesture.hold if code not in sm.MOUSE_BUTTON_CODES]
    if not modifiers:
        print(
            "  %sFAIL%s gesture '%s' holds no modifier, nothing to measure"
            % (RED, RESET, gesture.name)
        )
        return 1

    # Drive exactly this gesture's axes, hard enough to pass the deadzone and
    # no harder, and slow the pointer right down so the drag stays inside the
    # probe window.
    axes = [0] * len(sm.AXES)
    for axis in gesture.axes:
        axes[sm.AXES.index(axis)] = 200
    settings = dict(profile_set.settings)
    settings["pointer_speed"] = args.pointer_speed
    if args.lead is not None:
        settings["modifier_lead_ms"] = args.lead

    workdir = tempfile.mkdtemp(prefix="spacemouse-modifier-")
    binary, error = build_probe(workdir)
    if binary is None:
        print("  %sFAIL%s %s" % (RED, RESET, error))
        return 1
    print("== probe built")

    before = json.loads(hyprctl("activewindow", "-j") or "{}")
    saved_cursor = sm.hypr_cursor_position()
    log_path = os.path.join(workdir, "probe.log")
    log = open(log_path, "w")
    seconds = args.hold + 2.5
    child = subprocess.Popen(
        [binary, "%.1f" % seconds],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    # The device is opened before the focus is borrowed, because opening it
    # waits for udev and the person at the keyboard should not have to wait
    # with it: the probe window holds the focus for about a second in total.
    device = None
    window = None
    stolen = ""
    try:
        window = find_probe_window()
        if window is None:
            print("  %sFAIL%s the probe window never appeared" % (RED, RESET))
            return 1
        device = sm.VirtualDevice()
        device.open()
        engine = sm.GestureEngine(device, settings, cursor=None)
        engine.set_profile(profile)
        lead = profile.number("modifier_lead_ms", settings, 0.0)

        sm.hypr_focus_window(window["address"])
        x = int(window["at"][0]) + int(window["size"][0]) // 2
        y = int(window["at"][1]) + int(window["size"][1]) // 2
        sm.hypr_warp_cursor(x, y)
        time.sleep(0.25)
        stolen = str((sm.hypr_json("j/activewindow") or {}).get("address") or "")
        print("== window %s focused, pointer at %d,%d" % (window["address"], x, y))
        print(
            "== playing %s/%s (modifier lead %.0f ms)"
            % (profile.name, gesture.name, lead)
        )
        play(engine, profile_set, axes, args.hold)
        engine.release_all()
        time.sleep(0.2)
    finally:
        if saved_cursor:
            sm.hypr_warp_cursor(*saved_cursor)
        if isinstance(before, dict) and before.get("address"):
            sm.hypr_focus_window(before["address"])
        if device is not None:
            device.close()
        try:
            child.wait(timeout=seconds + 3)
        except subprocess.TimeoutExpired:
            child.terminate()
        log.close()

    with open(log_path) as handle:
        text = handle.read()
    if args.keep_log:
        shutil.copyfile(log_path, args.keep_log)
    events = parse_events(text)

    print("\n== what the X client saw")
    for event in events:
        if event["kind"] in ("MotionNotify", "FocusIn"):
            continue
        print(
            "  %8.1f ms  %-13s %s state=0x%04x%s"
            % (
                event["t"],
                event["kind"],
                event.get("sym") or "button=%s" % event.get("button", "?"),
                event.get("state", 0),
                "  SHIFT" if event.get("state", 0) & SHIFT_MASK else "",
            )
        )
    motions = [e for e in events if e["kind"] == "MotionNotify"]
    if motions:
        print(
            "  (%d motion notifications, first state 0x%04x)"
            % (len(motions), motions[0].get("state", 0))
        )

    failures = []

    def check(condition, message):
        if condition:
            print("  %sok%s   %s" % (GREEN, RESET, message))
        else:
            print("  %sFAIL%s %s" % (RED, RESET, message))
            failures.append(message)

    shift_down = [
        e for e in events if e["kind"] == "KeyPress" and e.get("sym") in SHIFT_SYMS
    ]
    shift_up = [
        e for e in events if e["kind"] == "KeyRelease" and e.get("sym") in SHIFT_SYMS
    ]
    button_down = [e for e in events if e["kind"] == "ButtonPress"]
    button_up = [e for e in events if e["kind"] == "ButtonRelease"]
    # A window that loses the focus mid-run measured nothing: the events went
    # somewhere else. That is the machine being used while this ran, not a
    # verdict about the daemon, so it is called by name.
    focus_lost = [
        e
        for e in events
        if e["kind"] == "FocusOut"
        and shift_down
        and e["t"] > shift_down[0]["t"]
        and (not button_up or e["t"] < button_up[-1]["t"])
    ]

    print("\n== verdict")
    if focus_lost:
        print(
            "  %swarn%s the probe lost the focus %.0f ms in, mid-gesture: the rest"
            " of the run went to another window, so run it again on a quiet"
            " machine" % (YELLOW, RESET, focus_lost[0]["t"])
        )
        failures.append("the probe lost the focus mid-gesture")
    if stolen and window is not None and stolen != window["address"]:
        print(
            "  %swarn%s the focus was on %s, not the probe: something else was"
            " being used while this ran, so the result means nothing"
            % (YELLOW, RESET, stolen)
        )
        failures.append("the probe did not hold the focus")
    check(bool(shift_down), "the client got a Shift KeyPress")
    check(bool(button_down), "the client got a ButtonPress")
    if shift_down and button_down:
        check(
            shift_down[0]["t"] < button_down[0]["t"],
            "shift arrived %.1f ms before the button"
            % (button_down[0]["t"] - shift_down[0]["t"]),
        )
        check(
            bool(button_down[0].get("state", 0) & SHIFT_MASK),
            "the ButtonPress carries ShiftMask (state=0x%04x)"
            % button_down[0].get("state", 0),
        )
    if button_up and shift_up:
        check(
            button_up[-1]["t"] <= shift_up[-1]["t"],
            "the button went up while shift was still held",
        )
        check(
            bool(button_up[-1].get("state", 0) & SHIFT_MASK),
            "the ButtonRelease carries ShiftMask (state=0x%04x)"
            % button_up[-1].get("state", 0),
        )

    print("\n== result   log: %s" % (args.keep_log or log_path))
    if failures:
        print("  %s%d check(s) failed%s" % (RED, len(failures), RESET))
        return 1
    print("  %sthe modifier reaches the client before the button%s" % (GREEN, RESET))
    return 0


if __name__ == "__main__":
    sys.exit(main())
