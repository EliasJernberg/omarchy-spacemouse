#!/usr/bin/env python3
"""Live pointer arbitration, without touching the mouse you are holding.

A second uinput device stands in for a physical mouse, and the proxy is told
to take that one and nothing else. Then the three things that matter are
checked against the real kernel and the real compositor:

  1. outside a gesture the stand-in moves the cursor exactly as it should,
     and nothing is forwarded through the virtual device,
  2. during a gesture its motion is swallowed, so the cursor does not move at
     all while the puck owns the drag,
  3. its buttons still come through, so the mouse is not dead while held.

Run it with the daemon running or not; it uses its own devices either way.
The cursor is put back where it was at the end, whatever happens.

    python3 tests/live_pointer.py
"""

import os
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import sm  # noqa: E402

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

STAND_IN = "Omarchy SpaceMouse Test Pointer"


class FakeDaemon(object):
    """The three things PointerProxy actually asks the daemon for."""

    def __init__(self, device, settings):
        self.device = device
        self.pointer_mode = "proxied"
        self.gesture_active = False
        self.profile_set = type("P", (), {"settings": settings})()

    def main_loop_is_alive(self, now=None):
        return True

    def ticks_ago(self, now=None):
        return 0.0


class Reader(threading.Thread):
    """Reads our own virtual device's node, so the test can see exactly what
    the daemon put on the wire."""

    daemon = True

    def __init__(self, path):
        threading.Thread.__init__(self, name="reader")
        self.path = path
        self.events = []
        self.running = True

    def take(self):
        events, self.events = self.events, []
        return events

    def run(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return
        try:
            while self.running:
                try:
                    blob = os.read(fd, sm.INPUT_EVENT_SIZE * 64)
                except BlockingIOError:
                    time.sleep(0.01)
                    continue
                except OSError:
                    return
                for index in range(len(blob) // sm.INPUT_EVENT_SIZE):
                    chunk = blob[
                        index * sm.INPUT_EVENT_SIZE : (index + 1) * sm.INPUT_EVENT_SIZE
                    ]
                    _, _, etype, code, value = struct.unpack(sm.INPUT_EVENT_FMT, chunk)
                    self.events.append((etype, code, value))
        finally:
            os.close(fd)


def write_events(fd, events):
    blob = b""
    for etype, code, value in events:
        blob += struct.pack(sm.INPUT_EVENT_FMT, 0, 0, etype, code, value)
    blob += struct.pack(sm.INPUT_EVENT_FMT, 0, 0, sm.EV_SYN, sm.SYN_REPORT, 0)
    os.write(fd, blob)


def main():
    failures = []

    print("== permissions")
    if not os.access("/dev/uinput", os.W_OK):
        print("  %sFAIL%s /dev/uinput is not writable" % (RED, RESET))
        return 1
    print("  %sok%s /dev/uinput is writable" % (GREEN, RESET))

    settings = {
        # Take the stand-in and nothing else: the real mouse in the user's
        # hand is left completely alone.
        "pointer_include": "Test Pointer",
        "pointer_exclude_names": ["Logitech", "DualSense", "Ducky", "SpaceMouse Pro"],
        "pointer_grab": "gesture",
    }

    device = sm.VirtualDevice()
    stand_in = sm.VirtualDevice(name=STAND_IN, vendor=0x6666, product=0x0001)
    proxy = None
    reader = None
    start = sm.hypr_cursor_position()
    try:
        device.open()
        stand_in.open()
        print("\n== devices")
        print(
            "  %sok%s ours: %s, stand-in: %s"
            % (GREEN, RESET, device.sysname, stand_in.sysname)
        )

        daemon = FakeDaemon(device, settings)
        proxy = sm.PointerProxy(daemon, threading.Event(), log=lambda m: None)
        time.sleep(1.0)  # let udev publish the stand-in
        proxy.scan()
        names = proxy.device_names
        print("  watching: %s" % (names or "nothing"))
        if STAND_IN not in names:
            print("  %sFAIL%s the proxy did not pick up the stand-in" % (RED, RESET))
            return 1
        if any("Logitech" in name for name in names):
            print(
                "  %sFAIL%s it grabbed the real mouse, which this test must not"
                % (RED, RESET)
            )
            return 1
        print(
            "  %sok%s the stand-in is watched and the real mouse is not"
            % (GREEN, RESET)
        )

        def pump(seconds=0.35):
            """Let the proxy read whatever is waiting, the way its loop would."""
            deadline = time.time() + seconds
            while time.time() < deadline:
                for pointer in list(proxy.pointers.values()):
                    if pointer.fd is not None:
                        proxy.pump(pointer.fd)
                time.sleep(0.02)

        def check(condition, message):
            if condition:
                print("  %sok%s   %s" % (GREEN, RESET, message))
            else:
                print("  %sFAIL%s %s" % (RED, RESET, message))
                failures.append(message)

        fd = stand_in.fd

        # The cursor is shared with whoever is at the keyboard, so measuring it
        # would measure their hand as well as this test. What the daemon does
        # with the stand-in's events is measured instead, at the two places it
        # can be seen exactly: the proxy's own counters, and the events that
        # come back out of our own virtual device's node.
        node = None
        base = "/sys/devices/virtual/input/%s" % device.sysname
        for entry in sorted(os.listdir(base)):
            if entry.startswith("event"):
                node = "/dev/input/%s" % entry
                break
        if node and os.access(node, os.R_OK):
            reader = Reader(node)
            reader.start()
            time.sleep(0.2)
            reader.take()
        else:
            print("  %swarn%s cannot read %s, using the counters alone"
                  % (YELLOW, RESET, node))

        def out_events():
            return [e for e in (reader.take() if reader else []) if e[0] != sm.EV_SYN]

        print("\n== outside a gesture")
        write_events(fd, [(sm.EV_REL, sm.REL_X, 40), (sm.EV_REL, sm.REL_Y, 20)])
        time.sleep(0.3)
        pump()
        check(
            sum(p.forwarded for p in proxy.pointers.values()) == 0,
            "nothing is forwarded: the kernel is delivering it directly",
        )
        check(out_events() == [], "our virtual device stayed silent")

        print("\n== during a gesture")
        daemon.gesture_active = True
        proxy.set_gesture(True)
        check(proxy.holding, "the mouse is grabbed for the length of the drag")
        write_events(fd, [(sm.EV_REL, sm.REL_X, 60), (sm.EV_REL, sm.REL_Y, 60)])
        time.sleep(0.3)
        pump()
        dropped = sum(p.dropped_rel for p in proxy.pointers.values())
        check(dropped == 2, "its motion was swallowed (%d events dropped)" % dropped)
        check(out_events() == [], "so none of it reached the pointer")

        # A button, though, still has to work. BTN_TASK is chosen because
        # essentially nothing binds it.
        write_events(fd, [(sm.EV_KEY, sm.BTN_TASK, 1)])
        write_events(fd, [(sm.EV_KEY, sm.BTN_TASK, 0)])
        time.sleep(0.3)
        pump()
        buttons = out_events()
        check(
            buttons == [(sm.EV_KEY, sm.BTN_TASK, 1), (sm.EV_KEY, sm.BTN_TASK, 0)],
            "its buttons still came through: %s" % (buttons,),
        )
        check(
            sum(p.dropped_rel for p in proxy.pointers.values()) == 2,
            "and nothing was dropped that was not motion",
        )

        print("\n== after the gesture")
        daemon.gesture_active = False
        proxy.set_gesture(False)
        check(not proxy.holding, "the mouse was handed straight back")
        forwarded_before = sum(p.forwarded for p in proxy.pointers.values())
        write_events(fd, [(sm.EV_REL, sm.REL_X, -40), (sm.EV_REL, sm.REL_Y, -20)])
        time.sleep(0.3)
        pump()
        check(
            sum(p.forwarded for p in proxy.pointers.values()) == forwarded_before,
            "back to the kernel delivering it directly",
        )
        check(out_events() == [], "and our device is silent again")


        print("\n== the watchdog")
        proxy.set_gesture(True)
        watchdog = sm.PointerWatchdog(daemon, proxy, threading.Event())
        daemon.main_loop_is_alive = lambda now=None: False
        daemon.ticks_ago = lambda now=None: 5.0
        tripped = watchdog.check()
        check(tripped, "a stalled main loop released the grab")
        check(not proxy.pointers, "and let go of the device entirely")
    finally:
        if reader is not None:
            reader.running = False
        if proxy is not None:
            proxy.release_all("test over")
        stand_in.close()
        device.close()
        if start:
            time.sleep(0.2)
            sm.hypr_warp_cursor(*start)

    print("\n== result")
    if failures:
        print("  %s%d check(s) failed%s" % (RED, len(failures), RESET))
        return 1
    print("  %sthe pointer arbitration works on this machine%s" % (GREEN, RESET))
    return 0


if __name__ == "__main__":
    sys.exit(main())
