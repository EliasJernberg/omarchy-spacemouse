"""Pointer arbitration: who gets grabbed, what gets through, and letting go.

The rule underneath all of this is that the mouse must never die. Most of the
file is about the ways it could, and the fact that none of them happen.
"""

import errno
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import FakeIO, sm  # noqa: E402


def candidate(
    name="Some Mouse",
    vendor="046d",
    product="c547",
    sysname="input9",
    properties=None,
    node="event13",
):
    """A PointerCandidate built by hand, with no sysfs involved."""
    item = sm.PointerCandidate.__new__(sm.PointerCandidate)
    item.node = node
    item.path = "/dev/input/%s" % node
    item.name = name
    item.sysname = sysname
    item.vendor = vendor
    item.product = product
    item.minor = 77
    item.properties = properties if properties is not None else {"ID_INPUT_MOUSE": "1"}
    item.rel_mask = (1 << sm.REL_X) | (1 << sm.REL_Y)
    item.key_mask = 1 << sm.BTN_LEFT
    return item


def event(etype, code, value):
    return (etype, code, value)


class SelectionTest(unittest.TestCase):
    """Which devices the proxy is allowed to take over."""

    def selector(self, **settings):
        return sm.PointerSelector(settings, own_sysname="input62")

    def test_a_plain_mouse_is_selected(self):
        self.assertIsNone(self.selector().rejection(candidate()))

    def test_our_own_device_is_never_grabbed(self):
        # The one that would be catastrophic: every event we emit would come
        # straight back in and be emitted again.
        mine = candidate(name=sm.DEVICE_NAME, sysname="input62")
        self.assertEqual(self.selector().rejection(mine), "our own virtual device")

    def test_another_instance_of_us_is_not_grabbed_either(self):
        twin = candidate(name=sm.DEVICE_NAME, sysname="input70")
        self.assertEqual(self.selector().rejection(twin), "carries our device's name")

    def test_the_puck_itself_is_left_to_spacenavd(self):
        puck = candidate(
            name="3Dconnexion SpaceMouse Pro",
            vendor="046d",
            product="c62b",
            properties={"ID_INPUT_MOUSE": "1", "ID_INPUT_3D_MOUSE": "1"},
        )
        self.assertEqual(self.selector().rejection(puck), "3d_mouse")

    def test_a_touchpad_is_refused(self):
        pad = candidate(
            name="DualSense Touchpad",
            properties={"ID_INPUT_MOUSE": "1", "ID_INPUT_TOUCHPAD": "1"},
        )
        self.assertEqual(self.selector().rejection(pad), "touchpad")

    def test_a_joystick_is_refused(self):
        stick = candidate(properties={"ID_INPUT_MOUSE": "1", "ID_INPUT_JOYSTICK": "1"})
        self.assertEqual(self.selector().rejection(stick), "joystick")

    def test_a_keyboard_is_not_a_pointer(self):
        keyboard = candidate(properties={"ID_INPUT_KEYBOARD": "1"})
        self.assertEqual(self.selector().rejection(keyboard), "not a mouse")

    def test_the_default_exclude_list_covers_the_known_awkward_ones(self):
        selector = sm.PointerSelector({}, own_sysname="input62")
        for name in ("Ducky Keyboard Mouse", "DualSense Wireless Controller Touchpad"):
            item = candidate(name=name)
            self.assertIsNotNone(selector.rejection(item), name)

    def test_excluded_by_vendor_and_product(self):
        selector = sm.PointerSelector(
            {"pointer_exclude_ids": ["046d:c547"]}, own_sysname="input62"
        )
        self.assertEqual(selector.rejection(candidate()), "excluded id 046d:c547")

    def test_excluded_by_name_fragment(self):
        selector = sm.PointerSelector(
            {"pointer_exclude_names": ["pro x"]}, own_sysname="input62"
        )
        self.assertIn(
            "excluded name", selector.rejection(candidate(name="Logitech PRO X"))
        )

    def test_include_overrides_the_exclusions_but_not_our_own_device(self):
        selector = sm.PointerSelector(
            {"pointer_include": "Test Pointer"}, own_sysname="input62"
        )
        forced = candidate(name="Omarchy SpaceMouse Test Pointer", sysname="input70")
        self.assertIsNone(selector.rejection(forced))
        ours = candidate(name="Omarchy SpaceMouse Test Pointer", sysname="input62")
        self.assertEqual(selector.rejection(ours), "our own virtual device")

    def test_without_udev_data_the_capabilities_decide(self):
        selector = self.selector()
        item = candidate(properties={})
        self.assertIsNone(selector.rejection(item))
        item.key_mask = 0  # no buttons: not a mouse
        self.assertEqual(selector.rejection(item), "not a mouse")

    def test_select_filters_a_whole_list(self):
        selector = self.selector()
        items = [
            candidate(name="Logitech PRO X", sysname="input9"),
            candidate(name=sm.DEVICE_NAME, sysname="input62"),
            candidate(name="Ducky Keyboard Mouse", sysname="input5"),
        ]
        self.assertEqual([c.name for c in selector.select(items)], ["Logitech PRO X"])


class FilterTest(unittest.TestCase):
    """What a grabbed mouse's events turn into on the way out."""

    def test_everything_passes_outside_a_gesture(self):
        batch = [
            event(sm.EV_KEY, sm.BTN_LEFT, 1),
            event(sm.EV_REL, sm.REL_X, 7),
            event(sm.EV_REL, sm.REL_Y, -3),
            event(sm.EV_REL, sm.REL_WHEEL, 1),
        ]
        self.assertEqual(sm.filter_forwarded(batch, False), batch)

    def test_pointer_motion_is_dropped_during_a_gesture(self):
        batch = [
            event(sm.EV_REL, sm.REL_X, 7),
            event(sm.EV_REL, sm.REL_Y, -3),
            event(sm.EV_REL, sm.REL_WHEEL, 1),
            event(sm.EV_KEY, sm.BTN_LEFT, 1),
        ]
        self.assertEqual(
            sm.filter_forwarded(batch, True),
            [event(sm.EV_REL, sm.REL_WHEEL, 1), event(sm.EV_KEY, sm.BTN_LEFT, 1)],
        )

    def test_a_motion_only_batch_disappears_entirely_during_a_gesture(self):
        batch = [event(sm.EV_REL, sm.REL_X, 4), event(sm.EV_REL, sm.REL_Y, 4)]
        self.assertIsNone(sm.filter_forwarded(batch, True))

    def test_the_wheel_still_works_during_a_gesture(self):
        batch = [
            event(sm.EV_REL, sm.REL_WHEEL_HI_RES, 120),
            event(sm.EV_REL, sm.REL_WHEEL, 1),
        ]
        self.assertEqual(sm.filter_forwarded(batch, True), batch)

    def test_undeclared_event_types_are_dropped(self):
        # Our device has no EV_ABS; the Logitech receiver sends ABS_VOLUME.
        batch = [event(0x03, 0x20, 1), event(sm.EV_REL, sm.REL_X, 2)]
        self.assertEqual(
            sm.filter_forwarded(batch, False), [event(sm.EV_REL, sm.REL_X, 2)]
        )

    def test_scancodes_pass_but_never_alone(self):
        with_button = [
            event(sm.EV_MSC, sm.MSC_SCAN, 0x90001),
            event(sm.EV_KEY, sm.BTN_LEFT, 1),
        ]
        self.assertEqual(sm.filter_forwarded(with_button, False), with_button)
        self.assertIsNone(
            sm.filter_forwarded([event(sm.EV_MSC, sm.MSC_SCAN, 0x90001)], False)
        )

    def test_media_keys_survive_the_trip(self):
        batch = [event(sm.EV_KEY, 164, 1)]  # KEY_PLAYPAUSE
        self.assertEqual(sm.filter_forwarded(batch, False), batch)


class FakeDaemon(object):
    """Just enough daemon for the proxy and the watchdog."""

    def __init__(self, device, alive=True, mode="proxied"):
        self.device = device
        self.pointer_mode = mode
        self.gesture_active = False
        self.profile_set = type("P", (), {"settings": {}})()
        self._alive = alive
        self.last_tick = 0.0

    def main_loop_is_alive(self, now=None):
        return self._alive

    def ticks_ago(self, now=None):
        return 0.0 if self._alive else 5.0


class ProxyPumpTest(unittest.TestCase):
    """Reading a grabbed device and writing it back out."""

    def setUp(self):
        self.io = FakeIO()
        self.device = sm.VirtualDevice(io=self.io, settle=0)
        self.device.open()
        self.io.writes = []
        self.daemon = FakeDaemon(self.device)
        self.proxy = sm.PointerProxy(
            self.daemon, __import__("threading").Event(), io=self.io
        )
        self.pointer = sm.GrabbedPointer(candidate(), 55, io=self.io)
        self.pointer.grab()
        self.proxy.pointers["/dev/input/event13"] = self.pointer

    def forwarded(self):
        out = []
        for etype, code, value in self.io.events():
            if etype != sm.EV_SYN:
                out.append((etype, code, value))
        return out

    def feed(self, events):
        self.pointer.pending = list(events)
        self.proxy.flush(self.pointer)

    def test_a_batch_is_forwarded_bit_for_bit(self):
        batch = [
            event(sm.EV_REL, sm.REL_X, 12),
            event(sm.EV_REL, sm.REL_Y, -5),
            event(sm.EV_KEY, sm.BTN_LEFT, 1),
        ]
        self.feed(batch)
        self.assertEqual(self.forwarded(), batch)
        self.assertEqual(len(self.io.writes), 1, "one batch, one write")

    def test_each_batch_ends_with_a_syn_report(self):
        self.feed([event(sm.EV_REL, sm.REL_X, 1)])
        self.assertEqual(self.io.events()[-1], (sm.EV_SYN, sm.SYN_REPORT, 0))

    def test_motion_is_dropped_while_the_puck_is_dragging(self):
        self.daemon.gesture_active = True
        self.feed([event(sm.EV_REL, sm.REL_X, 30), event(sm.EV_REL, sm.REL_Y, 30)])
        self.assertEqual(self.io.writes, [], "nothing should reach the device")
        self.assertEqual(self.pointer.dropped_rel, 2)

    def test_buttons_still_reach_the_device_while_dragging(self):
        self.daemon.gesture_active = True
        self.feed([event(sm.EV_REL, sm.REL_X, 30), event(sm.EV_KEY, sm.BTN_RIGHT, 1)])
        self.assertEqual(self.forwarded(), [event(sm.EV_KEY, sm.BTN_RIGHT, 1)])

    def test_passthrough_resumes_the_moment_the_gesture_ends(self):
        self.daemon.gesture_active = True
        self.feed([event(sm.EV_REL, sm.REL_X, 9)])
        self.assertEqual(self.io.writes, [])
        self.daemon.gesture_active = False
        self.feed([event(sm.EV_REL, sm.REL_X, 9)])
        self.assertEqual(self.forwarded(), [event(sm.EV_REL, sm.REL_X, 9)])

    def test_reading_splits_batches_on_syn_report(self):
        blob = b""
        for etype, code, value in (
            (sm.EV_REL, sm.REL_X, 3),
            (sm.EV_SYN, sm.SYN_REPORT, 0),
            (sm.EV_REL, sm.REL_X, 4),
            (sm.EV_SYN, sm.SYN_REPORT, 0),
        ):
            blob += struct.pack(sm.INPUT_EVENT_FMT, 0, 0, etype, code, value)
        read_calls = []

        def fake_read(fd, size):
            read_calls.append(fd)
            return blob

        real_read = os.read
        os.read = fake_read
        try:
            self.proxy.pump(55)
        finally:
            os.read = real_read
        self.assertEqual(len(self.io.writes), 2, "two batches, two writes")
        self.assertEqual(
            [(c, v) for t, c, v in self.io.events() if t == sm.EV_REL],
            [(sm.REL_X, 3), (sm.REL_X, 4)],
        )

    def test_nothing_is_forwarded_once_the_device_is_closed(self):
        self.device.close()
        self.io.writes = []
        self.feed([event(sm.EV_REL, sm.REL_X, 5)])
        self.assertEqual(self.io.writes, [])

    def test_an_ungrabbed_device_is_read_and_dropped(self):
        # Outside a gesture the kernel is still delivering these to the
        # compositor. Forwarding them as well would double every movement.
        self.pointer.ungrab()
        self.feed([event(sm.EV_REL, sm.REL_X, 40)])
        self.assertEqual(self.io.writes, [])


class GestureGrabTest(unittest.TestCase):
    """The grab is held for the length of a drag, and no longer."""

    def setUp(self):
        import threading

        self.io = FakeIO()
        self.device = sm.VirtualDevice(io=self.io, settle=0)
        self.device.open()
        self.daemon = FakeDaemon(self.device)
        self.daemon.profile_set.settings = {"pointer_grab": "gesture"}
        self.proxy = sm.PointerProxy(self.daemon, threading.Event(), io=self.io)
        self.pointer = sm.GrabbedPointer(candidate(), 55, io=self.io)
        self.proxy.pointers["/dev/input/event13"] = self.pointer

    def test_nothing_is_grabbed_until_a_gesture_starts(self):
        self.assertFalse(self.pointer.grabbed)
        self.assertEqual(self.io.grabs, [])

    def test_a_gesture_grabs_and_the_end_of_it_releases(self):
        self.proxy.set_gesture(True)
        self.assertTrue(self.pointer.grabbed)
        self.assertEqual(self.io.grabs, [(55, 1)])
        self.proxy.set_gesture(False)
        self.assertFalse(self.pointer.grabbed)
        self.assertEqual(self.io.grabs, [(55, 1), (55, 0)])

    def test_repeating_the_same_state_changes_nothing(self):
        self.proxy.set_gesture(True)
        self.proxy.set_gesture(True)
        self.assertEqual(self.io.grabs, [(55, 1)])

    def test_the_always_policy_ignores_gesture_transitions(self):
        self.daemon.profile_set.settings["pointer_grab"] = "always"
        self.proxy.set_gesture(True)
        self.assertEqual(self.io.grabs, [], "the scan owns the grab in this mode")

    def test_release_all_clears_the_gesture_flag(self):
        self.proxy.set_gesture(True)
        self.proxy.release_all("test")
        self.assertFalse(self.proxy.gesture_grab)
        self.assertIn((55, 0), self.io.grabs)


class GrabTest(unittest.TestCase):
    def setUp(self):
        self.io = FakeIO()
        self.pointer = sm.GrabbedPointer(candidate(), 55, io=self.io)

    def test_grab_and_ungrab_use_eviocgrab(self):
        self.pointer.grab()
        self.assertEqual(self.io.grabs, [(55, 1)])
        self.assertTrue(self.pointer.grabbed)
        self.pointer.ungrab()
        self.assertEqual(self.io.grabs, [(55, 1), (55, 0)])
        self.assertFalse(self.pointer.grabbed)

    def test_ungrab_twice_is_harmless(self):
        self.pointer.grab()
        self.pointer.ungrab()
        self.pointer.ungrab()
        self.assertEqual(self.io.grabs.count((55, 0)), 1)

    def test_ungrab_survives_a_failing_ioctl(self):
        # The device was unplugged: the grab is already gone, and saying so
        # must not raise on the way out.
        def boom(fd, request, arg=0):
            raise OSError(errno.ENODEV, "No such device")

        self.pointer.grab()
        self.io.ioctl = boom
        self.pointer.ungrab()
        self.assertFalse(self.pointer.grabbed)

    def test_close_releases_first(self):
        self.pointer.grab()
        self.pointer.close()
        self.assertIn((55, 0), self.io.grabs)
        self.assertEqual(self.io.closed, [55])
        self.assertIsNone(self.pointer.fd)


class WatchdogTest(unittest.TestCase):
    """The part that has to work when everything else has stopped."""

    def setUp(self):
        import threading

        self.io = FakeIO()
        self.device = sm.VirtualDevice(io=self.io, settle=0)
        self.device.open()
        self.daemon = FakeDaemon(self.device)
        self.proxy = sm.PointerProxy(self.daemon, threading.Event(), io=self.io)
        self.pointer = sm.GrabbedPointer(candidate(), 55, io=self.io)
        self.pointer.grab()
        self.proxy.pointers["/dev/input/event13"] = self.pointer
        self.proxy.state = "proxied"
        self.watchdog = sm.PointerWatchdog(self.daemon, self.proxy, threading.Event())

    def test_a_ticking_main_loop_keeps_the_grab(self):
        self.assertFalse(self.watchdog.check())
        self.assertTrue(self.pointer.grabbed)

    def test_a_stalled_main_loop_releases_every_grab(self):
        self.daemon._alive = False
        self.assertTrue(self.watchdog.check())
        self.assertFalse(self.pointer.grabbed)
        self.assertIn((55, 0), self.io.grabs)
        self.assertEqual(self.proxy.pointers, {})
        self.assertEqual(self.proxy.state, "shared")

    def test_it_does_not_trip_twice_over_the_same_stall(self):
        self.daemon._alive = False
        self.assertTrue(self.watchdog.check())
        self.assertFalse(self.watchdog.check())
        self.assertEqual(self.watchdog.trips, 1)

    def test_release_all_closes_the_descriptors(self):
        self.proxy.release_all("test")
        self.assertEqual(self.io.closed, [55])

    def test_the_proxy_wants_nothing_without_a_device(self):
        self.daemon.device = None
        self.assertFalse(self.proxy.wanted())

    def test_the_proxy_wants_nothing_in_shared_mode(self):
        self.daemon.pointer_mode = "shared"
        self.assertFalse(self.proxy.wanted())

    def test_the_proxy_wants_nothing_while_the_loop_is_stalled(self):
        self.daemon._alive = False
        self.assertFalse(self.proxy.wanted())


class DescribeTest(unittest.TestCase):
    def setUp(self):
        import threading

        self.io = FakeIO()
        self.device = sm.VirtualDevice(io=self.io, settle=0)
        self.device.open()
        self.daemon = FakeDaemon(self.device)
        self.proxy = sm.PointerProxy(self.daemon, threading.Event(), io=self.io)

    def test_shared_by_default(self):
        self.assertEqual(self.proxy.describe(), "shared")

    def test_no_permission_is_spelled_out(self):
        self.proxy.state = "shared"
        self.proxy.detail = "no permission"
        self.assertEqual(self.proxy.describe(), "shared (no permission)")

    def test_proxied_names_the_devices(self):
        self.proxy.state = "proxied"
        self.proxy.pointers["/dev/input/event13"] = sm.GrabbedPointer(
            candidate(name="Logitech PRO X"), 55, io=self.io
        )
        self.assertEqual(self.proxy.describe(), "proxied (Logitech PRO X)")


if __name__ == "__main__":
    unittest.main()
