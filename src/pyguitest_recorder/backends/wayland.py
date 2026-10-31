"""Capture input on Wayland by reading evdev devices below the compositor.

The acquisition layer base.py's docstring anticipated. No Wayland client can
observe another client's input through the display server -- that is the tier-6
prohibition pyguitest will not cross, and it is why there is no "Wayland
XRecord". But the kernel's input devices sit *below* the compositor, and a
process in the `input` group can read `/dev/input/event*` directly: the same
device nodes, the same permission tier (`input` group / udev ACL), that
pyguitest's own uinput *replay* already requires. That makes this the one
capture path that works identically on every compositor because it does not go
through any of them -- run live on all five in the test fleet: GNOME/Mutter,
KWin, COSMIC, Hyprland and niri.

What it sees and does not:
- It sees every physical click, motion, scroll and key, as `RawEvent`s in the
  same vocabulary XRecord produces, so the existing normalizer consumes them
  unchanged.
- An absolute device (a tablet, or a VM's integration pointer) reports exact
  `x`/`y`, scaled through its own absinfo range to the screen the DRM
  connector reports. A relative mouse reports deltas, so its position is
  tracked -- seeded from the screen centre (or the absolute device's current
  value), clamped to the screen -- and every click, scroll and motion is
  stamped through `_stamp()`, which prefers the true position from an attached
  `CursorSource` (the opt-in ScreenCast cursor stream, `screencast_cursor`)
  over the tracked one. Exact with an absolute device or the cursor stream;
  the bare delta track's seed is a guess whose offset from the real cursor
  never self-corrects, so on a relative-only session it can attribute a click
  to the wrong window -- the gap `screencast_cursor` exists to close.
- A spike measured on GNOME Wayland that AT-SPI emits no input events at all
  (focus/state changes only), which is why targeting is resolved separately
  and this backend is strictly the input stream.
- It admits to what it lost. The kernel drops events when its queue for this
  reader overruns and says so with SYN_DROPPED; the stream is then discarded
  to the end of the truncated packet, held keys are re-read from the device
  so nothing stays logically pressed, and the recording carries a note. A
  device that will not open is skipped with a note rather than failing the
  whole recording. Timestamps are asked of the kernel in the pipeline's own
  clock (`EVIOCSCLOCKID`) so no offset can go stale under them.

Keys are decoded through `libxkbcommon` (`_xkb.py`) under the layout the
`XKB_DEFAULT_*` variables name -- not the compositor's active keymap, and a
runtime layout switch is not tracked; an environment that exports none of them
decodes as the library's built-in default (US). Only where the library will
not load does translation fall back to the fixed US table in `_evdev_keysyms`
-- which is then the same limitation pyguitest's uinput replay carries. The
stop chord is not recognised here: `stop_pressed_at` stays None and the
consumer's own recogniser ends the recording, which base.py explicitly allows.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Iterator, Sequence
from types import ModuleType
from typing import TYPE_CHECKING, Any

from ._xkb import XkbTranslator
from .base import CaptureUnavailable, RawEvent

if TYPE_CHECKING:
    from ._screencast import CursorSource

# NB: `_evdev_keysyms` is imported lazily (in `_decode_key`), not here: it does
# `from evdev import ecodes` at module scope, so importing it at the top would
# make `import ...backends.wayland` raise a bare ImportError when python-evdev
# is absent -- defeating the clean `CaptureUnavailable` that `_evdev()` raises
# for exactly that case (a pure-Wayland box without the `[wayland]` extra).

__all__ = ["WaylandCaptureBackend", "available", "pick_devices"]

# evdev button code -> X button number (the convention XRecord/detail uses).
_BUTTONS = {
    0x110: 1,
    0x112: 2,
    0x111: 3,
    0x113: 8,
    0x114: 9,
}  # LEFT MIDDLE RIGHT SIDE EXTRA

_EVIOCSCLOCKID = 0x400445A0
"""`EVIOCSCLOCKID`: ask a device to stamp its events with another clock.

`_IOW('E', 0xa0, int)` expanded by hand -- `(1 << 30) | (4 << 16) |
(ord('E') << 8) | 0xa0` -- because Python has no `_IOW`. Correct for every
architecture using asm-generic's `ioctl.h`, which is every one this backend
runs on; an architecture that lays the bits out differently (alpha, mips,
powerpc, sparc) refuses the ioctl rather than acting on a wrong request
number, and `_clock_offset` is the fallback for exactly that.
"""


def _eviocgkey(size: int) -> int:
    """`EVIOCGKEY(size)`: read a device's whole key-state bitmap.

    `_IOC(_IOC_READ, 'E', 0x18, size)`, expanded for the same reason as
    `_EVIOCSCLOCKID`. The length is part of the request number, so this is a
    function where that is a constant.
    """
    return 0x80004518 | (size << 16)


class _SyntheticKey:
    """A key event this backend makes itself, closing a gap an overrun left.

    `_decode_key` reads only `code` and `value` off an event, so a resync
    needs no more of one than this -- and a named class keeps a fabricated
    event legible as such at the call site, which a tuple or a mock would not.
    """

    __slots__ = ("code", "value")

    def __init__(self, code: int, value: int) -> None:
        self.code, self.value = code, value


def _evdev() -> ModuleType:
    # Broad except, not just ImportError: FreeBSD ships a partial python-evdev
    # whose `ecodes` raises KeyError at import. Either way evdev is unusable
    # here, and the caller (choose_backend's auto fallback, available()) must
    # get a clean CaptureUnavailable -- not a raw KeyError crashing the doctor
    # on a BSD box with no X server to fall back from.
    try:
        import evdev  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - any import failure means no evdev
        raise CaptureUnavailable(
            "python-evdev is not installed or not usable on this platform"
        ) from exc
    # Annotated rather than returned directly: CI's lint job runs mypy in an
    # environment without evdev installed, where the import resolves to Any
    # and a bare `return evdev` fails strict's no-any-return. An ignore or a
    # cast would be flagged as unused/redundant wherever evdev IS installed,
    # so the annotation is the one spelling that type-checks in both.
    module: ModuleType = evdev
    return module


def drm_screen_size() -> tuple[int, int] | None:
    """The current mode of the first enabled DRM connector, or None.

    Read from sysfs because this backend deliberately has no display
    connection to ask: it is not an X client and must not become a Wayland
    client. The first line of an enabled connector's `modes` is its current
    mode -- confirmed against a live KWin session (`kscreen-doctor` and this
    agreed on 1280x800). One screen is assumed; multi-monitor arithmetic is
    the DPI/multi-monitor matrix's territory, not guessed at here.
    """
    import glob  # noqa: PLC0415

    for status in sorted(glob.glob("/sys/class/drm/*/enabled")):
        try:
            with open(status) as fh:
                if fh.read().strip() != "enabled":
                    continue
            with open(status[: -len("enabled")] + "modes") as fh:
                first = fh.readline().strip()
            width, height = first.split("x", 1)
            return int(width), int(height)
        except (OSError, ValueError):
            continue
    return None


def pick_devices() -> list[str]:
    """Paths of the readable keyboard and pointer devices, buttons excluded."""
    evdev = _evdev()
    from evdev import ecodes as e  # noqa: PLC0415

    chosen = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue  # not readable -- wrong permissions or gone
        try:
            caps = dev.capabilities()
        except OSError:
            # Vanished between open and the ioctl (hotplug churn); the
            # rescan that calls this runs on the reader thread, where an
            # escaped OSError would end capture for the rest of the run.
            with contextlib.suppress(OSError):
                dev.close()
            continue
        if _wants_device(e, caps):
            chosen.append(path)
        with contextlib.suppress(OSError):
            dev.close()
    return chosen


def _wants_device(e: Any, caps: dict[Any, Any]) -> bool:
    """Whether a device's capabilities make it a keyboard or a pointer.

    A keyboard is recognised by `KEY_A`. A pointer must carry pointer
    *buttons* (BTN_LEFT/BTN_TOUCH) or relative pointer axes (REL_X/REL_Y, or
    a wheel). EV_ABS alone is deliberately NOT a pointer: gamepads expose ABS
    sticks and accelerometer nodes expose ABS axes, and treating those as
    pointers captured joystick noise as motion and -- worse -- let
    `_learn_abs_range` seed the pointer position from a stick's resting
    value.
    """
    keys = set(caps.get(e.EV_KEY, []))
    if e.KEY_A in keys:
        return True
    if {e.BTN_LEFT, e.BTN_TOUCH} & keys:
        return True
    rel = set(caps.get(e.EV_REL, []))
    return bool({e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL} & rel)


def available() -> bool:
    """Whether this machine can capture via evdev (devices readable now)."""
    try:
        return bool(pick_devices())
    except CaptureUnavailable:
        return False


class WaylandCaptureBackend:
    """Capture raw input from evdev devices below the compositor."""

    name = "wayland-evdev"

    def __init__(
        self,
        paths: Sequence[str] | None = None,
        screen_size: tuple[int, int] | None = None,
        screen: int = 0,
        cursor_source: CursorSource | None = None,
    ) -> None:
        """Capture `paths`, or auto-pick keyboard+pointer devices if None.

        `screen_size` is the pixel size absolute-device coordinates are scaled
        to; None asks the DRM connector at `start`. `screen` is the opaque tag
        carried on every event, the same meaning it has on every backend.

        `cursor_source`, when given, is the ScreenCast cursor reader (the
        `screencast_cursor` opt-in): it is started and stopped with this
        backend, and while it has a position every event is stamped with that
        true position instead of the delta-tracked one. None keeps the
        delta tracking, which is every case but a bare-metal relative mouse.
        """
        self._paths = list(paths) if paths is not None else None
        self._screen_size = screen_size
        self._screen = screen
        self._cursor = cursor_source
        self._queue: queue.Queue[RawEvent | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._devices: list[Any] = []
        self._abs_ranges: dict[int, tuple[int, int, int, int]] = {}
        self._padlike_fds: set[int] = set()
        """Fds whose ABS stream is not a screen position and must be ignored:
        a touchpad's pad-local finger coordinates, a gamepad's sticks, a
        sensor's axes. Their button events still decode normally."""
        self._pos: tuple[int, int] | None = None  # pointer position, if known
        self._clock_offset = 0.0
        """Added to a kernel timestamp to rebase it onto `time.monotonic`.

        Only for devices whose `EVIOCSCLOCKID` ioctl failed. Where it succeeds
        -- every ordinary Linux device -- the kernel stamps that device's
        events with CLOCK_MONOTONIC itself and no arithmetic is needed; see
        `_monotonic_fds`. Otherwise the stamps are CLOCK_REALTIME, while every
        other backend -- and the recorder's own `_now()`, which the analyzer's
        offsets, `--regenerate --from/--to` trimming and the capture-lag
        accounting (the password focus-lag gate included) are all measured
        against -- speaks `time.monotonic`.

        Asking the kernel is not merely tidier than this subtraction, which is
        why it is tried first. `start` captures the offset once, and a single
        offset does not survive NTP or an administrator stepping the realtime
        clock mid-recording: it *carries the step into the timeline* as a gap
        that never happened, where a gap is the one thing the analyzer reads
        as the user pausing. Recomputing it per event would trade that for a
        timeline that jitters by the drift between two clocks. Only being
        stamped in the pipeline's own clock avoids both. Stays 0.0 for the
        unit tests, which feed `_decode` fabricated timestamps directly.
        """
        self._monotonic_fds: set[int] = set()
        """Fds the kernel stamps with CLOCK_MONOTONIC, which `_clock_offset`
        must not be added to.

        An fd absent here takes the offset -- including the `None` the unit
        tests decode with, which is what keeps a fabricated timestamp
        answering to `_clock_offset` alone.
        """
        self._xkb: XkbTranslator | None = None
        self._held_shift = False  # fallback modifier state when xkb is absent
        self._pending: dict[int | None, dict[str, int]] = {}
        """Per device, the motion banked so far in the packet it is building.

        Per device because a packet belongs to one: it runs from that device's
        events to *that device's* SYN_REPORT, so a SYN_REPORT must never flush
        motion another device banked, and a SYN_DROPPED must never discard it.
        Shared, as this was, both were possible by construction.

        Reaching either from userspace turns out to be another matter, and the
        honest answer is that this is a latent trap rather than a bug anyone
        has seen: the input core buffers a device's events and force-emits a
        SYN_REPORT when its value buffer fills, so a reader is never handed a
        partial packet and a second device is never mid-packet. Measured on
        Debian -- an un-synced write delivers nothing at all, and 120 key
        events in one packet came back as whole packets, three of them. So
        the keying is kept for the invariant it makes unconditional, not for a
        symptom it fixes. Empty entries are dropped so the map is `{}` at rest.
        """
        self._dropped_fds: set[int | None] = set()
        """Fds in the middle of a SYN_DROPPED overrun, whose events are being
        discarded until the SYN_REPORT that closes the incomplete packet."""
        self._held_keys: dict[int | None, set[int]] = {}
        """Per device, the codes this backend believes are held down.

        Per device because the authority it is reconciled against --
        `EVIOCGKEY` -- answers for one device, so a key held on a *different*
        keyboard must not look like one this device lost.
        """
        self.notes: list[str] = []
        """What happened during capture that the recording should admit to.

        Drained into the recording's environment block by the recorder's
        `_collect_warnings`, the same channel the resolver's late warnings
        take, so an overrun or a skipped device reaches both the session
        file's header and the generated script's footer. Appended from the
        reader thread and read once it has been joined; a list append is
        atomic under the GIL, and nothing here reads it in between.
        """
        self.stop_pressed_at: float | None = None

    # -- CaptureBackend protocol --------------------------------------------
    def start(self) -> None:
        """Open the devices, learn the screen, and begin the reader thread."""
        evdev = _evdev()
        from evdev import ecodes as e  # noqa: PLC0415

        paths = self._paths if self._paths is not None else pick_devices()
        if not paths:
            raise CaptureUnavailable("no readable evdev input devices")
        self._devices, failures = self._open(evdev, paths)
        if not self._devices:
            raise CaptureUnavailable(
                "cannot open any input device: " + "; ".join(failures)
            )
        if self._screen_size is None:
            self._screen_size = drm_screen_size()
        self._clock_offset = time.monotonic() - time.time()
        for dev in self._devices:
            self._use_monotonic_clock(dev)
            self._learn_abs_range(e, dev)
        # Layout-aware key decoding through libxkbcommon where it is present;
        # None falls back to the fixed US table in `_decode_key`.
        self._xkb = XkbTranslator.create()
        if self._cursor is not None:
            # Best-effort: a denied or absent ScreenCast cursor source leaves
            # the delta tracking in place, never fails the recording.
            self._cursor.start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _open(
        self, evdev: ModuleType, paths: Sequence[str]
    ) -> tuple[list[Any], list[str]]:
        """Open what can be opened; return the devices and what refused.

        One device at a time rather than a comprehension over all of them.
        A single path can be denied or already gone while its neighbours are
        perfectly readable -- hotplug churn, a udev ACL granted for some
        devices and not others, a uinput device whose creator exited between
        `pick_devices` and here -- and failing the whole recording for one of
        them is the comprehension's behaviour. Worse, it *leaked* every
        descriptor already opened, because the half-built list went out of
        scope with the exception and `self._devices` was never assigned, so
        `stop` had nothing to close.

        Capture with a keyboard and no mouse is worth more than no capture at
        all, so a partial set starts; `start` still refuses when nothing
        opened. What was skipped goes in the notes, because a device missing
        from a recording is invisible in it.
        """
        devices: list[Any] = []
        failures: list[str] = []
        for path in paths:
            try:
                devices.append(evdev.InputDevice(path))
            except OSError as exc:
                failures.append(f"{path} ({exc.strerror or exc})")
        if failures and devices:
            self.notes.append(
                f"capture could not open {len(failures)} of {len(paths)} input "
                f"devices and recorded the rest: {'; '.join(failures)}"
            )
        return devices, failures

    def _use_monotonic_clock(self, dev: Any) -> None:
        """Ask the kernel to stamp this device's events with CLOCK_MONOTONIC.

        The clock correction that cannot go stale. python-evdev never issues
        this ioctl, so without it the stamps are CLOCK_REALTIME and have to be
        rebased by an offset a clock step invalidates -- see `_clock_offset`,
        which stays as the fallback for a kernel or driver that refuses.

        Per device, and before the reader thread starts: events already queued
        when it is set keep the clock they were stamped with, which is why
        this runs in `start` ahead of the thread rather than on first read.
        """
        import fcntl  # noqa: PLC0415
        import struct  # noqa: PLC0415

        try:
            fd = dev.fileno()
            fcntl.ioctl(fd, _EVIOCSCLOCKID, struct.pack("i", time.CLOCK_MONOTONIC))
        except (OSError, AttributeError, ValueError):
            return  # old kernel, odd driver, or a device that just vanished
        self._monotonic_fds.add(fd)

    def _forget_fd(self, fd: int) -> None:
        """Drop the per-device state keyed by a descriptor that has closed.

        Every per-device map here is keyed by file descriptor, and the kernel
        hands the same number back out as soon as it is free -- so a device
        picked up by the rescan can inherit the number a departed one had.
        Left behind, those entries would tell the new device's events the
        wrong clock, scale its coordinates through a stranger's absinfo range,
        or hold a key down that nothing is pressing.
        """
        self._monotonic_fds.discard(fd)
        self._dropped_fds.discard(fd)
        self._abs_ranges.pop(fd, None)
        self._padlike_fds.discard(fd)
        self._held_keys.pop(fd, None)
        self._pending.pop(fd, None)

    def _learn_abs_range(self, e: Any, dev: Any) -> None:
        """Cache an absolute device's range, and seed the pointer from it.

        The range is for scaling later events. The *current* value matters
        too: a recording whose first pointer event is a click -- no motion
        before it, because the pointer was already where it was wanted -- would
        otherwise stamp (0, 0), a click into the corner. Seeding the position
        from where the device already is makes that first click land where the
        pointer actually is, and any later motion corrects it regardless.

        A touchpad is the exception: it reports ABS_X/Y too, but those are
        finger positions in pad-local units that the compositor turns into
        *relative* cursor motion with acceleration -- scaling them to screen
        pixels would stamp coordinates that bear no relation to the cursor.
        Touchpads are recognised by `BTN_TOOL_FINGER`, the tool code every
        touchpad reports and no screen-mapped device (a drawing tablet, a
        VM's integration pointer) does; their ABS events are ignored outright
        and only their button clicks are recorded, stamped from the tracked
        or ScreenCast position like a relative mouse's.
        """
        try:
            caps = dev.capabilities()
        except OSError:
            return  # the device vanished between open and this ioctl
        if e.EV_ABS not in caps:
            return
        keys = set(caps.get(e.EV_KEY, []))
        if not ({e.BTN_LEFT, e.BTN_TOUCH} & keys):
            # ABS axes with no pointer button: a gamepad stick, an
            # accelerometer. Guarded here as well as in `_wants_device`
            # because an explicit `paths` list bypasses the picker, and a
            # stick's resting value must never become the pointer's seed --
            # nor its events motion: with no registered range `_scale_abs`
            # passes raw device units through, so the fd joins the ignore set
            # and `_decode` drops its ABS stream outright.
            self._padlike_fds.add(dev.fileno())
            return
        if e.BTN_TOOL_FINGER in keys:
            self._padlike_fds.add(dev.fileno())
            return
        absinfo = dict(caps[e.EV_ABS])
        ax, ay = absinfo.get(e.ABS_X), absinfo.get(e.ABS_Y)
        if ax is None or ay is None or ax.max <= ax.min:
            return
        fd = dev.fileno()
        self._abs_ranges[fd] = (ax.min, ax.max, ay.min, ay.max)
        if self._pos is None and self._screen_size is not None:
            self._pos = (
                self._scale_abs(ax.value, fd, axis=0),
                self._scale_abs(ay.value, fd, axis=1),
            )

    def events(self) -> Iterator[RawEvent]:
        """Yield events as they arrive, blocking, until `stop`."""
        while True:
            item = self._queue.get()
            if item is None:
                return
            yield item

    def drain(self) -> Iterator[RawEvent]:
        """Yield what is already buffered, without blocking."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                yield item

    def stop(self) -> None:
        """Stop the reader and release the devices."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        for dev in self._devices:
            with contextlib.suppress(OSError):
                dev.close()
        if self._xkb is not None:
            with contextlib.suppress(Exception):
                self._xkb.close()
            self._xkb = None
        if self._cursor is not None:
            with contextlib.suppress(Exception):
                self._cursor.stop()
        self._queue.put(None)  # unblock any events() consumer

    # -- reader -------------------------------------------------------------
    _RESCAN_SECONDS = 2.0
    """How often auto-picked capture rescans /dev/input for new devices.

    Input devices appear mid-recording in completely ordinary sessions: a
    mouse plugged in, a Bluetooth keyboard waking, and -- the case measured
    here -- a replay tool creating its uinput device after capture began. A
    recording that silently missed everything from such a device would look
    complete and be wrong, which is the one kind of gap this backend must
    never produce. Only auto-picked capture rescans; an explicit `paths` list
    asked for exactly those devices.
    """

    def _run(self) -> None:
        import selectors  # noqa: PLC0415
        import time  # noqa: PLC0415

        from evdev import ecodes as e  # noqa: PLC0415

        sel = selectors.DefaultSelector()
        watched: set[str] = set()
        for dev in self._devices:
            sel.register(dev.fileno(), selectors.EVENT_READ, dev)
            watched.add(dev.path)
        next_rescan = time.monotonic() + self._RESCAN_SECONDS
        while not self._stop.is_set():
            for key, _ in sel.select(timeout=0.2):
                try:
                    for ev in key.data.read():
                        self._decode(e, ev, key.data.fileno())
                except BlockingIOError:
                    pass
                except OSError:
                    # The device went away (unplugged, or its uinput creator
                    # exited). Everything read before this is already queued.
                    with contextlib.suppress(KeyError, ValueError):
                        sel.unregister(key.fd)
                    watched.discard(key.data.path)
                    with contextlib.suppress(OSError):
                        key.data.close()
                    # After the close, since the descriptor number is only
                    # free to be reissued once it is closed -- see `_forget_fd`.
                    self._forget_fd(key.fd)
            if self._paths is None and time.monotonic() >= next_rescan:
                next_rescan = time.monotonic() + self._RESCAN_SECONDS
                self._rescan(e, sel, watched)

    def _rescan(self, e: Any, sel: Any, watched: set[str]) -> None:
        """Pick up devices that appeared since capture started.

        Guarded per device: one can vanish between any two calls here (its
        uinput creator exiting is the very case the rescan exists for), and an
        OSError escaping onto the reader thread would end it -- every event
        for the rest of the recording silently lost, the one failure mode
        this backend must never have.
        """
        import selectors  # noqa: PLC0415

        for path in pick_devices():
            if path in watched:
                continue
            try:
                dev = _evdev().InputDevice(path)
            except OSError:
                continue
            try:
                self._use_monotonic_clock(dev)
                self._learn_abs_range(e, dev)
                sel.register(dev.fileno(), selectors.EVENT_READ, dev)
            except OSError:
                with contextlib.suppress(OSError):
                    dev.close()
                continue
            watched.add(path)
            self._devices.append(dev)

    def _decode(self, e: Any, ev: Any, fd: int | None = None) -> None:
        ts = ev.timestamp()
        if fd not in self._monotonic_fds:
            # A device the EVIOCSCLOCKID ioctl could not be set on: its
            # stamps are CLOCK_REALTIME and need rebasing onto the clock the
            # rest of the pipeline keeps. See `_clock_offset`.
            ts += self._clock_offset
        if ev.type == e.EV_SYN:
            self._decode_syn(e, ev, ts, fd)
            return
        if fd in self._dropped_fds:
            # Mid-overrun. This device's stream is not merely shorter, it is
            # incoherent, until the SYN_REPORT that ends the packet the kernel
            # truncated -- so everything up to that point goes on the floor,
            # which is what the kernel's own contract asks of a reader.
            return
        if ev.type == e.EV_KEY:
            self._decode_key(e, ev, ts, fd)
        elif ev.type == e.EV_REL:
            self._decode_rel(e, ev, ts, fd)
        elif ev.type == e.EV_ABS:
            if fd in self._padlike_fds:
                return  # a touchpad's pad-local finger position, not a cursor
            if ev.code == e.ABS_X:
                self._bank(fd)["x"] = self._scale_abs(ev.value, fd, axis=0)
            elif ev.code == e.ABS_Y:
                self._bank(fd)["y"] = self._scale_abs(ev.value, fd, axis=1)

    def _decode_syn(self, e: Any, ev: Any, ts: float, fd: int | None) -> None:
        """Handle a synchronisation event: the end of a packet, or a loss.

        Split out of `_decode` rather than sitting among its branches because
        the two SYN codes are the only ones whose meaning is about the stream
        itself instead of about input, and because a device mid-overrun has to
        be let through to its closing SYN_REPORT while everything else from it
        is discarded -- an order that reads as a special case inline and as a
        rule here.
        """
        if ev.code == e.SYN_DROPPED:
            self._begin_resync(fd)
        elif ev.code == e.SYN_REPORT:
            if fd in self._dropped_fds:
                self._end_resync(e, fd, ts)
            else:
                self._flush(ts, fd)

    def _bank(self, fd: int | None) -> dict[str, int]:
        """This device's packet-in-progress, created on first use."""
        return self._pending.setdefault(fd, {})

    def _begin_resync(self, fd: int | None) -> None:
        """Start discarding a device's stream, a SYN_DROPPED having arrived.

        The kernel emits SYN_DROPPED when its event queue for this reader
        overran and it threw events away -- the recorder falling behind under
        load being exactly when that happens. What is left is not a shorter
        stream but an incoherent one: a press whose release was dropped holds
        a modifier down over everything after it, and half a motion packet
        stamps a coordinate the pointer was never at. A recording like that
        looks complete and is wrong, which is the one failure this backend
        must not have, so the kernel's contract
        (Documentation/input/event-codes.rst) is followed: discard through the
        next SYN_REPORT, then re-read the device's state.

        Only this device's banked packet goes, since `_pending` is keyed by
        descriptor: the overrun is per reader-device queue, so a second
        pointer mid-packet lost nothing and has nothing to re-read.
        """
        self._dropped_fds.add(fd)
        self._pending.pop(fd, None)

    def _end_resync(self, e: Any, fd: int | None, ts: float) -> None:
        """Resume a device, reconciling what its keys are really doing.

        Called on the SYN_REPORT that closes the dropped window. The kernel is
        the only authority left on what is held, since the events that would
        have said so are the ones it discarded: a key this backend believed
        down that `EVIOCGKEY` reports up gets the release it lost, and one
        reported down that was missed gets its press. Both are stamped at the
        resync, because the moments they really happened are what the overrun
        took.

        Reconstructed in both directions on purpose. Releasing what is no
        longer held is the point -- a dropped Shift or Ctrl release otherwise
        holds that modifier over the whole rest of the recording -- and
        pressing what is held keeps every release in the stream paired with a
        press, which is the shape the normalizer reconstructs holds from. The
        recording says so either way: a note, not a silent repair -- written
        *after* the kernel is asked, because whether the reconciliation
        actually happened is part of what there is to admit to. Claiming held
        keys were re-read when `EVIOCGKEY` refused would describe a repair
        that did not occur, over a recording that can still hold a key down
        for the whole rest of its length.
        """
        self._dropped_fds.discard(fd)
        down = self._kernel_keys_down(e, fd)
        self.notes.append(
            "the kernel dropped input events while recording (SYN_DROPPED -- its "
            "queue for the recorder overran, which happens when the machine is "
            "under load): the truncated packet was discarded and "
            + (
                "held keys were re-read from the device"
                if down is not None
                else "the device would not say which keys it holds, so one may "
                "stay down for the rest of the recording"
            )
            + ", so some input is missing between the overrun and the recovery"
        )
        if down is None:
            return  # cannot be asked; the held set stands rather than guessing
        held = self._held_keys.setdefault(fd, set())
        for code in sorted(held - down):
            self._decode_key(e, _SyntheticKey(code, 0), ts, fd)
        for code in sorted(down - held):
            self._decode_key(e, _SyntheticKey(code, 1), ts, fd)

    def _kernel_keys_down(self, e: Any, fd: int | None) -> set[int] | None:
        """Which of a device's key codes the kernel reports held, or None.

        `EVIOCGKEY` fills a bitmap covering every code up to `KEY_MAX`, which
        includes the pointer buttons -- a button stuck down by an overrun is
        as damaging as a modifier. None where it cannot be asked: no
        descriptor (the unit tests decode without one) or a driver that
        refuses, in which case `_end_resync` leaves the held set alone rather
        than inventing events from a bitmap it does not have.
        """
        if fd is None:
            return None
        import fcntl  # noqa: PLC0415

        size = (e.KEY_MAX + 8) // 8
        try:
            buf = fcntl.ioctl(fd, _eviocgkey(size), bytes(size))
        except OSError:
            return None
        return {
            code for code in range(e.KEY_MAX + 1) if buf[code // 8] & (1 << (code % 8))
        }

    def _track_held(self, fd: int | None, code: int, value: int) -> None:
        """Record that a code went down or came up, for overrun reconciliation.

        Only for codes this backend actually emits an event for, so that
        `_end_resync` never fabricates one for a code it would drop anyway (a
        touchscreen's BTN_TOUCH, a stylus's tool codes).
        """
        held = self._held_keys.setdefault(fd, set())
        if value == 1:
            held.add(code)
        else:
            held.discard(code)

    def _scale_abs(self, value: int, fd: int | None, axis: int) -> int:
        """Scale a device-unit ABS value to screen pixels where both are known.

        An absolute device reports its own coordinate space (the VirtualBox
        tablet is 0..32767, its integration pointer 0..65535); the screen is
        pixels. Without the device's range or the screen size the raw value
        passes through -- honest, and what the unit tests feed directly.
        """
        rng = self._abs_ranges.get(fd) if fd is not None else None
        if rng is None or self._screen_size is None:
            return value
        amin, amax = (rng[0], rng[1]) if axis == 0 else (rng[2], rng[3])
        extent = self._screen_size[axis]
        return round((value - amin) * (extent - 1) / (amax - amin))

    def _stamp(self) -> tuple[int, int]:
        """The position to put on an event, preferring the cursor source.

        When the ScreenCast cursor source is running and has reported a
        position it is the ground truth (the whole point of the opt-in), so it
        wins over the delta-tracked `_pos`; otherwise the tracked position, or
        (0, 0) before anything is known.
        """
        if self._cursor is not None:
            pos = self._cursor.position()
            if pos is not None:
                return pos
        return self._pos if self._pos is not None else (0, 0)

    def _decode_key(self, e: Any, ev: Any, ts: float, fd: int | None = None) -> None:
        code, val = ev.code, ev.value
        if code in _BUTTONS:
            if val in (0, 1):
                self._track_held(fd, code, val)
                x, y = self._stamp()
                self._emit(
                    RawEvent(
                        kind="button_press" if val == 1 else "button_release",
                        timestamp=ts,
                        x=x,
                        y=y,
                        screen=self._screen,
                        button=_BUTTONS[code],
                    )
                )
            return
        if code in e.BTN:
            # A button code with no X mapping: BTN_TOUCH, BTN_TOOL_FINGER,
            # BTN_STYLUS and friends. Running those through the keysym decoder
            # fabricated key_press events with garbage keysyms ("0x0",
            # "BTN_TOUCH") that generate un-replayable `tap_key` calls, so
            # they are dropped. Known limit: a tap on a device that reports
            # only BTN_TOUCH (a bare touchscreen) is not recorded as a click.
            return
        if val == 2:  # autorepeat -- the normalizer reconstructs holds, drop it
            return
        pressed = val == 1
        self._track_held(fd, code, val)
        if self._xkb is not None:
            # xkb tracks every modifier itself (Shift, AltGr/level3, Caps) and
            # decodes under the session's real layout; feed it every key.
            keysym, text = self._xkb.key(code, pressed)
        else:
            from ._evdev_keysyms import keysym_and_text  # noqa: PLC0415

            if code in (e.KEY_LEFTSHIFT, e.KEY_RIGHTSHIFT):
                self._held_shift = pressed
            keysym, text = keysym_and_text(code, self._held_shift)
            text = text if pressed else ""
        self._emit(
            RawEvent(
                kind="key_press" if pressed else "key_release",
                timestamp=ts,
                keysym=keysym,
                text=text,
            )
        )

    def _decode_rel(self, e: Any, ev: Any, ts: float, fd: int | None = None) -> None:
        if ev.code == e.REL_X:
            bank = self._bank(fd)
            bank["dx"] = bank.get("dx", 0) + ev.value
        elif ev.code == e.REL_Y:
            bank = self._bank(fd)
            bank["dy"] = bank.get("dy", 0) + ev.value
        elif ev.code in (e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES):
            # The same physical detent again, in 120ths: a modern mouse emits
            # BOTH the classic code (value 1) and the hi-res one (value 120)
            # per notch, so honouring both recorded -- and replayed -- a
            # 121-detent scroll for every click of the wheel. The classic
            # code alone is the detent convention the pipeline speaks; what
            # hi-res adds (sub-detent positions from a free-spinning wheel)
            # has no representation downstream to lose.
            return
        elif ev.code == e.REL_WHEEL:
            x, y = self._stamp()
            self._emit(
                RawEvent(
                    kind="scroll",
                    timestamp=ts,
                    x=x,
                    y=y,
                    screen=self._screen,
                    dy=ev.value,
                )
            )
        elif ev.code == e.REL_HWHEEL:
            x, y = self._stamp()
            self._emit(
                RawEvent(
                    kind="scroll",
                    timestamp=ts,
                    x=x,
                    y=y,
                    screen=self._screen,
                    dx=ev.value,
                )
            )

    def _flush(self, ts: float, fd: int | None = None) -> None:
        # Both branches stamp through `_stamp`, so when the ScreenCast cursor
        # source is running its true position is what every motion carries;
        # with it off `_stamp` returns the position each branch just computed,
        # so the behaviour is unchanged (the absolute and delta-tracked cases).
        #
        # Only this device's bank is flushed -- a SYN_REPORT closes the packet
        # of the device that sent it, not whatever another pointer happens to
        # be mid-way through. `self._pos` stays shared, as the one cursor the
        # seat actually has.
        pending = self._pending.get(fd)
        if not pending:
            return
        if "x" in pending or "y" in pending:
            prev = self._pos or (0, 0)
            self._pos = (
                pending.pop("x", prev[0]),
                pending.pop("y", prev[1]),
            )
            x, y = self._stamp()
            self._emit(
                RawEvent(kind="motion", timestamp=ts, x=x, y=y, screen=self._screen)
            )
        dx = pending.pop("dx", 0)
        dy = pending.pop("dy", 0)
        if not pending:
            self._pending.pop(fd, None)  # keep the map `{}` at rest
        if dx or dy:
            # A relative device knows only deltas; its absolute position is
            # tracked from the screen's centre once the screen is known, and
            # clamped to it -- degraded next to a true absolute device or the
            # cursor source, said so in the docs, and better than (0, 0).
            if self._screen_size is not None:
                w, h = self._screen_size
                px, py = self._pos if self._pos is not None else (w // 2, h // 2)
                self._pos = (
                    min(max(px + dx, 0), w - 1),
                    min(max(py + dy, 0), h - 1),
                )
            x, y = self._stamp()
            self._emit(
                RawEvent(
                    kind="motion",
                    timestamp=ts,
                    x=x,
                    y=y,
                    screen=self._screen,
                    dx=dx,
                    dy=dy,
                )
            )

    def _emit(self, event: RawEvent) -> None:
        self._queue.put(event)
