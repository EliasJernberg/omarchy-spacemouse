"""Shared test helpers: import the daemon by path and fake the kernel away."""

import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON_PATH = os.path.join(REPO, "daemon", "spacemoused.py")
FIXTURE_BIN = os.path.join(
    REPO, "tests", "fixtures", "hardware", "calibration_capture.bin"
)
FIXTURE_TXT = os.path.join(
    REPO, "tests", "fixtures", "hardware", "calibration_capture.txt"
)
DEFAULT_PROFILES = os.path.join(REPO, "profiles.default.json")


def load_daemon():
    """Import daemon/spacemoused.py as a module, without installing anything."""
    if "spacemoused" in sys.modules:
        return sys.modules["spacemoused"]
    spec = importlib.util.spec_from_file_location("spacemoused", DAEMON_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["spacemoused"] = module
    spec.loader.exec_module(module)
    return module


sm = load_daemon()


class FakeIO(object):
    """Stands in for the uinput syscalls and records everything it is told."""

    def __init__(self, fail_open=None, fail_setup=False, sysname="input99"):
        self.fail_open = fail_open  # an OSError to raise from open()
        self.fail_setup = fail_setup  # make UI_DEV_SETUP fail
        self.sysname = sysname
        self.opened = []
        self.ioctls = []  # (request, arg)
        self.grabs = []  # (fd, 1 or 0) for every EVIOCGRAB
        self.writes = []  # bytes
        self.closed = []
        self.next_fd = 42

    def open(self, path):
        if self.fail_open is not None:
            raise self.fail_open
        self.opened.append(path)
        fd = self.next_fd
        self.next_fd += 1
        return fd

    def ioctl(self, fd, request, arg=0):
        if self.fail_setup and request == sm.UI_DEV_SETUP:
            raise OSError(25, "Inappropriate ioctl for device")
        if request == sm.UI_GET_SYSNAME and isinstance(arg, (bytearray, memoryview)):
            # The kernel writes the sysname into the caller's buffer.
            answer = self.sysname.encode("utf-8") + b"\x00"
            arg[: len(answer)] = answer
            self.ioctls.append((request, bytes(answer)))
            return 0
        if request == sm.EVIOCGRAB:
            self.grabs.append((fd, arg))
        self.ioctls.append((request, arg))
        return 0

    def write(self, fd, data):
        self.writes.append(data)
        return len(data)

    def close(self, fd):
        self.closed.append(fd)

    # -- convenience -------------------------------------------------------

    def requests(self, request):
        return [arg for req, arg in self.ioctls if req == request]

    def events(self):
        """Every input_event written so far, as (type, code, value) tuples."""
        import struct

        out = []
        for blob in self.writes:
            if len(blob) % sm.INPUT_EVENT_SIZE:
                continue
            for index in range(len(blob) // sm.INPUT_EVENT_SIZE):
                chunk = blob[
                    index * sm.INPUT_EVENT_SIZE : (index + 1) * sm.INPUT_EVENT_SIZE
                ]
                _, _, etype, code, value = struct.unpack(sm.INPUT_EVENT_FMT, chunk)
                out.append((etype, code, value))
        return out


class FakeClock(object):
    """A clock the tests move by hand, so idle timeouts are not real waits."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def key_events(device):
    """(code, value) for every EV_KEY event a TraceDevice recorded."""
    out = []
    for line in device.records:
        if not line.startswith("syn "):
            continue
        for token in line[4:].split():
            etype, code, value = (int(part) for part in token.split(":"))
            if etype == sm.EV_KEY:
                out.append((code, value))
    return out


def rel_events(device):
    """(code, value) for every EV_REL event a TraceDevice recorded."""
    out = []
    for line in device.records:
        if not line.startswith("syn "):
            continue
        for token in line[4:].split():
            etype, code, value = (int(part) for part in token.split(":"))
            if etype == sm.EV_REL:
                out.append((code, value))
    return out
