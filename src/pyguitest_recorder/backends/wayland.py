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
        """Added to every kernel timestamp to rebase it onto `time.monotonic`.

        The kernel stamps evdev events with CLOCK_REALTIME (python-evdev never
        issues EVIOCSCLOCKID), but every other backend -- and the recorder's
        own `_now()`, which the analyzer's offsets, `--regenerate --from/--to`
        trimming and the capture-lag accounting (the password focus-lag gate
        included) are all measured against -- speaks `time.monotonic`. `start`
        captures the realtime-to-monotonic offset once, so the whole recording
        shares one clock even across an NTP step mid-run; it stays 0.0 for the
        unit tests, which feed `_decode` fabricated timestamps directly.
        """
        self._xkb: XkbTranslator | None = None
        self._held_shift = False  # fallback modifier state when xkb is absent
        self._pending: dict[str, int] = {}
        self.stop_pressed_at: float | None = None

    # -- CaptureBackend protocol --------------------------------------------
    def start(self) -> None:
        """Open the devices, learn the screen, and begin the reader thread."""
        evdev = _evdev()
        from evdev import ecodes as e  # noqa: PLC0415

        paths = self._paths if self._paths is not None else pick_devices()
        if not paths:
            raise CaptureUnavailable("no readable evdev input devices")
        try:
            self._devices = [evdev.InputDevice(p) for p in paths]
        except OSError as exc:
            raise CaptureUnavailable(f"cannot open {exc.filename}: {exc}") from exc
        if self._screen_size is None:
            self._screen_size = drm_screen_size()
        self._clock_offset = time.monotonic() - time.time()
        for dev in self._devices:
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
                self._learn_abs_range(e, dev)
                sel.register(dev.fileno(), selectors.EVENT_READ, dev)
            except OSError:
                with contextlib.suppress(OSError):
                    dev.close()
                continue
            watched.add(path)
            self._devices.append(dev)

    def _decode(self, e: Any, ev: Any, fd: int | None = None) -> None:
        # Rebased onto time.monotonic -- the clock the rest of the pipeline
        # keeps; see `_clock_offset`.
        ts = ev.timestamp() + self._clock_offset
        if ev.type == e.EV_KEY:
            self._decode_key(e, ev, ts)
        elif ev.type == e.EV_REL:
            self._decode_rel(e, ev, ts)
        elif ev.type == e.EV_ABS:
            if fd in self._padlike_fds:
                return  # a touchpad's pad-local finger position, not a cursor
            if ev.code == e.ABS_X:
                self._pending["x"] = self._scale_abs(ev.value, fd, axis=0)
            elif ev.code == e.ABS_Y:
                self._pending["y"] = self._scale_abs(ev.value, fd, axis=1)
        elif ev.type == e.EV_SYN and ev.code == e.SYN_REPORT:
            self._flush(ts)

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

    def _decode_key(self, e: Any, ev: Any, ts: float) -> None:
        code, val = ev.code, ev.value
        if code in _BUTTONS:
            if val in (0, 1):
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

    def _decode_rel(self, e: Any, ev: Any, ts: float) -> None:
        if ev.code == e.REL_X:
            self._pending["dx"] = self._pending.get("dx", 0) + ev.value
        elif ev.code == e.REL_Y:
            self._pending["dy"] = self._pending.get("dy", 0) + ev.value
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

    def _flush(self, ts: float) -> None:
        # Both branches stamp through `_stamp`, so when the ScreenCast cursor
        # source is running its true position is what every motion carries;
        # with it off `_stamp` returns the position each branch just computed,
        # so the behaviour is unchanged (the absolute and delta-tracked cases).
        if "x" in self._pending or "y" in self._pending:
            prev = self._pos or (0, 0)
            self._pos = (
                self._pending.pop("x", prev[0]),
                self._pending.pop("y", prev[1]),
            )
            x, y = self._stamp()
            self._emit(
                RawEvent(kind="motion", timestamp=ts, x=x, y=y, screen=self._screen)
            )
        dx = self._pending.pop("dx", 0)
        dy = self._pending.pop("dy", 0)
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
