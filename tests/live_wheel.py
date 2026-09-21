#!/usr/bin/env python3
"""Live: how much of a wheel gesture survives as far as whole clicks?

A wheel gesture is emitted as a high resolution stream, 120 units to a click.
A browser acts on every unit; everything on Xwayland acts on whole clicks and
nothing else, so the same emission that zooms a Three.js viewer smoothly can
reach a Wine application as literally zero events. That is what happened to
Fusion's zoom, and this is the measurement that says so.

    python3 tests/live_wheel.py                        # the fusion profile
    python3 tests/live_wheel.py --profile browser-threejs
    python3 tests/live_wheel.py --deflection 150 --hold 0.4 --wheel-speed 3

It opens the same X window tests/live_modifier.py uses, plays the profile's
wheel gesture through the real engine, and counts two things: the units the
engine emitted, and the wheel buttons the client actually received (X calls
them buttons 4 and 5). Zero clicks against a pile of units is the failure
this exists to name.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import DEFAULT_PROFILES, sm  # noqa: E402
from live_modifier import (  # noqa: E402
    GREEN,
    RED,
    RESET,
    YELLOW,
    build_probe,
    find_probe_window,
    hyprctl,
    parse_events,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live wheel check")
    parser.add_argument("--profile", default="fusion")
    parser.add_argument("--gesture", default="", help="default: the first wheel group")
    parser.add_argument(
        "--deflection",
        type=int,
        default=150,
        help="raw counts on the gesture's axis; 150 is a comfortable half",
    )
    parser.add_argument("--hold", type=float, default=0.4, help="seconds to push")
    parser.add_argument(
        "--wheel-speed",
        type=float,
        default=0.0,
        help="override the profile's wheel_speed for this run",
    )
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
    wheels = profile.wheel_gestures
    if args.gesture:
        wheels = [g for g in wheels if g.name == args.gesture]
    if not wheels:
        print(
            "  %sFAIL%s profile '%s' has no wheel gesture%s"
            % (
                RED,
                RESET,
                profile.name,
                " called " + args.gesture if args.gesture else "",
            )
        )
        return 1
    gesture = wheels[0]

    axes = [0] * len(sm.AXES)
    for axis in gesture.axes:
        axes[sm.AXES.index(axis)] = args.deflection
    settings = dict(profile_set.settings)
    if args.wheel_speed:
        settings["wheel_speed"] = args.wheel_speed
        profile.overrides.pop("wheel_speed", None)
    speed = profile.number("wheel_speed", settings, 3.0)

    workdir = tempfile.mkdtemp(prefix="spacemouse-wheel-")
    binary, error = build_probe(workdir)
    if binary is None:
        print("  %sFAIL%s %s" % (RED, RESET, error))
        return 1

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

    device = None
    emitted = {"units": 0, "detents": 0}
    try:
        window = find_probe_window()
        if window is None:
            print("  %sFAIL%s the probe window never appeared" % (RED, RESET))
            return 1
        device = sm.VirtualDevice()
        device.open()
        real_wheel = device.wheel

        def counting_wheel(detents=0, hi_res=0, horizontal=False):
            emitted["units"] += abs(hi_res)
            emitted["detents"] += abs(detents)
            return real_wheel(detents=detents, hi_res=hi_res, horizontal=horizontal)

        device.wheel = counting_wheel
        engine = sm.GestureEngine(device, settings, cursor=None)
        engine.set_profile(profile)

        sm.hypr_focus_window(window["address"])
        x = int(window["at"][0]) + int(window["size"][0]) // 2
        y = int(window["at"][1]) + int(window["size"][1]) // 2
        sm.hypr_warp_cursor(x, y)
        time.sleep(0.25)
        print(
            "== %s/%s at %d counts for %.2f s, wheel_speed %.1f"
            % (profile.name, gesture.name, args.deflection, args.hold, speed)
        )
        last = time.monotonic()
        deadline = last + args.hold
        while time.monotonic() < deadline:
            now = time.monotonic()
            engine.tick(axes, now - last, profile_set)
            last = now
            time.sleep(1 / 120.0)
        engine.release_all()
        time.sleep(0.3)
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
        events = parse_events(handle.read())
    clicks = [
        e for e in events if e["kind"] == "ButtonPress" and e.get("button") in (4, 5)
    ]
    shutil.rmtree(workdir, ignore_errors=True)

    print("\n== what went out and what arrived")
    print(
        "  emitted   %d high resolution unit(s), %d whole detent(s)"
        % (emitted["units"], emitted["detents"])
    )
    print("  arrived   %d wheel click(s) at the X client" % len(clicks))
    print(
        "  a click is 120 units, so this push was worth %.2f of one"
        % (emitted["units"] / 120.0)
    )

    print("\n== verdict")
    if not emitted["units"]:
        print("  %sFAIL%s the gesture emitted nothing at all" % (RED, RESET))
        return 1
    if not clicks:
        print(
            "  %sFAIL%s %d unit(s) and not one whole click: an application that"
            " only understands clicks, which is everything on Xwayland, saw"
            " nothing. Raise wheel_speed for this profile."
            % (RED, RESET, emitted["units"])
        )
        return 1
    print(
        "  %sok%s   %d click(s) reached the client, so a click consumer sees this"
        % (GREEN, RESET, len(clicks))
    )
    if len(clicks) < 2 and args.hold >= 0.4:
        print(
            "  %swarn%s only just: a shorter or gentler push would deliver none"
            % (YELLOW, RESET)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
