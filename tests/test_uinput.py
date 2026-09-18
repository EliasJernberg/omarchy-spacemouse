"""The uinput layer: ioctl numbers, device setup, and the event bytes.

None of this touches the kernel. A fake io object records every ioctl and
write, which is what lets the numbers be checked against the values the kernel
headers define, on a machine where /dev/uinput may not even be readable.
"""

import errno
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import FakeIO, sm  # noqa: E402


class IoctlNumberTest(unittest.TestCase):
    """The values below are what linux/uinput.h expands to on any Linux."""

    def test_known_ioctl_numbers(self):
        self.assertEqual(sm.UI_DEV_CREATE, 0x5501)
        self.assertEqual(sm.UI_DEV_DESTROY, 0x5502)
        self.assertEqual(sm.UI_DEV_SETUP, 0x405C5503)
        self.assertEqual(sm.UI_SET_EVBIT, 0x40045564)
        self.assertEqual(sm.UI_SET_KEYBIT, 0x40045565)
        self.assertEqual(sm.UI_SET_RELBIT, 0x40045566)

    def test_struct_sizes(self):
        self.assertEqual(sm.UINPUT_SETUP_SIZE, 92)
        self.assertEqual(sm.UINPUT_USER_DEV_SIZE, 1116)
        self.assertEqual(sm.INPUT_EVENT_SIZE, 24)  # 64 bit kernel layout

    def test_event_codes(self):
        self.assertEqual((sm.EV_SYN, sm.EV_KEY, sm.EV_REL), (0x00, 0x01, 0x02))
        self.assertEqual((sm.REL_X, sm.REL_Y), (0x00, 0x01))
        self.assertEqual((sm.REL_WHEEL, sm.REL_HWHEEL), (0x08, 0x06))
        self.assertEqual((sm.REL_WHEEL_HI_RES, sm.REL_HWHEEL_HI_RES), (0x0B, 0x0C))
        self.assertEqual(
            (sm.BTN_LEFT, sm.BTN_RIGHT, sm.BTN_MIDDLE), (0x110, 0x111, 0x112)
        )


class DeviceSetupTest(unittest.TestCase):
    def setUp(self):
        self.io = FakeIO()
        self.device = sm.VirtualDevice(io=self.io, settle=0)
        self.device.open()
        self.addCleanup(self.device.close)

    def test_it_opens_the_node_and_creates_the_device(self):
        self.assertEqual(self.io.opened, ["/dev/uinput"])
        requests = [request for request, _ in self.io.ioctls]
        self.assertIn(sm.UI_DEV_CREATE, requests)
        # Every capability has to be declared before the device is created,
        # and the sysname can only be read afterwards.
        create = requests.index(sm.UI_DEV_CREATE)
        for bit in (sm.UI_SET_EVBIT, sm.UI_SET_KEYBIT, sm.UI_SET_RELBIT, sm.UI_SET_MSCBIT):
            self.assertLess(max(i for i, r in enumerate(requests) if r == bit), create)
        self.assertGreater(requests.index(sm.UI_GET_SYSNAME), create)

    def test_it_declares_key_rel_msc_and_syn_capabilities(self):
        # EV_MSC is there for the proxied physical mice: they send MSC_SCAN
        # alongside every button, and an undeclared type would be dropped.
        self.assertEqual(
            sorted(self.io.requests(sm.UI_SET_EVBIT)),
            sorted([sm.EV_KEY, sm.EV_REL, sm.EV_SYN, sm.EV_MSC]),
        )
        self.assertEqual(self.io.requests(sm.UI_SET_MSCBIT), [sm.MSC_SCAN])

    def test_it_declares_the_media_keys_a_proxied_mouse_may_carry(self):
        keybits = set(self.io.requests(sm.UI_SET_KEYBIT))
        for code in (115, 164, 255):          # VOLUMEUP, PLAYPAUSE, top of range
            self.assertIn(code, keybits, "keycode %d missing" % code)

    def test_it_stops_before_the_joystick_button_block(self):
        # BTN_JOYSTICK (0x120) and BTN_GAMEPAD (0x130) would have udev tag
        # this device as a joystick, which libinput handles very differently.
        keybits = set(self.io.requests(sm.UI_SET_KEYBIT))
        for code in (0x120, 0x130, 0x13F):
            self.assertNotIn(code, keybits)

    def test_it_learns_its_own_sysname(self):
        # Set by the fake io, which answers UI_GET_SYSNAME with "input99".
        self.assertEqual(self.device.sysname, "input99")

    def test_it_enables_the_mouse_buttons(self):
        keybits = self.io.requests(sm.UI_SET_KEYBIT)
        for code in (sm.BTN_LEFT, sm.BTN_RIGHT, sm.BTN_MIDDLE):
            self.assertIn(code, keybits)

    def test_it_enables_the_whole_keyboard_block(self):
        # udev only stamps ID_INPUT_KEYBOARD when keycodes 1..31 are present,
        # and without that tag the compositor gives the device no keyboard
        # capability, which would make shift-modified gestures do nothing.
        keybits = set(self.io.requests(sm.UI_SET_KEYBIT))
        for code in range(1, 32):
            self.assertIn(code, keybits, "keycode %d missing" % code)

    def test_it_enables_the_relative_axes(self):
        relbits = self.io.requests(sm.UI_SET_RELBIT)
        for code in (
            sm.REL_X,
            sm.REL_Y,
            sm.REL_WHEEL,
            sm.REL_HWHEEL,
            sm.REL_WHEEL_HI_RES,
            sm.REL_HWHEEL_HI_RES,
        ):
            self.assertIn(code, relbits)

    def test_the_device_is_named_for_the_status_bar_to_find(self):
        setups = self.io.requests(sm.UI_DEV_SETUP)
        self.assertEqual(len(setups), 1)
        blob = setups[0]
        self.assertEqual(len(blob), sm.UINPUT_SETUP_SIZE)
        bustype, vendor, product, version, name, ff_max = struct.unpack(
            "@HHHH80sI", blob
        )
        self.assertEqual(bustype, sm.BUS_VIRTUAL)
        self.assertEqual(name.split(b"\x00", 1)[0].decode(), "Omarchy SpaceMouse")
        self.assertEqual(ff_max, 0)
        self.assertTrue(vendor and product and version)

    def test_close_destroys_the_device(self):
        self.device.close()
        self.assertIn(sm.UI_DEV_DESTROY, [request for request, _ in self.io.ioctls])
        self.assertTrue(self.io.closed)
        self.assertFalse(self.device.is_open)


class LegacySetupTest(unittest.TestCase):
    def test_it_falls_back_to_uinput_user_dev(self):
        io = FakeIO(fail_setup=True)
        device = sm.VirtualDevice(io=io, settle=0)
        device.open()
        self.addCleanup(device.close)
        self.assertTrue(device.legacy_setup)
        self.assertEqual(len(io.writes), 1)
        self.assertEqual(len(io.writes[0]), sm.UINPUT_USER_DEV_SIZE)
        self.assertTrue(io.writes[0].startswith(b"Omarchy SpaceMouse\x00"))
        self.assertIn(sm.UI_DEV_CREATE, [request for request, _ in io.ioctls])


class PermissionTest(unittest.TestCase):
    def test_permission_denied_is_reported_as_denied(self):
        io = FakeIO(fail_open=OSError(errno.EACCES, "Permission denied"))
        device = sm.VirtualDevice(io=io, settle=0)
        with self.assertRaises(sm.UinputUnavailable) as caught:
            device.open()
        self.assertEqual(caught.exception.reason, "denied")
        self.assertFalse(device.is_open)

    def test_missing_node_is_reported_as_error(self):
        io = FakeIO(fail_open=OSError(errno.ENOENT, "No such file or directory"))
        device = sm.VirtualDevice(io=io, settle=0)
        with self.assertRaises(sm.UinputUnavailable) as caught:
            device.open()
        self.assertEqual(caught.exception.reason, "error")


class EmitTest(unittest.TestCase):
    def setUp(self):
        self.io = FakeIO()
        self.device = sm.VirtualDevice(io=self.io, settle=0)
        self.device.open()
        self.io.writes = []  # drop anything the setup wrote
        self.addCleanup(self.device.close)

    def test_one_batch_is_one_write_ending_in_syn_report(self):
        self.device.move(3, -4)
        self.device.syn()
        self.assertEqual(len(self.io.writes), 1)
        events = self.io.events()
        self.assertEqual(
            events,
            [
                (sm.EV_REL, sm.REL_X, 3),
                (sm.EV_REL, sm.REL_Y, -4),
                (sm.EV_SYN, sm.SYN_REPORT, 0),
            ],
        )

    def test_zero_movement_is_not_written(self):
        self.device.move(0, 0)
        self.device.syn()
        self.assertEqual(self.io.writes, [])

    def test_key_press_and_release(self):
        self.device.key_down(sm.BTN_MIDDLE)
        self.device.syn()
        self.device.key_up(sm.BTN_MIDDLE)
        self.device.syn()
        self.assertEqual(
            [(t, c, v) for t, c, v in self.io.events() if t == sm.EV_KEY],
            [(sm.EV_KEY, sm.BTN_MIDDLE, 1), (sm.EV_KEY, sm.BTN_MIDDLE, 0)],
        )

    def test_a_key_is_never_pressed_twice(self):
        self.device.key_down(sm.BTN_MIDDLE)
        self.device.key_down(sm.BTN_MIDDLE)
        self.device.syn()
        presses = [e for e in self.io.events() if e[0] == sm.EV_KEY]
        self.assertEqual(len(presses), 1)
        self.assertEqual(self.device.pressed, [sm.BTN_MIDDLE])

    def test_release_all_lets_go_in_reverse_order(self):
        shift = sm.KEY_NAMES["KEY_LEFTSHIFT"]
        self.device.key_down(shift)
        self.device.key_down(sm.BTN_MIDDLE)
        self.device.syn()
        self.io.writes = []
        self.device.release_all()
        self.assertEqual(
            [(c, v) for t, c, v in self.io.events() if t == sm.EV_KEY],
            [(sm.BTN_MIDDLE, 0), (shift, 0)],
        )
        self.assertEqual(self.device.pressed, [])

    def test_release_all_on_a_clean_device_writes_nothing(self):
        self.device.release_all()
        self.assertEqual(self.io.writes, [])

    def test_close_releases_what_is_held(self):
        self.device.key_down(sm.BTN_MIDDLE)
        self.device.syn()
        self.io.writes = []
        self.device.close()
        self.assertEqual(
            [(c, v) for t, c, v in self.io.events() if t == sm.EV_KEY],
            [(sm.BTN_MIDDLE, 0)],
        )

    def test_wheel_emits_high_resolution_before_the_detent(self):
        self.device.wheel(detents=1, hi_res=120)
        self.device.syn()
        events = [(c, v) for t, c, v in self.io.events() if t == sm.EV_REL]
        self.assertEqual(events, [(sm.REL_WHEEL_HI_RES, 120), (sm.REL_WHEEL, 1)])

    def test_horizontal_wheel_uses_the_horizontal_codes(self):
        self.device.wheel(detents=-1, hi_res=-120, horizontal=True)
        self.device.syn()
        events = [(c, v) for t, c, v in self.io.events() if t == sm.EV_REL]
        self.assertEqual(events, [(sm.REL_HWHEEL_HI_RES, -120), (sm.REL_HWHEEL, -1)])

    def test_tap_presses_then_releases(self):
        ctrl = sm.KEY_NAMES["KEY_LEFTCTRL"]
        home = sm.KEY_NAMES["KEY_HOME"]
        self.device.tap([ctrl, home])
        self.assertEqual(
            [(c, v) for t, c, v in self.io.events() if t == sm.EV_KEY],
            [(ctrl, 1), (home, 1), (home, 0), (ctrl, 0)],
        )
        self.assertEqual(self.device.pressed, [])

    def test_events_written_while_closed_are_dropped_not_crashing(self):
        self.device.close()
        self.device.move(5, 5)
        self.device.syn()
        self.assertEqual(self.io.writes, [])

    def test_input_event_layout(self):
        self.device.move(7, 0)
        self.device.syn()
        blob = self.io.writes[0]
        self.assertEqual(len(blob) % sm.INPUT_EVENT_SIZE, 0)
        sec, usec, etype, code, value = struct.unpack(
            sm.INPUT_EVENT_FMT, blob[: sm.INPUT_EVENT_SIZE]
        )
        # The kernel timestamps the event itself, so zeros go out on the wire.
        self.assertEqual((sec, usec), (0, 0))
        self.assertEqual((etype, code, value), (sm.EV_REL, sm.REL_X, 7))


class TraceDeviceTest(unittest.TestCase):
    """The --dry-run sink has to behave exactly like the real one."""

    def test_it_tracks_held_keys_the_same_way(self):
        device = sm.TraceDevice(stream=None)
        device.key_down(sm.BTN_MIDDLE)
        device.syn()
        self.assertEqual(device.pressed, [sm.BTN_MIDDLE])
        device.release_all()
        self.assertEqual(device.pressed, [])
        self.assertEqual(device.records, ["syn 1:274:1", "syn 1:274:0"])

    def test_it_never_opens_anything(self):
        device = sm.TraceDevice(stream=None)
        device.open()
        self.assertTrue(device.is_open)
        self.assertEqual(device.records, ["open Omarchy SpaceMouse"])


if __name__ == "__main__":
    unittest.main()
