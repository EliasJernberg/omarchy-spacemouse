#!/usr/bin/env python3
"""Live check against the real kernel: create the device, read its events back.

Everything else in tests/ runs against fakes. This one needs hardware access,
so it is not part of the suite:

    python3 tests/live_check.py            # create, emit, read back, report
    python3 tests/live_check.py --keep     # leave the device alive for 30 s

What it does:

  1. opens /dev/uinput and creates "Omarchy SpaceMouse",
  2. finds the /dev/input/eventN the kernel gave it, by name,
  3. reads that node in a thread while the recorded capture is played through
     the real gesture engine with the shipped `default` profile,
  4. checks that the events that come back out of the kernel are the ones the
     engine put in: the middle button pressed and released, pointer motion in
     both axes, wheel, and the FIT button's key taps.

Reading the event node needs permission of its own (see the README's udev
section). Without it the emit half still runs and the readback is reported as
skipped rather than failed.
"""

import argparse
import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import DEFAULT_PROFILES, FIXTURE_BIN, sm  # noqa: E402

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

CODE_NAMES = {
    (sm.EV_KEY, sm.BTN_LEFT): "BTN_LEFT",
    (sm.EV_KEY, sm.BTN_RIGHT): "BTN_RIGHT",
    (sm.EV_KEY, sm.BTN_MIDDLE): "BTN_MIDDLE",
    (sm.EV_KEY, sm.KEY_NAMES["KEY_LEFTSHIFT"]): "KEY_LEFTSHIFT",
    (sm.EV_KEY, sm.KEY_NAMES["KEY_HOME"]): "KEY_HOME",
    (sm.EV_REL, sm.REL_X): "REL_X",
    (sm.EV_REL, sm.REL_Y): "REL_Y",
    (sm.EV_REL, sm.REL_WHEEL): "REL_WHEEL",
    (sm.EV_REL, sm.REL_WHEEL_HI_RES): "REL_WHEEL_HI_RES",
    (sm.EV_SYN, sm.SYN_REPORT): "SYN_REPORT",
}


def name_of(etype, code):
    return CODE_NAMES.get((etype, code), "type %d code %d" % (etype, code))


def find_event_node(device_name, timeout=5.0):
    """The /dev/input/eventN whose device name is device_name."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for entry in sorted(os.listdir("/sys/class/input")):
            if not entry.startswith("event"):
                continue
            try:
                with open("/sys/class/input/%s/device/name" % entry, "r") as handle:
                    if handle.read().strip() == device_name:
                        return "/dev/input/%s" % entry
            except OSError:
                continue
        time.sleep(0.2)
    return None


class EventReader(threading.Thread):
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
                    blob = os.read(fd, sm.INPUT_EVENT_SIZE * 64)
                except BlockingIOError:
                    time.sleep(0.01)
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


def play(engine, profile_set, blob, speed=8.0):
    """Feed the recorded capture through the engine, at wall clock pace."""
    count = len(blob) // sm.FRAME_SIZE
    axes = [0] * 6
    last = time.monotonic()
    for index in range(count):
        event = sm.decode_frame(
            blob[index * sm.FRAME_SIZE : (index + 1) * sm.FRAME_SIZE]
        )
        if event.kind == "motion":
            axes = list(event.axes)
        elif event.kind == "press":
            engine.handle_button(event.button, True)
        delay = 0.01
        if event.kind == "motion" and 1 <= event.period <= 200:
            delay = event.period / 1000.0
        time.sleep(delay / speed)
        now = time.monotonic()
        engine.tick(axes, now - last, profile_set)
        last = now
    engine.release_all()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live uinput check")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the device alive for 30 s so you can poke at it",
    )
    parser.add_argument("--speed", type=float, default=8.0)
    parser.add_argument("--profile", default="default")
    args = parser.parse_args(argv)

    failures = []
    skipped = []

    print("== /dev/uinput")
    if not os.path.exists("/dev/uinput"):
        print("  %sFAIL%s /dev/uinput does not exist (modprobe uinput)" % (RED, RESET))
        return 1
    if not os.access("/dev/uinput", os.W_OK):
        print("  %sFAIL%s /dev/uinput is not writable by this user." % (RED, RESET))
        print("       Add the udev rule from the README, then log out and back in.")
        return 1
    print("  %sok%s writable" % (GREEN, RESET))

    print("\n== creating the device")
    device = sm.VirtualDevice()
    device.open()
    print(
        "  %sok%s created '%s'%s"
        % (
            GREEN,
            RESET,
            sm.DEVICE_NAME,
            " (legacy setup path)" if device.legacy_setup else "",
        )
    )

    node = find_event_node(sm.DEVICE_NAME)
    reader = None
    if node is None:
        skipped.append("the kernel never published an event node for the device")
        print("  %swarn%s no /dev/input/eventN appeared for it" % (YELLOW, RESET))
    else:
        print("  %sok%s the kernel gave it %s" % (GREEN, RESET, node))
        if os.access(node, os.R_OK):
            reader = EventReader(node)
            reader.start()
            time.sleep(0.3)
            if reader.error:
                skipped.append("could not read %s: %s" % (node, reader.error))
                reader = None
        else:
            skipped.append(
                "%s is not readable by this user, so nothing was read back" % node
            )
            print(
                "  %swarn%s %s is not readable (see the README's udev section)"
                % (YELLOW, RESET, node)
            )

    print("\n== playing the capture through the engine")
    profile_set = sm.ProfileSet(DEFAULT_PROFILES)
    profile = profile_set.by_name(args.profile)
    if profile is None:
        print("  %sFAIL%s no profile named '%s'" % (RED, RESET, args.profile))
        device.close()
        return 1
    engine = sm.GestureEngine(device, profile_set.settings)
    engine.set_profile(profile)
    with open(FIXTURE_BIN, "rb") as handle:
        blob = handle.read()
    started = time.monotonic()
    play(engine, profile_set, blob, speed=args.speed)
    print(
        "  %sok%s %d frames in %.1f s with profile '%s'"
        % (
            GREEN,
            RESET,
            len(blob) // sm.FRAME_SIZE,
            time.monotonic() - started,
            profile.name,
        )
    )

    if reader is not None:
        time.sleep(0.4)
        reader.running = False
        reader.join(timeout=2)
        events = list(reader.events)
        print("\n== what came back out of the kernel")
        print("  %d events" % len(events))
        counts = {}
        for etype, code, value in events:
            if etype == sm.EV_SYN:
                continue
            counts.setdefault((etype, code), []).append(value)
        for key in sorted(counts):
            values = counts[key]
            print(
                "  %-18s %4d events, sum %d" % (name_of(*key), len(values), sum(values))
            )

        def want(etype, code, why):
            if (etype, code) in counts:
                print("  %sok%s   %s" % (GREEN, RESET, why))
            else:
                print("  %sFAIL%s %s" % (RED, RESET, why))
                failures.append(why)

        print()
        want(sm.EV_KEY, sm.BTN_MIDDLE, "the middle button reached the kernel")
        want(sm.EV_REL, sm.REL_X, "horizontal pointer motion reached the kernel")
        want(sm.EV_REL, sm.REL_Y, "vertical pointer motion reached the kernel")
        want(
            sm.EV_KEY,
            sm.KEY_NAMES["KEY_HOME"],
            "the FIT button's key tap reached the kernel",
        )
        want(sm.EV_REL, sm.REL_WHEEL_HI_RES, "the zoom wheel reached the kernel")

        middle = counts.get((sm.EV_KEY, sm.BTN_MIDDLE), [])
        if middle:
            if middle.count(1) == middle.count(0):
                print(
                    "  %sok%s   every button press was matched by a release"
                    % (GREEN, RESET)
                )
            else:
                print(
                    "  %sFAIL%s %d presses but %d releases"
                    % (RED, RESET, middle.count(1), middle.count(0))
                )
                failures.append("unbalanced button presses")

    if args.keep:
        print("\n== keeping the device alive for 30 s (Ctrl-C to stop)")
        try:
            time.sleep(30)
        except KeyboardInterrupt:
            pass

    device.close()
    print("\n== device closed")

    print("\n== result")
    for note in skipped:
        print("  %sskipped%s %s" % (YELLOW, RESET, note))
    if failures:
        print("  %s%d check(s) failed%s" % (RED, len(failures), RESET))
        return 1
    if skipped:
        print("  %severything that could run passed%s" % (YELLOW, RESET))
        return 0
    print("  %sall live checks passed%s" % (GREEN, RESET))
    return 0


if __name__ == "__main__":
    sys.exit(main())
