#!/usr/bin/env python3
"""omarchy-spacemouse daemon: per-application 3Dconnexion profiles for Hyprland.

What it does, in one breath: read the puck from spacenavd, watch which window
Hyprland has focused, pick a profile for that window class, and turn puck
motion into whatever that application understands. Applications with their own
spacenavd support (KiCad, FreeCAD, Blender, Fusion through Bifrost) get a
"native" profile, which means the daemon stays quiet and lets them talk to
spacenavd themselves. Everything else gets synthetic mouse or keyboard input
from a virtual uinput device, which is how 3DxWare makes a SpaceMouse work in
applications that never heard of one.

Wire format, spacenavd (verified against spacenavd 1.3.1, src/proto_unix.c
send_uevent and src/proto.h): every event is exactly 32 bytes, eight native
little endian int32. data[0] is the event type:

    UEV_MOTION  = 0  ->  [0, x, y, z, rx, ry, rz, period_ms]
    UEV_PRESS   = 1  ->  [1, button, 1, 0, 0, 0, 0, 0]
    UEV_RELEASE = 2  ->  [2, button, 0, 0, 0, 0, 0, 0]
    UEV_DEV     = 3, UEV_CFG = 4, UEV_RAWAXIS = 5, UEV_RAWBUTTON = 6

Protocol version 0 is the default for a fresh connection and sets
EVMASK_MOTION | EVMASK_BUTTON, so there is no handshake and none is performed.
spacenavd serves several clients at once, so running next to KiCad is fine.

Axis convention on this hardware (SpaceMouse Pro, measured):
    right        = x+      lift          = y+      forward (away) = z+
    front edge down = rx-  clockwise from above = ry-      FIT button = 5
Full deflection lands near 350, rest noise stays under 20, cross-talk onto
other axes can reach 50.

Everything here is Python standard library on purpose: no build step, no
virtualenv, and the file can be dropped on any Arch box with python3.
"""

import argparse
import errno
import fcntl
import json
import math
import os
import queue
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

APP_NAME = "omarchy-spacemouse"
VERSION = "1.0.0"
DEVICE_NAME = "Omarchy SpaceMouse"
# How long to wait for Hyprland to name the focused window before falling back
# to the profile list's own fallback.
FOCUS_GRACE_SECONDS = 3.0

# ---------------------------------------------------------------------------
# spacenavd protocol
# ---------------------------------------------------------------------------

FRAME_SIZE = 32
FRAME_FMT = "<8i"

UEV_MOTION = 0
UEV_PRESS = 1
UEV_RELEASE = 2
UEV_DEV = 3
UEV_CFG = 4
UEV_RAWAXIS = 5
UEV_RAWBUTTON = 6

AXES = ("x", "y", "z", "rx", "ry", "rz")
DEFAULT_SPNAV_SOCKET = "/run/spnav.sock"


class SpnavEvent(object):
    """One decoded spacenavd event.

    kind is "motion", "press", "release" or "other". Motion carries axes (a
    six tuple in AXES order) and period (milliseconds the frame covers);
    press/release carry button.
    """

    __slots__ = ("kind", "axes", "button", "period", "raw")

    def __init__(self, kind, axes=None, button=None, period=0, raw=None):
        self.kind = kind
        self.axes = axes
        self.button = button
        self.period = period
        self.raw = raw

    def __repr__(self):
        if self.kind == "motion":
            return "SpnavEvent(motion, axes=%s, period=%d)" % (self.axes, self.period)
        if self.kind in ("press", "release"):
            return "SpnavEvent(%s, button=%d)" % (self.kind, self.button)
        return "SpnavEvent(%s, raw=%s)" % (self.kind, self.raw)


def decode_frame(frame):
    """Decode one 32 byte spacenavd frame into an SpnavEvent."""
    if len(frame) != FRAME_SIZE:
        raise ValueError(
            "spacenavd frame must be %d bytes, got %d" % (FRAME_SIZE, len(frame))
        )
    values = struct.unpack(FRAME_FMT, frame)
    etype = values[0]
    if etype == UEV_MOTION:
        return SpnavEvent(
            "motion", axes=tuple(values[1:7]), period=values[7], raw=values
        )
    if etype == UEV_PRESS:
        return SpnavEvent("press", button=values[1], raw=values)
    if etype == UEV_RELEASE:
        return SpnavEvent("release", button=values[1], raw=values)
    return SpnavEvent("other", raw=values)


def iter_frames(blob):
    """Yield SpnavEvents for every whole frame in a recorded capture."""
    for index in range(len(blob) // FRAME_SIZE):
        yield decode_frame(blob[index * FRAME_SIZE : (index + 1) * FRAME_SIZE])


# ---------------------------------------------------------------------------
# uinput: event codes and ioctl numbers
# ---------------------------------------------------------------------------

EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02

SYN_REPORT = 0

REL_X = 0x00
REL_Y = 0x01
REL_HWHEEL = 0x06
REL_WHEEL = 0x08
REL_WHEEL_HI_RES = 0x0B
REL_HWHEEL_HI_RES = 0x0C

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
BTN_SIDE = 0x113
BTN_EXTRA = 0x114
BTN_FORWARD = 0x115
BTN_BACK = 0x116
BTN_TASK = 0x117

BUS_VIRTUAL = 0x06

# ioctl numbers, built the way the kernel builds them:
#   _IOC(dir, type, nr, size) = (dir << 30) | (size << 16) | (type << 8) | nr
_IOC_WRITE = 1
UINPUT_IOCTL_BASE = ord("U")


def _iow(nr, size):
    return (_IOC_WRITE << 30) | (size << 16) | (UINPUT_IOCTL_BASE << 8) | nr


def _io(nr):
    return (UINPUT_IOCTL_BASE << 8) | nr


UI_DEV_CREATE = _io(1)  # 0x5501
UI_DEV_DESTROY = _io(2)  # 0x5502
# struct uinput_setup: struct input_id id (8) + char name[80] + __u32 ff_effects_max
UINPUT_SETUP_SIZE = 8 + 80 + 4  # 92
UI_DEV_SETUP = _iow(3, UINPUT_SETUP_SIZE)  # 0x405c5503
UI_SET_EVBIT = _iow(100, 4)  # 0x40045564
UI_SET_KEYBIT = _iow(101, 4)  # 0x40045565
UI_SET_RELBIT = _iow(102, 4)  # 0x40045566

UINPUT_MAX_NAME_SIZE = 80
ABS_CNT = 64
# struct uinput_user_dev: name[80] + input_id(8) + ff_effects_max(4) + 4 * s32[64]
UINPUT_USER_DEV_SIZE = UINPUT_MAX_NAME_SIZE + 8 + 4 + 4 * ABS_CNT * 4  # 1116

# struct input_event on a 64 bit kernel: two __kernel_long_t for the timeval,
# then type/code as __u16 and value as __s32.
INPUT_EVENT_FMT = "@llHHi"
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FMT)

# The standard keyboard block, 1..127. The whole block is enabled on the
# virtual device for two reasons: udev's input_id builtin only stamps
# ID_INPUT_KEYBOARD when keycodes 1..31 are present (and without that tag the
# compositor never gives the device a keyboard seat capability, so modifiers
# such as shift would go nowhere), and having every key available means a
# profile can bind any key without recreating the device.
KEY_NAMES = {
    "KEY_ESC": 1,
    "KEY_1": 2,
    "KEY_2": 3,
    "KEY_3": 4,
    "KEY_4": 5,
    "KEY_5": 6,
    "KEY_6": 7,
    "KEY_7": 8,
    "KEY_8": 9,
    "KEY_9": 10,
    "KEY_0": 11,
    "KEY_MINUS": 12,
    "KEY_EQUAL": 13,
    "KEY_BACKSPACE": 14,
    "KEY_TAB": 15,
    "KEY_Q": 16,
    "KEY_W": 17,
    "KEY_E": 18,
    "KEY_R": 19,
    "KEY_T": 20,
    "KEY_Y": 21,
    "KEY_U": 22,
    "KEY_I": 23,
    "KEY_O": 24,
    "KEY_P": 25,
    "KEY_LEFTBRACE": 26,
    "KEY_RIGHTBRACE": 27,
    "KEY_ENTER": 28,
    "KEY_LEFTCTRL": 29,
    "KEY_A": 30,
    "KEY_S": 31,
    "KEY_D": 32,
    "KEY_F": 33,
    "KEY_G": 34,
    "KEY_H": 35,
    "KEY_J": 36,
    "KEY_K": 37,
    "KEY_L": 38,
    "KEY_SEMICOLON": 39,
    "KEY_APOSTROPHE": 40,
    "KEY_GRAVE": 41,
    "KEY_LEFTSHIFT": 42,
    "KEY_BACKSLASH": 43,
    "KEY_Z": 44,
    "KEY_X": 45,
    "KEY_C": 46,
    "KEY_V": 47,
    "KEY_B": 48,
    "KEY_N": 49,
    "KEY_M": 50,
    "KEY_COMMA": 51,
    "KEY_DOT": 52,
    "KEY_SLASH": 53,
    "KEY_RIGHTSHIFT": 54,
    "KEY_KPASTERISK": 55,
    "KEY_LEFTALT": 56,
    "KEY_SPACE": 57,
    "KEY_CAPSLOCK": 58,
    "KEY_F1": 59,
    "KEY_F2": 60,
    "KEY_F3": 61,
    "KEY_F4": 62,
    "KEY_F5": 63,
    "KEY_F6": 64,
    "KEY_F7": 65,
    "KEY_F8": 66,
    "KEY_F9": 67,
    "KEY_F10": 68,
    "KEY_NUMLOCK": 69,
    "KEY_SCROLLLOCK": 70,
    "KEY_KP7": 71,
    "KEY_KP8": 72,
    "KEY_KP9": 73,
    "KEY_KPMINUS": 74,
    "KEY_KP4": 75,
    "KEY_KP5": 76,
    "KEY_KP6": 77,
    "KEY_KPPLUS": 78,
    "KEY_KP1": 79,
    "KEY_KP2": 80,
    "KEY_KP3": 81,
    "KEY_KP0": 82,
    "KEY_KPDOT": 83,
    "KEY_102ND": 86,
    "KEY_F11": 87,
    "KEY_F12": 88,
    "KEY_KPENTER": 96,
    "KEY_RIGHTCTRL": 97,
    "KEY_KPSLASH": 98,
    "KEY_SYSRQ": 99,
    "KEY_RIGHTALT": 100,
    "KEY_HOME": 102,
    "KEY_UP": 103,
    "KEY_PAGEUP": 104,
    "KEY_LEFT": 105,
    "KEY_RIGHT": 106,
    "KEY_END": 107,
    "KEY_DOWN": 108,
    "KEY_PAGEDOWN": 109,
    "KEY_INSERT": 110,
    "KEY_DELETE": 111,
    "KEY_MUTE": 113,
    "KEY_VOLUMEDOWN": 114,
    "KEY_VOLUMEUP": 115,
    "KEY_KPEQUAL": 117,
    "KEY_PAUSE": 119,
    "KEY_LEFTMETA": 125,
    "KEY_RIGHTMETA": 126,
    "KEY_COMPOSE": 127,
}

BUTTON_NAMES = {
    "left": BTN_LEFT,
    "right": BTN_RIGHT,
    "middle": BTN_MIDDLE,
    "side": BTN_SIDE,
    "extra": BTN_EXTRA,
    "forward": BTN_FORWARD,
    "back": BTN_BACK,
    "task": BTN_TASK,
}

MODIFIER_ALIASES = {
    "shift": "KEY_LEFTSHIFT",
    "ctrl": "KEY_LEFTCTRL",
    "control": "KEY_LEFTCTRL",
    "alt": "KEY_LEFTALT",
    "super": "KEY_LEFTMETA",
    "meta": "KEY_LEFTMETA",
    "win": "KEY_LEFTMETA",
}

MOUSE_BUTTON_CODES = frozenset(BUTTON_NAMES.values())


def resolve_token(token):
    """Turn one token ("middle", "shift", "KEY_HOME", "home", "f5") into a code."""
    name = str(token).strip()
    if not name:
        raise ValueError("empty key token")
    lowered = name.lower()
    if lowered in BUTTON_NAMES:
        return BUTTON_NAMES[lowered]
    if lowered in MODIFIER_ALIASES:
        return KEY_NAMES[MODIFIER_ALIASES[lowered]]
    upper = name.upper()
    if upper in KEY_NAMES:
        return KEY_NAMES[upper]
    if ("KEY_" + upper) in KEY_NAMES:
        return KEY_NAMES["KEY_" + upper]
    if upper.startswith("BTN_"):
        for code_name, code in (
            ("BTN_LEFT", BTN_LEFT),
            ("BTN_RIGHT", BTN_RIGHT),
            ("BTN_MIDDLE", BTN_MIDDLE),
            ("BTN_SIDE", BTN_SIDE),
            ("BTN_EXTRA", BTN_EXTRA),
            ("BTN_FORWARD", BTN_FORWARD),
            ("BTN_BACK", BTN_BACK),
            ("BTN_TASK", BTN_TASK),
        ):
            if upper == code_name:
                return code
    raise ValueError("unknown key or button '%s'" % token)


def parse_combo(spec):
    """Parse "shift+middle" or ["shift", "middle"] into a list of codes.

    Order is preserved: modifiers are meant to be listed before the key or
    button they modify, because that is the order they are pressed in.
    """
    if spec is None:
        return []
    if isinstance(spec, (list, tuple)):
        tokens = []
        for item in spec:
            tokens.extend(str(item).split("+"))
    else:
        tokens = str(spec).split("+")
    return [resolve_token(token) for token in tokens if str(token).strip()]


# ---------------------------------------------------------------------------
# uinput device
# ---------------------------------------------------------------------------


class UinputIO(object):
    """The real syscalls. Swapped for a fake in the tests."""

    def open(self, path):
        return os.open(path, os.O_WRONLY | os.O_NONBLOCK)

    def ioctl(self, fd, request, arg=0):
        return fcntl.ioctl(fd, request, arg)

    def write(self, fd, data):
        return os.write(fd, data)

    def close(self, fd):
        os.close(fd)


class UinputUnavailable(Exception):
    """Raised when /dev/uinput cannot be opened or set up."""

    def __init__(self, message, reason="error"):
        Exception.__init__(self, message)
        self.reason = reason


class EventSink(object):
    """Queue events, hold keys, let go of them.

    Shared by the real uinput device and the trace sink used by --dry-run.
    Subclasses provide syn(), which is what actually delivers a batch.
    """

    def __init__(self):
        self.pressed = []  # press order, so release runs in reverse
        self._pending = []

    def queue(self, etype, code, value):
        self._pending.append((etype, code, value))

    def syn(self):
        raise NotImplementedError

    def move(self, dx, dy):
        if dx:
            self.queue(EV_REL, REL_X, int(dx))
        if dy:
            self.queue(EV_REL, REL_Y, int(dy))

    def wheel(self, detents=0, hi_res=0, horizontal=False):
        if hi_res:
            self.queue(
                EV_REL,
                REL_HWHEEL_HI_RES if horizontal else REL_WHEEL_HI_RES,
                int(hi_res),
            )
        if detents:
            self.queue(EV_REL, REL_HWHEEL if horizontal else REL_WHEEL, int(detents))

    def key_down(self, code):
        if code in self.pressed:
            return
        self.pressed.append(code)
        self.queue(EV_KEY, code, 1)

    def key_up(self, code):
        if code not in self.pressed:
            return
        self.pressed.remove(code)
        self.queue(EV_KEY, code, 0)

    def tap(self, codes):
        for code in codes:
            self.key_down(code)
        self.syn()
        for code in reversed(codes):
            self.key_up(code)
        self.syn()

    def release_all(self):
        """Let go of everything, right now.

        Called on every profile change, on disable and on shutdown. A stuck
        middle button would make the desktop unusable, so this is the one path
        that must never be skipped.
        """
        if not self.pressed:
            return
        for code in reversed(list(self.pressed)):
            self.queue(EV_KEY, code, 0)
        self.pressed = []
        self.syn()


class VirtualDevice(EventSink):
    """A virtual mouse plus keyboard built straight on /dev/uinput.

    One device carries both capabilities on purpose. A gesture like
    "shift + middle drag" has to press a modifier and a mouse button that the
    compositor sees as belonging together, and a single device is also what a
    real 3Dconnexion puck looks like to the kernel.
    """

    def __init__(self, name=DEVICE_NAME, path="/dev/uinput", io=None, settle=0.25):
        EventSink.__init__(self)
        self.name = name
        self.path = path
        self.io = io or UinputIO()
        self.fd = None
        self.legacy_setup = False
        # How long to wait for udev to publish the new device before writing
        # to it. Zero in the tests, where nothing downstream is listening.
        self.settle = settle

    # -- lifecycle ---------------------------------------------------------

    @property
    def is_open(self):
        return self.fd is not None

    def open(self):
        if self.fd is not None:
            return
        try:
            fd = self.io.open(self.path)
        except OSError as exc:
            reason = "denied" if exc.errno in (errno.EACCES, errno.EPERM) else "error"
            raise UinputUnavailable("cannot open %s: %s" % (self.path, exc), reason) from exc
        try:
            self._configure(fd)
        except OSError as exc:
            try:
                self.io.close(fd)
            except OSError:
                pass
            raise UinputUnavailable("cannot set up %s: %s" % (self.path, exc), "error") from exc
        self.fd = fd
        self.pressed = []
        self._pending = []
        # The kernel needs a moment to publish the new device through udev
        # before anything downstream (libinput, the compositor) picks it up.
        # Events written before that are delivered to nobody.
        if self.settle:
            time.sleep(self.settle)

    def _configure(self, fd):
        self.io.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        self.io.ioctl(fd, UI_SET_EVBIT, EV_REL)
        self.io.ioctl(fd, UI_SET_EVBIT, EV_SYN)
        for code in sorted(set(KEY_NAMES.values())):
            self.io.ioctl(fd, UI_SET_KEYBIT, code)
        for code in sorted(MOUSE_BUTTON_CODES):
            self.io.ioctl(fd, UI_SET_KEYBIT, code)
        for code in (
            REL_X,
            REL_Y,
            REL_WHEEL,
            REL_HWHEEL,
            REL_WHEEL_HI_RES,
            REL_HWHEEL_HI_RES,
        ):
            self.io.ioctl(fd, UI_SET_RELBIT, code)

        name = self.name.encode("utf-8")[: UINPUT_MAX_NAME_SIZE - 1]
        setup = struct.pack(
            "@HHHH80sI",
            BUS_VIRTUAL,
            0x5350,
            0x4D53,
            0x0001,
            name,
            0,
        )
        try:
            self.io.ioctl(fd, UI_DEV_SETUP, setup)
        except OSError:
            # Kernels before 4.5 have no UI_DEV_SETUP; write the old
            # uinput_user_dev struct to the fd instead.
            self.legacy_setup = True
            user_dev = struct.pack(
                "@80sHHHHI",
                name,
                BUS_VIRTUAL,
                0x5350,
                0x4D53,
                0x0001,
                0,
            )
            user_dev += b"\x00" * (UINPUT_USER_DEV_SIZE - len(user_dev))
            self.io.write(fd, user_dev)
        self.io.ioctl(fd, UI_DEV_CREATE)

    def close(self):
        if self.fd is None:
            return
        try:
            self.release_all()
        except OSError:
            pass
        fd, self.fd = self.fd, None
        try:
            self.io.ioctl(fd, UI_DEV_DESTROY)
        except OSError:
            pass
        try:
            self.io.close(fd)
        except OSError:
            pass

    # -- emitting ----------------------------------------------------------

    def queue(self, etype, code, value):
        self._pending.append((etype, code, value))

    def syn(self):
        """Flush every queued event plus one SYN_REPORT in a single write."""
        if not self._pending:
            return
        if self.fd is None:
            self._pending = []
            return
        blob = b""
        for etype, code, value in self._pending:
            blob += struct.pack(INPUT_EVENT_FMT, 0, 0, etype, code, int(value))
        blob += struct.pack(INPUT_EVENT_FMT, 0, 0, EV_SYN, SYN_REPORT, 0)
        self._pending = []
        self.io.write(self.fd, blob)


class TraceDevice(EventSink):
    """Stand-in for VirtualDevice that writes down what it would have done.

    Used by --dry-run, which is how the pipeline is exercised on a machine
    where /dev/uinput is not writable yet, and by the tests.
    """

    def __init__(self, stream=None, name=DEVICE_NAME):
        EventSink.__init__(self)
        self.stream = stream
        self.name = name
        self.records = []
        self.legacy_setup = False
        self.fd = -1

    @property
    def is_open(self):
        return True

    def open(self):
        self._write("open %s" % self.name)

    def close(self):
        self.release_all()
        self._write("close")

    def _write(self, line):
        self.records.append(line)
        if self.stream is not None:
            self.stream.write(line + "\n")
            self.stream.flush()

    def syn(self):
        if not self._pending:
            return
        parts = []
        for etype, code, value in self._pending:
            parts.append("%d:%d:%d" % (etype, code, value))
        self._pending = []
        self._write("syn " + " ".join(parts))


# ---------------------------------------------------------------------------
# configuration and profiles
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "tick_hz": 60.0,
    "deadzone": 30.0,
    "full_scale": 350.0,
    "curve": 1.5,
    "sensitivity": 1.0,
    "axis_gain": {"x": 1.0, "y": 1.0, "z": 1.0, "rx": 1.0, "ry": 1.0, "rz": 1.0},
    "axis_invert": {
        "x": False,
        "y": False,
        "z": False,
        "rx": False,
        "ry": False,
        "rz": False,
    },
    "activate_threshold": 0.12,
    "release_threshold": 0.05,
    "idle_release_ms": 150.0,
    "dominance_ratio": 1.35,
    "pointer_speed": 900.0,
    "wheel_speed": 3.0,
    "wheel_hi_res": True,
    "spnav_socket": DEFAULT_SPNAV_SOCKET,
}


class Gesture(object):
    """One axis group in a mouse profile: what it reads, what it emits."""

    def __init__(self, name, spec):
        self.name = name
        self.enabled = bool(spec.get("enabled", True))
        self.mode = str(spec.get("mode", "drag"))  # "drag" or "wheel"
        self.speed = float(spec.get("speed", 1.0))
        self.hold = parse_combo(spec.get("hold"))
        self.axes = {}
        for axis, mapping in (spec.get("axes") or {}).items():
            if axis not in AXES:
                raise ValueError("gesture '%s' uses unknown axis '%s'" % (name, axis))
            if isinstance(mapping, (int, float)):
                mapping = {"to": "dx", "gain": float(mapping)}
            target = str(mapping.get("to", "dx"))
            if target not in ("dx", "dy", "wheel", "hwheel"):
                raise ValueError(
                    "gesture '%s' maps %s to unknown target '%s'" % (name, axis, target)
                )
            self.axes[axis] = (target, float(mapping.get("gain", 1.0)))

    def magnitude(self, norm):
        """How hard this group is being driven, 0..1-ish."""
        total = 0.0
        for axis in self.axes:
            value = norm.get(axis, 0.0)
            total += value * value
        return math.sqrt(total)


class KeyBinding(object):
    """One axis direction bound to a key in a "keys" profile."""

    def __init__(self, spec):
        self.axis = str(spec.get("axis", "y"))
        if self.axis not in AXES:
            raise ValueError("key binding uses unknown axis '%s'" % self.axis)
        direction = str(spec.get("dir", "+"))
        self.sign = -1.0 if direction.strip() in ("-", "neg", "negative") else 1.0
        self.codes = parse_combo(spec.get("key"))
        self.threshold = float(spec.get("threshold", 0.3))
        self.repeat_hz = float(spec.get("repeat_hz", 10.0))
        self.hold = bool(spec.get("hold", False))


class ButtonAction(object):
    """What a puck button does in a given profile."""

    def __init__(self, spec):
        if isinstance(spec, str):
            spec = {"action": "key", "key": spec}
        self.action = str(spec.get("action", "none"))
        self.command = spec.get("command")
        self.codes = parse_combo(spec.get("key")) if spec.get("key") else []
        self.label = spec.get("label") or ""


class Profile(object):
    def __init__(self, spec):
        self.name = str(spec.get("name") or "unnamed")
        self.type = str(spec.get("type") or "mouse")
        if self.type not in ("native", "off", "mouse", "keys"):
            raise ValueError(
                "profile '%s' has unknown type '%s'" % (self.name, self.type)
            )
        pattern = spec.get("match")
        self.match_source = pattern
        self.regex = None
        if pattern not in (None, "", "*"):
            self.regex = re.compile(str(pattern), re.IGNORECASE)
        self.description = str(spec.get("description") or "")
        self.icon = str(spec.get("icon") or "")
        self.fit_key = parse_combo(spec.get("fit_key")) if spec.get("fit_key") else []
        self.gestures = []
        for name, gspec in (spec.get("gestures") or {}).items():
            gesture = Gesture(name, gspec or {})
            if gesture.enabled:
                self.gestures.append(gesture)
        self.bindings = [KeyBinding(item) for item in (spec.get("bindings") or [])]
        self.buttons = {}
        for number, bspec in (spec.get("buttons") or {}).items():
            self.buttons[int(number)] = ButtonAction(bspec)

    @property
    def is_fallback(self):
        return self.regex is None

    def matches(self, window_class):
        if self.regex is None:
            return True
        return self.regex.search(window_class or "") is not None

    def as_dict(self):
        return {
            "name": self.name,
            "type": self.type,
            "match": self.match_source,
            "description": self.description,
            "gestures": [g.name for g in self.gestures],
        }


OFF_PROFILE = Profile(
    {
        "name": "off",
        "type": "off",
        "match": None,
        "description": "Daemon disabled, nothing is emitted",
    }
)


def deep_merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class ProfileSet(object):
    """Settings plus the ordered profile list, reloaded when the file changes."""

    def __init__(self, path, defaults_path=None, log=None):
        self.path = path
        self.defaults_path = defaults_path
        self.log = log or (lambda message: None)
        self.settings = dict(DEFAULT_SETTINGS)
        self.profiles = []
        self.mtime = 0.0
        self.error = ""
        self.load()

    def _read(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def load(self):
        data = None
        source = None
        for candidate in (self.path, self.defaults_path):
            if candidate and os.path.exists(candidate):
                try:
                    data = self._read(candidate)
                    source = candidate
                    break
                except Exception as exc:  # noqa: BLE001
                    self.error = "%s: %s" % (candidate, exc)
                    self.log("config error in %s: %s" % (candidate, exc))
        if data is None:
            self.log("no usable profile file, falling back to built-in defaults")
            data = {"profiles": [{"name": "default", "type": "mouse", "match": None}]}
            source = None
        try:
            settings = deep_merge(DEFAULT_SETTINGS, data.get("settings") or {})
            profiles = [Profile(spec) for spec in (data.get("profiles") or [])]
        except Exception as exc:  # noqa: BLE001
            self.error = "%s: %s" % (source or "<defaults>", exc)
            self.log(
                "refusing to load broken profiles (%s), keeping the previous set" % exc
            )
            if self.profiles:
                return False
            settings = dict(DEFAULT_SETTINGS)
            profiles = [Profile({"name": "default", "type": "mouse", "match": None})]
        if not profiles:
            profiles = [Profile({"name": "default", "type": "mouse", "match": None})]
        if not any(profile.is_fallback for profile in profiles):
            profiles.append(
                Profile({"name": "default", "type": "mouse", "match": None})
            )
        self.settings = settings
        self.profiles = profiles
        self.error = ""
        if source and os.path.exists(source):
            try:
                self.mtime = os.path.getmtime(source)
            except OSError:
                self.mtime = 0.0
        self.log(
            "loaded %d profiles from %s"
            % (len(profiles), source or "built-in defaults")
        )
        return True

    def reload_if_changed(self):
        if not self.path or not os.path.exists(self.path):
            return False
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if mtime == self.mtime:
            return False
        self.log("profiles.json changed on disk, reloading")
        return self.load()

    def select(self, window_class):
        for profile in self.profiles:
            if profile.regex is not None and profile.matches(window_class):
                return profile
        for profile in self.profiles:
            if profile.is_fallback:
                return profile
        return OFF_PROFILE

    def by_name(self, name):
        for profile in self.profiles:
            if profile.name == name:
                return profile
        return None

    def normalize(self, raw):
        """Raw counts to a signed 0..1 per axis, deadzone and curve applied."""
        return normalize_axes(raw, self.settings)


def normalize_axes(raw, settings):
    """Raw spacenavd counts to a signed 0..1 per axis.

    Below the deadzone an axis reads exactly zero, which is what keeps the
    resting noise (up to 20 counts on this puck) and the cross-talk onto
    neighbouring axes (up to 50) from starting a gesture on their own. The
    response curve is applied after the deadzone, so a small deflection stays
    small and the useful range is not spent on the first millimetre.
    """
    deadzone = float(settings.get("deadzone", 30.0))
    full_scale = float(settings.get("full_scale", 350.0))
    curve = float(settings.get("curve", 1.5))
    sensitivity = float(settings.get("sensitivity", 1.0))
    gains = settings.get("axis_gain") or {}
    inverts = settings.get("axis_invert") or {}
    span = max(1.0, full_scale - deadzone)
    out = {}
    for index, axis in enumerate(AXES):
        value = float(raw[index])
        magnitude = abs(value)
        if magnitude <= deadzone:
            out[axis] = 0.0
            continue
        scaled = min(1.0, (magnitude - deadzone) / span)
        if curve != 1.0:
            scaled = scaled**curve
        scaled *= float(gains.get(axis, 1.0)) * sensitivity
        if value < 0:
            scaled = -scaled
        if inverts.get(axis, False):
            scaled = -scaled
        out[axis] = scaled
    return out


# ---------------------------------------------------------------------------
# gesture engine
# ---------------------------------------------------------------------------


class GestureEngine(object):
    """Turns normalized axis values into held buttons and pointer motion.

    One gesture group is active at a time. The group with the largest
    magnitude wins, which is what keeps orbit and pan from ever holding their
    buttons at the same time. An active group keeps its buttons down until the
    puck has been back in the deadzone for idle_release_ms, so a brief pause
    mid-drag does not chop the gesture in two.
    """

    def __init__(self, device, settings=None, log=None, clock=time.monotonic):
        self.device = device
        self.settings = settings or dict(DEFAULT_SETTINGS)
        self.log = log or (lambda message: None)
        self.clock = clock
        self.profile = OFF_PROFILE
        self.active = None  # active Gesture
        self.active_since = 0.0
        self.last_above = 0.0
        self._frac_x = 0.0
        self._frac_y = 0.0
        self._hires = {"wheel": 0.0, "hwheel": 0.0}
        self._detent = {"wheel": 0.0, "hwheel": 0.0}
        self._key_next = {}
        self._key_held = {}
        self._children = []  # exec actions, kept so they can be reaped

    # -- profile -----------------------------------------------------------

    def set_profile(self, profile):
        if profile is self.profile:
            return False
        self.release_all()
        self.profile = profile
        self._frac_x = 0.0
        self._frac_y = 0.0
        self._hires = {"wheel": 0.0, "hwheel": 0.0}
        self._detent = {"wheel": 0.0, "hwheel": 0.0}
        return True

    def release_all(self):
        """Drop the active gesture and every key the device is holding."""
        self.active = None
        self._key_next = {}
        self._key_held = {}
        try:
            self.device.release_all()
        except OSError as exc:
            self.log("could not release keys: %s" % exc)

    @property
    def active_name(self):
        return self.active.name if self.active else ""

    # -- gesture selection -------------------------------------------------

    def _activate(self, gesture, now):
        self.active = gesture
        self.active_since = now
        self.last_above = now
        for code in gesture.hold:
            self.device.key_down(code)
        if gesture.hold:
            self.device.syn()

    def _deactivate(self):
        if self.active is None:
            return
        for code in reversed(self.active.hold):
            self.device.key_up(code)
        self.device.syn()
        self.active = None

    def tick(self, raw_axes, dt, profile_set=None):
        if self.profile.type in ("native", "off"):
            if self.active or self.device.pressed:
                self.release_all()
            return
        norm = (
            profile_set.normalize(raw_axes)
            if profile_set
            else normalize_axes(raw_axes, self.settings)
        )
        if self.profile.type == "keys":
            self._tick_keys(norm, dt)
            return
        self._tick_mouse(norm, dt)

    def _tick_mouse(self, norm, dt):
        now = self.clock()
        settings = self.settings
        activate = float(settings.get("activate_threshold", 0.12))
        release = float(settings.get("release_threshold", 0.05))
        idle = float(settings.get("idle_release_ms", 150.0)) / 1000.0
        ratio = float(settings.get("dominance_ratio", 1.35))

        magnitudes = {}
        best = None
        best_magnitude = 0.0
        for gesture in self.profile.gestures:
            magnitude = gesture.magnitude(norm)
            magnitudes[gesture.name] = magnitude
            if magnitude > best_magnitude:
                best = gesture
                best_magnitude = magnitude

        if self.active is not None:
            current = magnitudes.get(self.active.name, 0.0)
            if current >= release:
                self.last_above = now
            elif now - self.last_above >= idle:
                self._deactivate()
            if (
                self.active is not None
                and best is not None
                and best is not self.active
                and best_magnitude >= activate
                and best_magnitude > current * ratio
            ):
                self._deactivate()
                self._activate(best, now)
        if self.active is None and best is not None and best_magnitude >= activate:
            self._activate(best, now)

        if self.active is None:
            return
        self._emit(self.active, norm, dt)

    def _emit(self, gesture, norm, dt):
        settings = self.settings
        pointer_speed = float(settings.get("pointer_speed", 900.0))
        wheel_speed = float(settings.get("wheel_speed", 3.0))
        hi_res_enabled = bool(settings.get("wheel_hi_res", True))
        dx = 0.0
        dy = 0.0
        wheel = {"wheel": 0.0, "hwheel": 0.0}
        for axis, (target, gain) in gesture.axes.items():
            value = norm.get(axis, 0.0) * gain
            if value == 0.0:
                continue
            if target == "dx":
                dx += value * pointer_speed * gesture.speed * dt
            elif target == "dy":
                dy += value * pointer_speed * gesture.speed * dt
            else:
                wheel[target] += value * wheel_speed * gesture.speed * dt

        moved = False
        if dx or dy:
            self._frac_x += dx
            self._frac_y += dy
            step_x = int(self._frac_x)
            step_y = int(self._frac_y)
            self._frac_x -= step_x
            self._frac_y -= step_y
            if step_x or step_y:
                self.device.move(step_x, step_y)
                moved = True

        for target in ("wheel", "hwheel"):
            detents = wheel[target]
            if not detents:
                continue
            horizontal = target == "hwheel"
            self._hires[target] += detents * 120.0
            units = int(self._hires[target])
            self._hires[target] -= units
            clicks = 0
            if units:
                self._detent[target] += units
                while self._detent[target] >= 120.0:
                    self._detent[target] -= 120.0
                    clicks += 1
                while self._detent[target] <= -120.0:
                    self._detent[target] += 120.0
                    clicks -= 1
            if units and hi_res_enabled:
                self.device.wheel(detents=clicks, hi_res=units, horizontal=horizontal)
                moved = True
            elif clicks:
                self.device.wheel(detents=clicks, horizontal=horizontal)
                moved = True
        if moved:
            self.device.syn()

    # -- keys profile ------------------------------------------------------

    def _tick_keys(self, norm, dt):
        now = self.clock()
        for index, binding in enumerate(self.profile.bindings):
            value = norm.get(binding.axis, 0.0) * binding.sign
            active = value >= binding.threshold
            if binding.hold:
                held = self._key_held.get(index, False)
                if active and not held:
                    for code in binding.codes:
                        self.device.key_down(code)
                    self.device.syn()
                    self._key_held[index] = True
                elif not active and held:
                    for code in reversed(binding.codes):
                        self.device.key_up(code)
                    self.device.syn()
                    self._key_held[index] = False
                continue
            if not active:
                self._key_next.pop(index, None)
                continue
            due = self._key_next.get(index)
            if due is None or now >= due:
                self.device.tap(binding.codes)
                period = 1.0 / max(0.5, binding.repeat_hz)
                self._key_next[index] = now + period

    # -- puck buttons ------------------------------------------------------

    def handle_button(self, number, pressed):
        if not pressed:
            return None
        if self.profile.type in ("native", "off"):
            return None
        action = self.profile.buttons.get(int(number))
        if action is None:
            return None
        if action.action == "key" and action.codes:
            self.device.tap(action.codes)
            return "key"
        if action.action == "fit":
            if self.profile.fit_key:
                self.device.tap(self.profile.fit_key)
                return "fit"
            self.log(
                "button %d asked for fit, but profile '%s' has no fit_key"
                % (number, self.profile.name)
            )
            return None
        if action.action == "exec" and action.command:
            # Reap whatever finished since last time: the children are ours
            # even though they run in their own session, and an unreaped one
            # sits around as a zombie for the life of the daemon.
            self._children = [child for child in self._children if child.poll() is None]
            try:
                self._children.append(
                    subprocess.Popen(
                        action.command,
                        shell=True,
                        start_new_session=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.log("button %d exec failed: %s" % (number, exc))
                return None
            return "exec"
        return None


# ---------------------------------------------------------------------------
# Hyprland focus
# ---------------------------------------------------------------------------


def hypr_instance():
    """Find the running Hyprland instance without shelling out to hyprctl."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    root = os.path.join(runtime, "hypr")
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if signature and os.path.exists(os.path.join(root, signature, ".socket2.sock")):
        return os.path.join(root, signature)
    if not os.path.isdir(root):
        return None
    candidates = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.exists(os.path.join(path, ".socket2.sock")):
            try:
                candidates.append((os.path.getmtime(path), path))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def hypr_request(command, timeout=1.0):
    """Ask Hyprland something over .socket.sock (the hyprctl transport)."""
    instance = hypr_instance()
    if not instance:
        return None
    path = os.path.join(instance, ".socket.sock")
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(command.encode("utf-8"))
        chunks = []
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
        sock.close()
        return b"".join(chunks).decode("utf-8", "replace")
    except OSError:
        return None


def parse_activewindow(payload):
    """activewindow>>class,title - the title may itself contain commas."""
    if "," not in payload:
        return payload, ""
    window_class, title = payload.split(",", 1)
    return window_class, title


class FocusWatcher(threading.Thread):
    """Follows Hyprland's focused window over .socket2.sock, with reconnect."""

    daemon = True

    def __init__(self, on_change, stop_event, log=None):
        threading.Thread.__init__(self, name="focus-watcher")
        self.on_change = on_change
        self.stop_event = stop_event
        self.log = log or (lambda message: None)
        self.connected = False

    def poll_current(self):
        payload = hypr_request("j/activewindow")
        if not payload:
            return
        try:
            data = json.loads(payload)
        except ValueError:
            return
        if isinstance(data, dict):
            self.on_change(data.get("class") or "", data.get("title") or "")

    def run(self):
        while not self.stop_event.is_set():
            instance = hypr_instance()
            if not instance:
                self.connected = False
                self.stop_event.wait(3.0)
                continue
            path = os.path.join(instance, ".socket2.sock")
            sock = None
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect(path)
                sock.settimeout(None)
                self.connected = True
                self.log("following Hyprland focus on %s" % path)
                self.poll_current()
                buffer = b""
                while not self.stop_event.is_set():
                    ready, _, _ = select.select([sock], [], [], 1.0)
                    if not ready:
                        continue
                    chunk = sock.recv(8192)
                    if not chunk:
                        raise OSError("Hyprland closed the event socket")
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        self._handle_line(line.decode("utf-8", "replace"))
            except OSError as exc:
                if not self.stop_event.is_set():
                    self.log("Hyprland event socket lost (%s), retrying in 2 s" % exc)
            finally:
                self.connected = False
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if not self.stop_event.is_set():
                self.stop_event.wait(2.0)

    def _handle_line(self, line):
        if ">>" not in line:
            return
        event, payload = line.split(">>", 1)
        if event == "activewindow":
            window_class, title = parse_activewindow(payload)
            self.on_change(window_class, title)
        elif event in ("closewindow", "closelayer"):
            # Hyprland sends activewindow>>, right after when focus lands
            # nowhere, so nothing to do here beyond noticing.
            pass


# ---------------------------------------------------------------------------
# spacenavd reader
# ---------------------------------------------------------------------------


class SpnavReader(threading.Thread):
    daemon = True

    def __init__(self, daemon_ref, stop_event, log=None):
        threading.Thread.__init__(self, name="spnav-reader")
        self.daemon_ref = daemon_ref
        self.stop_event = stop_event
        self.log = log or (lambda message: None)
        self.connected = False

    def run(self):
        while not self.stop_event.is_set():
            path = self.daemon_ref.profile_set.settings.get(
                "spnav_socket", DEFAULT_SPNAV_SOCKET
            )
            sock = None
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                sock.connect(path)
                sock.settimeout(1.0)
                self.connected = True
                self.log("connected to spacenavd at %s" % path)
                buffer = b""
                while not self.stop_event.is_set():
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError as exc:
                        if exc.errno == errno.EINTR:
                            continue
                        raise
                    if not chunk:
                        raise OSError("spacenavd closed the connection")
                    buffer += chunk
                    while len(buffer) >= FRAME_SIZE:
                        self.daemon_ref.handle_event(decode_frame(buffer[:FRAME_SIZE]))
                        buffer = buffer[FRAME_SIZE:]
            except OSError as exc:
                if not self.stop_event.is_set():
                    self.log("spacenavd link lost (%s), retrying in 2 s" % exc)
            finally:
                self.connected = False
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                self.daemon_ref.center()
            if not self.stop_event.is_set():
                self.stop_event.wait(2.0)


class ReplayReader(threading.Thread):
    """Feeds a recorded capture through the same pipeline as the hardware."""

    daemon = True

    def __init__(self, daemon_ref, stop_event, path, speed=1.0, loop=False, log=None):
        threading.Thread.__init__(self, name="replay-reader")
        self.daemon_ref = daemon_ref
        self.stop_event = stop_event
        self.path = path
        self.speed = speed if speed > 0 else 1.0
        self.loop = loop
        self.log = log or (lambda message: None)
        self.finished = threading.Event()

    def run(self):
        with open(self.path, "rb") as handle:
            blob = handle.read()
        count = len(blob) // FRAME_SIZE
        self.log("replay: %s, %d frames" % (self.path, count))
        while not self.stop_event.is_set():
            for index in range(count):
                if self.stop_event.is_set():
                    break
                frame = blob[index * FRAME_SIZE : (index + 1) * FRAME_SIZE]
                event = decode_frame(frame)
                self.daemon_ref.handle_event(event)
                delay = 0.01
                if event.kind == "motion" and 1 <= event.period <= 200:
                    delay = event.period / 1000.0
                time.sleep(delay / self.speed)
            if not self.loop:
                break
            time.sleep(0.5)
        self.daemon_ref.center()
        self.log("replay finished")
        self.finished.set()


# ---------------------------------------------------------------------------
# control socket
# ---------------------------------------------------------------------------


class ControlServer(threading.Thread):
    """Unix socket behind spacemouse-ctl. One JSON line in, one JSON line out."""

    daemon = True

    def __init__(self, daemon_ref, path, stop_event, log=None):
        threading.Thread.__init__(self, name="control-server")
        self.daemon_ref = daemon_ref
        self.path = path
        self.stop_event = stop_event
        self.log = log or (lambda message: None)
        self.sock = None

    def start_listening(self):
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(self.path):
            os.unlink(self.path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.sock.listen(8)
        self.sock.settimeout(1.0)

    def run(self):
        while not self.stop_event.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stop_event.is_set():
                    break
                continue
            try:
                conn.settimeout(2.0)
                data = b""
                while b"\n" not in data and len(data) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                line = data.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
                response = self.dispatch(line)
                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
        try:
            if self.sock:
                self.sock.close()
            if os.path.exists(self.path):
                os.unlink(self.path)
        except OSError:
            pass

    def dispatch(self, line):
        if not line:
            return {"ok": False, "error": "empty request"}
        try:
            request = json.loads(line)
        except ValueError:
            parts = line.split(None, 1)
            request = {"cmd": parts[0], "arg": parts[1] if len(parts) > 1 else ""}
        command = str(request.get("cmd") or "").strip()
        arg = str(request.get("arg") or "").strip()
        return self.daemon_ref.control(command, arg)


# ---------------------------------------------------------------------------
# the daemon
# ---------------------------------------------------------------------------


def runtime_dir():
    base = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    return os.path.join(base, APP_NAME)


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SpaceMouseDaemon(object):
    def __init__(self, args):
        self.args = args
        self.stop_event = threading.Event()
        self.verbose = bool(args.verbose)
        self.profile_set = ProfileSet(args.config, args.defaults, log=self.log)
        self.device = None
        self.engine = None
        self.uinput_state = "closed"
        self.uinput_detail = ""
        self.next_uinput_try = 0.0
        self._uinput_complaint = ""

        self.enabled = True
        self.manual_profile = None  # name of a manual override
        self.started_at = time.monotonic()
        self.focus_known = False
        self.window_class = ""
        self.window_title = ""
        self.current_profile = OFF_PROFILE
        self.last_event = 0.0
        self.axes_lock = threading.Lock()
        self.engine_lock = threading.RLock()
        self.axes = [0] * 6
        self.buttons = queue.Queue()
        self.status_path = os.path.join(runtime_dir(), "status.json")
        self.control_path = os.path.join(runtime_dir(), "control.sock")
        self.status_dirty = True
        self.spnav = None
        self.replay = None
        self.focus = None
        self._control_server = None

    # -- logging -----------------------------------------------------------

    def log(self, message):
        sys.stderr.write("%s: %s\n" % (APP_NAME, message))
        sys.stderr.flush()

    def debug(self, message):
        if self.verbose:
            self.log(message)

    # -- device ------------------------------------------------------------

    def make_device(self):
        if self.args.dry_run:
            stream = sys.stdout
            if self.args.trace:
                stream = open(self.args.trace, "w", encoding="utf-8")
            device = TraceDevice(stream=stream)
            device.open()
            self.uinput_state = "dry-run"
            return device
        return VirtualDevice(path=self.args.uinput)

    def ensure_uinput(self):
        """Open the virtual device lazily, and keep retrying if it is denied.

        The retry is silent after the first complaint. A missing udev rule is a
        standing condition, not an event, and a line every ten seconds for the
        length of a session would bury everything else in the journal.
        """
        if self.device.is_open:
            return True
        now = time.monotonic()
        if now < self.next_uinput_try:
            return False
        try:
            self.device.open()
        except UinputUnavailable as exc:
            self.uinput_state = exc.reason
            self.uinput_detail = str(exc)
            self.next_uinput_try = now + 10.0
            self.status_dirty = True
            if self._uinput_complaint != self.uinput_detail:
                self._uinput_complaint = self.uinput_detail
                self.log(str(exc))
                if exc.reason == "denied":
                    self.log(
                        "add the udev rule and log back in: "
                        'KERNEL=="uinput", MODE="0660", GROUP="uucp". '
                        "Retrying every 10 s, quietly."
                    )
            return False
        self.uinput_state = "ready"
        self.uinput_detail = ""
        self._uinput_complaint = ""
        self.status_dirty = True
        self.log("virtual device '%s' created" % DEVICE_NAME)
        return True

    # -- event intake ------------------------------------------------------

    def handle_event(self, event):
        if event.kind == "motion":
            with self.axes_lock:
                self.axes = list(event.axes)
            self.last_event = time.time()
        elif event.kind in ("press", "release"):
            self.buttons.put((event.button, event.kind == "press"))
            self.last_event = time.time()
            self.debug("puck button %d %s" % (event.button, event.kind))

    def center(self):
        with self.axes_lock:
            self.axes = [0] * 6

    # -- profile selection -------------------------------------------------

    def select_for(self, window_class):
        """The profile a given window class should get right now."""
        if not self.enabled or not self.focus_is_known():
            return OFF_PROFILE
        if self.manual_profile:
            profile = self.profile_set.by_name(self.manual_profile)
            if profile is not None:
                return profile
            self.log(
                "manual profile '%s' is gone, back to automatic" % self.manual_profile
            )
            self.manual_profile = None
        return self.profile_set.select(window_class)

    def desired_profile(self):
        return self.select_for(self.window_class)

    def focus_is_known(self):
        """Whether the focused window has been read yet.

        Until it has, no profile is applied: starting on the fallback profile
        for a moment would arm the emulated gestures against whatever window
        happens to be in front. If Hyprland never answers (it is not running,
        or the socket is gone), the grace period expires and the fallback
        applies after all, which is what a session without a compositor wants.
        """
        if self.args.no_focus or self.focus_known:
            return True
        return (time.monotonic() - self.started_at) > FOCUS_GRACE_SECONDS

    def on_focus(self, window_class, title):
        self.focus_known = True
        if window_class == self.window_class and title == self.window_title:
            return
        self.window_class = window_class
        self.window_title = title
        self.status_dirty = True
        self.debug("focus: class=%r title=%r" % (window_class, title))

    def apply_profile(self, profile=None):
        """Switch the engine to a profile, releasing whatever was held.

        Called from the main loop and from the control socket, so it takes the
        engine lock: a profile change that overlaps a tick must not interleave
        with the tick's own key bookkeeping.
        """
        # Snapshot the class first: the focus watcher runs in its own thread,
        # and a class read after the decision would make the log line describe
        # a window the profile was not chosen for.
        window_class = self.window_class
        profile = profile or self.select_for(window_class)
        with self.engine_lock:
            if profile is self.current_profile:
                return profile
            self.engine.set_profile(profile)
            self.current_profile = profile
        self.status_dirty = True
        self.log(
            "profile -> %s (%s) for class %r"
            % (profile.name, profile.type, window_class)
        )
        return profile

    # -- status ------------------------------------------------------------

    def status(self):
        return {
            "version": VERSION,
            "pid": os.getpid(),
            "enabled": self.enabled,
            "mode": "manual" if self.manual_profile else "auto",
            "profile": self.current_profile.name,
            "profile_type": self.current_profile.type,
            "profile_description": self.current_profile.description,
            "gesture": self.engine.active_name if self.engine else "",
            "window_class": self.window_class,
            "window_title": self.window_title,
            "spnav": "connected"
            if (self.spnav and self.spnav.connected)
            else ("replay" if self.replay else "disconnected"),
            "hyprland": "connected"
            if (self.focus and self.focus.connected)
            else "disconnected",
            "uinput": self.uinput_state,
            "uinput_detail": self.uinput_detail,
            "last_event": round(self.last_event, 3),
            "config": self.args.config,
            "config_error": self.profile_set.error,
            "profiles": [profile.as_dict() for profile in self.profile_set.profiles],
            "updated": round(time.time(), 3),
        }

    def write_status(self):
        payload = json.dumps(self.status(), indent=2)
        directory = os.path.dirname(self.status_path)
        os.makedirs(directory, exist_ok=True)
        tmp = self.status_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        os.replace(tmp, self.status_path)
        self.status_dirty = False

    # -- control -----------------------------------------------------------

    def control(self, command, arg=""):
        if command == "status":
            return {"ok": True, "status": self.status()}
        if command == "enable":
            self.enabled = True
        elif command == "disable":
            self.enabled = False
        elif command == "toggle":
            self.enabled = not self.enabled
        elif command == "profile":
            if not arg:
                return {"ok": False, "error": "profile needs a name"}
            if self.profile_set.by_name(arg) is None:
                return {"ok": False, "error": "no profile named '%s'" % arg}
            self.manual_profile = arg
            self.enabled = True
        elif command == "auto":
            self.manual_profile = None
        elif command == "reload":
            self.profile_set.load()
            with self.engine_lock:
                self.engine.settings = self.profile_set.settings
                self.engine.set_profile(OFF_PROFILE)
                self.current_profile = OFF_PROFILE
            self.manual_profile = None
        elif command == "quit":
            self.stop_event.set()
        else:
            return {"ok": False, "error": "unknown command '%s'" % command}
        # Apply right away instead of waiting for the next tick, so the reply
        # describes the profile that is actually live.
        if self.engine is not None and command != "quit":
            self.apply_profile()
        self.status_dirty = True
        return {"ok": True, "status": self.status()}

    # -- main loop ---------------------------------------------------------

    def run(self):
        self.device = self.make_device()
        self.engine = GestureEngine(
            self.device, self.profile_set.settings, log=self.log
        )

        os.makedirs(runtime_dir(), exist_ok=True)
        self._control_server = ControlServer(
            self, self.control_path, self.stop_event, log=self.log
        )
        try:
            self._control_server.start_listening()
            self._control_server.start()
        except OSError as exc:
            self.log("control socket unavailable (%s), running without it" % exc)

        if self.args.replay:
            self.replay = ReplayReader(
                self,
                self.stop_event,
                self.args.replay,
                speed=self.args.replay_speed,
                loop=self.args.replay_loop,
                log=self.log,
            )
            self.replay.start()
        else:
            self.spnav = SpnavReader(self, self.stop_event, log=self.log)
            self.spnav.start()

        if not self.args.no_focus:
            self.focus = FocusWatcher(self.on_focus, self.stop_event, log=self.log)
            self.focus.start()

        if self.args.profile:
            if self.profile_set.by_name(self.args.profile) is None:
                self.log(
                    "no profile named '%s', staying on automatic" % self.args.profile
                )
            else:
                self.manual_profile = self.args.profile

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._on_signal)

        self.log(
            "started, %d profiles, status in %s"
            % (len(self.profile_set.profiles), self.status_path)
        )
        last = time.monotonic()
        last_config_check = last
        last_status = 0.0
        try:
            while not self.stop_event.is_set():
                hz = float(self.profile_set.settings.get("tick_hz", 60.0)) or 60.0
                period = 1.0 / hz
                now = time.monotonic()
                dt = min(0.25, max(0.0, now - last))
                last = now

                if now - last_config_check >= 1.0:
                    last_config_check = now
                    if self.profile_set.reload_if_changed():
                        with self.engine_lock:
                            self.engine.settings = self.profile_set.settings
                            self.engine.set_profile(OFF_PROFILE)
                            self.current_profile = OFF_PROFILE
                        self.status_dirty = True

                profile = self.apply_profile()

                needs_device = profile.type in ("mouse", "keys")
                if needs_device:
                    self.ensure_uinput()

                with self.engine_lock:
                    while True:
                        try:
                            number, pressed = self.buttons.get_nowait()
                        except queue.Empty:
                            break
                        if needs_device and self.device.is_open:
                            self.engine.handle_button(number, pressed)

                    if needs_device and self.device.is_open:
                        with self.axes_lock:
                            raw = list(self.axes)
                        before = self.engine.active_name
                        self.engine.tick(raw, dt, self.profile_set)
                        if self.engine.active_name != before:
                            self.status_dirty = True

                if (
                    self.replay
                    and self.replay.finished.is_set()
                    and not self.args.replay_loop
                ):
                    self.stop_event.set()

                if self.status_dirty or now - last_status >= 2.0:
                    last_status = now
                    try:
                        self.write_status()
                    except OSError as exc:
                        self.log("could not write status: %s" % exc)

                sleep = period - (time.monotonic() - now)
                if sleep > 0:
                    self.stop_event.wait(sleep)
        finally:
            self.shutdown()
        return 0

    def _on_signal(self, signum, frame):  # noqa: ARG002
        self.log("signal %d, shutting down" % signum)
        self.stop_event.set()

    def shutdown(self):
        self.stop_event.set()
        if self.engine:
            self.engine.release_all()
        if self.device:
            try:
                self.device.close()
            except OSError:
                pass
        self.uinput_state = "closed"
        try:
            self.write_status()
        except OSError:
            pass
        try:
            if os.path.exists(self.control_path):
                os.unlink(self.control_path)
        except OSError:
            pass
        self.log("stopped")


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


def mode_dump(args):
    """Print decoded events, from a capture file or live from spacenavd."""
    if args.dump and args.dump != "-":
        with open(args.dump, "rb") as handle:
            blob = handle.read()
        for index, event in enumerate(iter_frames(blob)):
            if event.kind == "motion":
                print(
                    "%6d  motion  x=%5d y=%5d z=%5d rx=%5d ry=%5d rz=%5d  period=%d"
                    % ((index,) + event.axes + (event.period,))
                )
            elif event.kind in ("press", "release"):
                print("%6d  %-7s button=%d" % (index, event.kind, event.button))
            else:
                print("%6d  other   %s" % (index, event.raw))
        return 0

    path = DEFAULT_SPNAV_SOCKET
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    sock.connect(path)
    sock.settimeout(1.0)
    print("listening on %s, Ctrl-C to stop" % path)
    buffer = b""
    start = time.time()
    try:
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer += chunk
            while len(buffer) >= FRAME_SIZE:
                event = decode_frame(buffer[:FRAME_SIZE])
                buffer = buffer[FRAME_SIZE:]
                stamp = time.time() - start
                if event.kind == "motion":
                    print(
                        "%8.3f  motion  x=%5d y=%5d z=%5d rx=%5d ry=%5d rz=%5d  period=%d"
                        % ((stamp,) + event.axes + (event.period,))
                    )
                elif event.kind in ("press", "release"):
                    print("%8.3f  %-7s button=%d" % (stamp, event.kind, event.button))
                else:
                    print("%8.3f  other   %s" % (stamp, event.raw))
                sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="spacemoused",
        description="Per-application SpaceMouse profiles for Omarchy/Hyprland",
    )
    parser.add_argument(
        "--config",
        default=os.path.join(config_dir(), "profiles.json"),
        help="profile file (default: ~/.config/omarchy-spacemouse/profiles.json)",
    )
    parser.add_argument(
        "--defaults",
        default=os.path.join(repo_root(), "profiles.default.json"),
        help="fallback profile file shipped with the repo",
    )
    parser.add_argument("--uinput", default="/dev/uinput", help="uinput device node")
    parser.add_argument("--profile", default="", help="force a profile by name")
    parser.add_argument(
        "--no-focus", action="store_true", help="do not follow Hyprland focus"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="never touch /dev/uinput, print what would be emitted",
    )
    parser.add_argument(
        "--trace", default="", help="with --dry-run: write the trace here"
    )
    parser.add_argument(
        "--replay", default="", help="feed a recorded .bin capture instead of the puck"
    )
    parser.add_argument(
        "--replay-speed", type=float, default=1.0, help="replay speed multiplier"
    )
    parser.add_argument("--replay-loop", action="store_true", help="loop the capture")
    parser.add_argument(
        "--dump",
        nargs="?",
        const="-",
        default=None,
        help="decode frames from a capture (or live) and print them",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.dump is not None:
        return mode_dump(args)
    return SpaceMouseDaemon(args).run()


if __name__ == "__main__":
    sys.exit(main())
