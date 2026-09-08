"""Input capture over the X11 RECORD extension.

XRecord is the only interface on this stack that lets one client observe
another client's input, which is why it is the recorder's first backend and
why the recorder is X11-only. Under a Wayland session it reaches XWayland
clients and nothing else -- native Wayland windows are invisible here, by
design, and the recording says so rather than producing a script with silent
gaps in it.

The extension needs two display connections, and which one does what is not
interchangeable. `record_enable_context` blocks for the lifetime of the
capture, so it needs a connection of its own; that call runs on a worker
thread and pushes parsed events onto a queue, so `events()` can be consumed by
a UI without blocking it.

The context is created on *that* connection, not the other one. A record
context is addressed by an XID the creating client allocated from its own
range, so enabling it from a second connection is refused with BadContext --
which is what this backend did until it was first run against a real server.
The second connection exists to *disable* the context, which is the only way
to make the blocking call return.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any

from .base import CaptureUnavailable, RawEvent

__all__ = ["X11CaptureBackend", "available", "unavailable_reason"]

# X11 core event type codes, from the protocol. Named here so the parsing
# below does not depend on importing Xlib at module scope.
_KEY_PRESS = 2
_KEY_RELEASE = 3
_BUTTON_PRESS = 4
_BUTTON_RELEASE = 5
_MOTION = 6

_SHIFT_MASK = 1 << 0

# X11 reports the wheel as buttons. The signs here convert to pyguitest's
# convention on the way in, so nothing downstream has to know X11's: `dy`
# positive is up and `dx` positive is right, matching Session.scroll.
_WHEEL = {4: (0, 1), 5: (0, -1), 6: (-1, 0), 7: (1, 0)}

_SENTINEL = object()

_JOIN_TIMEOUT = 2.0
"""Seconds to wait for the pump thread to leave record_enable_context."""


def unavailable_reason() -> str | None:
    """Why this machine cannot capture, or None if it can.

    Three distinct failures look identical from the outside and need
    different fixes, so they are reported apart: the library is missing, the
    display will not open, or the server has no RECORD extension.
    """
    try:
        from Xlib import display as _display
    except ImportError:
        return (
            "python-xlib is not installed; "
            "pip install 'pyguitest-recorder[x11]' to enable X11 capture"
        )
    try:
        connection = _display.Display()
    except Exception as exc:  # noqa: BLE001 - any connection failure is fatal here
        return (
            f"cannot open the X display ({exc}). Input capture has no path on "
            "a native Wayland session -- reading another client's input is "
            "what compositors exist to prevent -- so the recorder needs Xorg, "
            "or XWayland with the target application running on it"
        )
    try:
        if not connection.query_extension("RECORD"):
            return (
                "the X server has no RECORD extension; on Xorg it ships in "
                "the xorg-x11-server-extra / x11-xserver-utils package"
            )
    finally:
        connection.close()
    return None


def available() -> bool:
    """Whether this machine can capture input."""
    return unavailable_reason() is None


class X11CaptureBackend:
    """Capture keyboard and pointer input through XRecord."""

    name = "xrecord"

    def __init__(self, display: str | None = None, screen: int = 0) -> None:
        """Prepare a capture backend against `display`, or $DISPLAY."""
        self.display_name = display
        self.screen = screen
        self._control: Any = None
        self._pump: Any = None
        self._context: Any = None
        self._thread: threading.Thread | None = None
        self._queue: queue.Queue[Any] = queue.Queue()
        self._keysyms: dict[int, str] = {}

    def start(self) -> None:
        """Open both connections, create the record context and begin pumping.

        Everything from the first connection onward runs under one try/except
        that tears down whatever was opened so far on any failure -- a display
        connection or the record context left dangling here has no other
        owner to close it. This used to only run `stop()` for the
        RECORD-extension-missing case; any other failure (the second
        connection refused, `record_create_context` rejected, the thread
        failing to start) leaked whatever had already been opened.
        """
        xlib = _import_xlib()
        try:
            self._control = _connect(xlib, self.display_name)
            self._pump = _connect(xlib, self.display_name)
            if not self._control.query_extension("RECORD"):
                raise CaptureUnavailable(
                    "the X server has no RECORD extension; on Xorg this is the "
                    "x11-xserver-utils / xorg-x11-server-extra package"
                )
            self._keysyms = _keysym_names(xlib)
            # Created on the pump connection because that is the one that will
            # enable it; see the module docstring.
            self._context = self._pump.record_create_context(
                0,
                [xlib["record"].AllClients],
                [
                    {
                        "core_requests": (0, 0),
                        "core_replies": (0, 0),
                        "ext_requests": (0, 0, 0, 0),
                        "ext_replies": (0, 0, 0, 0),
                        "delivered_events": (0, 0),
                        "device_events": (_KEY_PRESS, _MOTION),
                        "errors": (0, 0),
                        "client_started": False,
                        "client_died": False,
                    }
                ],
            )
            self._thread = threading.Thread(
                target=self._run, name="xrecord-capture", daemon=True
            )
            self._thread.start()
        except Exception:
            self.stop()
            raise

    def _run(self) -> None:
        """Pump the record context. Blocks until the context is disabled."""
        try:
            self._pump.record_enable_context(self._context, self._handle)
        except Exception as exc:  # noqa: BLE001 - surfaced through the queue
            self._queue.put(exc)
        finally:
            self._queue.put(_SENTINEL)

    def _handle(self, reply: Any) -> None:
        """Parse one record reply into raw events."""
        if reply.category != 0 or reply.client_swapped:
            return
        if not isinstance(reply.data, bytes) or reply.data[0] < 2:
            return
        xlib = _import_xlib()
        data = reply.data
        while len(data):
            event, data = (
                xlib["rq"]
                .EventField(None)
                .parse_binary_value(data, self._pump.display, None, None)
            )
            raw = self._translate(event)
            if raw is not None:
                self._queue.put(raw)

    def _translate(self, event: Any) -> RawEvent | None:
        """Convert one X event into a RawEvent, or None if it carries nothing."""
        now = _now()
        kind = event.type
        if kind == _MOTION:
            return RawEvent(
                kind="motion",
                timestamp=now,
                x=event.root_x,
                y=event.root_y,
                screen=self.screen,
            )
        if kind in (_BUTTON_PRESS, _BUTTON_RELEASE):
            return self._button(event, kind, now)
        if kind in (_KEY_PRESS, _KEY_RELEASE):
            return self._key(event, kind, now)
        return None

    def _button(self, event: Any, kind: int, now: float) -> RawEvent | None:
        """Convert a button event, folding the wheel buttons into scrolls."""
        wheel = _WHEEL.get(event.detail)
        if wheel is not None:
            if kind == _BUTTON_RELEASE:
                return None
            dx, dy = wheel
            return RawEvent(
                kind="scroll",
                timestamp=now,
                x=event.root_x,
                y=event.root_y,
                screen=self.screen,
                dx=dx,
                dy=dy,
            )
        return RawEvent(
            kind="button_press" if kind == _BUTTON_PRESS else "button_release",
            timestamp=now,
            x=event.root_x,
            y=event.root_y,
            screen=self.screen,
            button=event.detail,
        )

    def _key(self, event: Any, kind: int, now: float) -> RawEvent:
        """Convert a key event, resolving the keysym under the shift state."""
        shifted = bool(event.state & _SHIFT_MASK)
        keysym = self._control.keycode_to_keysym(event.detail, 1 if shifted else 0)
        if keysym == 0:
            keysym = self._control.keycode_to_keysym(event.detail, 0)
        return RawEvent(
            kind="key_press" if kind == _KEY_PRESS else "key_release",
            timestamp=now,
            x=event.root_x,
            y=event.root_y,
            screen=self.screen,
            keysym=self._keysyms.get(keysym, f"0x{keysym:x}"),
            text=_printable(keysym),
        )

    def events(self) -> Iterator[RawEvent]:
        """Yield captured events until `stop` is called."""
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                return
            if isinstance(item, Exception):
                raise CaptureUnavailable(str(item)) from item
            yield item

    def stop(self) -> None:
        """Disable the context, let the pump thread out, then close both ends.

        Order matters and the obvious order is wrong. `record_enable_context`
        blocks inside a socket read on the pump connection for the whole
        capture, so closing that connection first pulls the socket out from
        under a thread that is reading it. Disabling the context through the
        *control* connection is what makes the blocking call return; only once
        the thread has left it is the connection safe to close.
        """
        if self._context is not None and self._control is not None:
            try:
                self._control.record_disable_context(self._context)
                self._control.flush()
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            # Bounded: a server that never answers the disable must not hang
            # the process, and the daemon thread dies with it either way.
            thread.join(timeout=_JOIN_TIMEOUT)
        if self._context is not None and self._pump is not None:
            with contextlib.suppress(Exception):
                self._pump.record_free_context(self._context)
        for connection in (self._pump, self._control):
            if connection is not None:
                with contextlib.suppress(Exception):
                    connection.close()
        self._pump = self._control = self._context = None
        self._queue.put(_SENTINEL)


def _import_xlib() -> dict[str, Any]:
    """Import python-xlib, raising CaptureUnavailable with the fix if absent."""
    try:
        from Xlib import XK, display
        from Xlib.ext import record
        from Xlib.protocol import rq
    except ImportError as exc:
        raise CaptureUnavailable(
            "python-xlib is not installed; pip install 'pyguitest-recorder[x11]'"
        ) from exc
    return {"XK": XK, "display": display, "record": record, "rq": rq}


def _connect(xlib: dict[str, Any], name: str | None) -> Any:
    """Open one display connection."""
    try:
        return xlib["display"].Display(name) if name else xlib["display"].Display()
    except Exception as exc:  # noqa: BLE001
        raise CaptureUnavailable(f"cannot open display {name or '$DISPLAY'}") from exc


def _keysym_names(xlib: dict[str, Any]) -> dict[int, str]:
    """Build the keysym-to-name map, in the vocabulary press_key accepts.

    pyguitest's `press_key` takes X keysym names -- `Return`, `Control_L`,
    `F5` -- so resolving names here means key identity survives the whole
    pipeline without a second translation table.

    The extra keysym groups have to be asked for. python-xlib loads only the
    core Latin/miscellany sets into `XK` by default, so every multimedia key
    was nameless and fell through to its hex value: a laptop whose F-row
    sends media keys unless Fn is held recorded `gui.send_keys("^({0x1008ff12})")`
    for Ctrl+F1, which is XF86AudioMute and which `press_key` cannot resolve.
    Loading the groups first makes that `XF86_AudioMute`, which it can.
    """
    XK = xlib["XK"]
    for group in ("xf86", "xkb", "latin1", "miscellany"):
        # Absent on an older python-xlib, and a missing group is one keysym
        # family without names rather than a reason to fail the recording.
        with contextlib.suppress(Exception):
            XK.load_keysym_group(group)
    names: dict[int, str] = {}
    for attribute in dir(XK):
        if attribute.startswith("XK_"):
            names.setdefault(getattr(XK, attribute), attribute[3:])
    return names


def _printable(keysym: int) -> str:
    """The character a keysym produces, or empty for a non-printing key."""
    try:
        from Xlib import XK
    except ImportError:  # pragma: no cover - reached only without python-xlib
        return ""
    text = str(XK.keysym_to_string(keysym) or "")
    if len(text) != 1 or ord(text) < 32 or ord(text) == 127:
        return ""
    return text


def _now() -> float:
    """The capture clock. Monotonic, so a clock adjustment cannot reorder events."""
    return time.monotonic()
