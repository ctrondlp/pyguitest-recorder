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
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

from ..stopkey import StopKey
from .base import CaptureUnavailable, RawEvent

__all__ = ["X11CaptureBackend", "available", "unavailable_reason"]

# X11 core event type codes, from the protocol. Named here so the parsing
# below does not depend on importing Xlib at module scope.
_KEY_PRESS = 2
_KEY_RELEASE = 3
_BUTTON_PRESS = 4
_BUTTON_RELEASE = 5
_MOTION = 6
_MAPPING_NOTIFY = 34
_MAPPING_MODIFIER = 0
_MAPPING_KEYBOARD = 1

# Core request opcodes this backend records, so a remap is seen in the same
# ordered stream as the key events around it -- see `_Keymap`.
_CHANGE_KEYBOARD_MAPPING = 100
_SET_MODIFIER_MAPPING = 118

# `reply.category` for record data: what the server sent, and what a client did.
_FROM_SERVER = 0
_FROM_CLIENT = 1

_SHIFT_MASK = 1 << 0
_LOCK_MASK = 1 << 1

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


def is_xwayland(display: str | None = None) -> bool | None:
    """Whether the X server being recorded is XWayland. None if it cannot be asked.

    Asked of the server rather than inferred from the environment, because the
    environment cannot tell the two cases apart that matter here. `DISPLAY` and
    `WAYLAND_DISPLAY` are both set for a recording of a Wayland session's own
    XWayland *and* for a recording of a private Xvfb started from a Wayland
    desktop -- the first is an XWayland recording and the second is not, and
    guessing wrong either loses the warning a reader needs or attaches it to a
    recording where it is false.

    XWayland advertises itself as an X extension named `XWAYLAND`, which is
    exactly the display-scoped question this needs to answer. Confirmed both
    ways on one machine: a GNOME Shell 51.rc session's `:0` lists it, and an
    Xvfb on `:77` does not.

    None rather than False where python-xlib is missing or the display will not
    open: "cannot tell" is not "no", and the caller falls back to the
    environment for that case rather than asserting either way.
    """
    try:
        from Xlib import display as _display
    except ImportError:
        return None
    try:
        connection = _display.Display(display) if display else _display.Display()
    except Exception:  # noqa: BLE001 - an unopenable display answers nothing
        return None
    try:
        return "XWAYLAND" in connection.list_extensions()
    except Exception:  # noqa: BLE001 - same: no answer, not a negative one
        return None
    finally:
        with contextlib.suppress(Exception):
            connection.close()


def available() -> bool:
    """Whether this machine can capture input."""
    return unavailable_reason() is None


class X11CaptureBackend:
    """Capture keyboard and pointer input through XRecord."""

    name = "xrecord"

    def __init__(
        self,
        display: str | None = None,
        screen: int = 0,
        stop_key: StopKey | None = None,
    ) -> None:
        """Prepare a capture backend against `display`, or $DISPLAY.

        `stop_key` is the recogniser used to end the stream where the user's own
        press completed the chord -- see `stop_pressed_at`. Without one, capture
        ends when `stop` is called, and only the consumer's view of the stop key
        decides where the recording does.
        """
        self.display_name = display
        self.screen = screen
        self.stop_pressed_at: float | None = None
        self._stop_key = stop_key
        self._finished = False
        self._control: Any = None
        self._pump: Any = None
        self._context: Any = None
        self._thread: threading.Thread | None = None
        self._queue: queue.Queue[Any] = queue.Queue()
        self._keysyms: dict[int, str] = {}
        self._keymap = _Keymap()
        self._group_mask: int | None = None
        # A remap a client requested, held until the server's MappingNotify
        # says it took effect; and the notify just applied, whose copies to
        # every other client follow it and must not be applied again.
        self._staged: tuple[Any, ...] | None = None
        self._applied: tuple[int, int, int] | None = None

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
            self._keymap.load(self._control)
            self._group_mask = _group_switch_mask(self._keymap, xlib)
            # Created on the pump connection because that is the one that will
            # enable it; see the module docstring.
            self._context = self._pump.record_create_context(
                0,
                [xlib["record"].AllClients],
                [
                    _record_range(
                        device_events=(_KEY_PRESS, _MOTION),
                        delivered_events=(_MAPPING_NOTIFY, _MAPPING_NOTIFY),
                    ),
                    _record_range(
                        core_requests=(
                            _CHANGE_KEYBOARD_MAPPING,
                            _CHANGE_KEYBOARD_MAPPING,
                        )
                    ),
                    _record_range(
                        core_requests=(_SET_MODIFIER_MAPPING, _SET_MODIFIER_MAPPING)
                    ),
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
        """Parse one record reply into raw events.

        Runs on the pump thread, and this is where the stop key is recognised as
        well as where it is consumed: the presses arrive here long before the
        consumer reaches them, and a recording that fell behind live input had
        no other way to end where the user asked -- see `stopkey`. Nothing is
        withheld until the chord completes, so a single Escape pressed to close
        a dialog is still handed on and recorded; the completing presses are
        handed on too, and the stream simply ends after them, which is what
        makes a recording end at the press however far behind consumption is.
        """
        if self._finished:
            return
        if not isinstance(reply.data, bytes) or not reply.data:
            return
        if reply.category == _FROM_CLIENT:
            # A byte-swapped client's remap is not decoded here: its
            # MappingNotify then finds nothing staged and is read back from
            # the server instead -- see `_mapping_notified`.
            if not reply.client_swapped:
                self._requested(reply.data)
            return
        if reply.category != _FROM_SERVER or reply.client_swapped:
            return
        if reply.data[0] < 2:
            return
        xlib = _import_xlib()
        data = reply.data
        while len(data):
            event, data = (
                xlib["rq"]
                .EventField(None)
                .parse_binary_value(data, self._pump.display, None, None)
            )
            if event.type == _MAPPING_NOTIFY:
                self._mapping_notified(event)
                continue
            # Anything else ends the run of copies one remap's notify came in.
            self._staged = self._applied = None
            raw = self._translate(event)
            if raw is None:
                continue
            self._queue.put(raw)
            if self._stop_key is not None and self._stop_key.feed(raw).stop:
                self._finish_at_stop(raw.timestamp)
                return

    def _finish_at_stop(self, timestamp: float) -> None:
        """End the stream where the stop key completed, and say when that was.

        Everything captured after this press belongs to the session, not to the
        recording, so it is never handed over: the alternative is a queue that
        grows for as long as someone keeps using the desktop after asking the
        recording to stop.
        """
        self.stop_pressed_at = timestamp
        self._finished = True
        self._queue.put(_SENTINEL)

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
        keysym = self._resolve_keysym(event.detail, event.state)
        return RawEvent(
            kind="key_press" if kind == _KEY_PRESS else "key_release",
            timestamp=now,
            x=event.root_x,
            y=event.root_y,
            screen=self.screen,
            keysym=self._keysyms.get(keysym, f"0x{keysym:x}"),
            text=_printable(keysym),
        )

    def _requested(self, data: bytes) -> None:
        """Stage a remap a client asked for, in stream order.

        Why the mapping is mirrored here rather than read from the server:
        a key event is decoded when the pump reaches it, which can be after
        the keycode it names has been remapped *again*. `xdotool type` does
        exactly that for text outside the base layout -- one spare keycode,
        remapped to each character in turn -- so a pump running behind
        would read every character through whichever mapping came last.
        The request carries the new keysyms and is recorded in the same
        ordered stream as the key events around it, so applying it here
        decodes each press through the mapping in force when it was made.

        Staged rather than applied: a request can fail (a bad value, a
        modifier change refused while a key is down), and only the
        MappingNotify the server broadcasts on success says it took effect.
        """
        self._staged = self._applied = None
        order: Any = sys.byteorder
        while len(data) >= 4:
            opcode = data[0]
            size = int.from_bytes(data[2:4], order) * 4
            if size < 4 or size > len(data):
                return  # a BIG-REQUESTS length or a torn record: not ours
            body, data = data[:size], data[size:]
            if opcode == _CHANGE_KEYBOARD_MAPPING and size >= 8:
                count, first, per = body[1], body[4], body[5]
                values = [
                    int.from_bytes(body[i : i + 4], order)
                    for i in range(8, 8 + count * per * 4, 4)
                ]
                rows = [values[i * per : (i + 1) * per] for i in range(count)]
                self._staged = (_MAPPING_KEYBOARD, first, count, rows)
            elif opcode == _SET_MODIFIER_MAPPING:
                per = body[1]
                codes = list(body[4 : 4 + 8 * per])
                modifiers = [codes[i * per : (i + 1) * per] for i in range(8)]
                self._staged = (_MAPPING_MODIFIER, 0, 0, modifiers)

    def _mapping_notified(self, event: Any) -> None:
        """Bring the mirrored mapping up to date with a MappingNotify.

        The notify is recorded as it is delivered, once per connected
        client, right after the request that caused it. A staged request it
        answers is applied from its own data; the copies after it are
        skipped. One with nothing staged came from somewhere the core
        requests do not show -- XKB, which is how `setxkbmap` and a desktop's
        layout switch remap -- and is read back from the server, which is
        the one case still exposed to a later remap overtaking it.
        """
        request = event.request
        if request not in (_MAPPING_KEYBOARD, _MAPPING_MODIFIER):
            return
        key = (
            (request, event.first_keycode, event.count)
            if request == _MAPPING_KEYBOARD
            else (request, 0, 0)
        )
        if key == self._applied:
            return
        staged, self._staged = self._staged, None
        if staged is not None and staged[:3] == key:
            if request == _MAPPING_KEYBOARD:
                self._keymap.apply_keyboard(staged[1], staged[3])
            else:
                self._keymap.apply_modifiers(staged[3])
            self._applied = key
        else:
            self._applied = None
            with contextlib.suppress(Exception):
                if request == _MAPPING_KEYBOARD:
                    self._keymap.load_keyboard(
                        self._control, event.first_keycode, event.count
                    )
                else:
                    self._keymap.load_modifiers(self._control)
        # Both mappings decide which modifier bit is AltGr: which modifier
        # Mode_switch/ISO_Level3_Shift is bound to, and which keycodes carry
        # those keysyms. Found once at start, it would otherwise go on
        # reading AltGr presses after such a remap as group 1.
        self._group_mask = _group_switch_mask(self._keymap, _import_xlib())

    def _resolve_keysym(self, keycode: int, state: int) -> int:
        """Pick the one keysym this keycode+modifier state actually produces.

        The old version only ever read index 0 or 1 -- group 1, unshifted or
        shifted -- so an AltGr-produced character (group 2, selected by
        whichever modifier the server binds to Mode_switch/ISO_Level3_Shift)
        was recorded as whatever group 1 happens to hold at that keycode, and
        CapsLock was ignored outright rather than treated as a latched Shift.
        Both are core-protocol default-interpretation rules
        (`XLookupString`'s documented behaviour with no per-key override,
        which this server-side capture has no way to ask for anyway), not a
        guess:

        - Group 2 is active when `_group_mask` (found once in `start()`) is
          set in `state`; group 1 otherwise. A layout with no third level
          binds no modifier to either keysym, `_group_mask` is None, and this
          always reads group 1 -- unchanged from before.
        - Within a group, Shift and Lock combine by this identity: Lock only
          matters for a keysym pair whose two members are the letter-case
          pair of each other (`_is_case_pair`) -- CapsLock has no effect on
          digits or punctuation -- and when it does, it acts as a second
          Shift: `effective_shift = shift ^ (lock and case_pair)`, which is
          why Shift+CapsLock on a letter types lowercase.
        """
        base_index = 2 if self._group_mask and (state & self._group_mask) else 0
        unshifted = self._keymap.keycode_to_keysym(keycode, base_index)
        shifted = self._keymap.keycode_to_keysym(keycode, base_index + 1)
        if shifted == 0:
            shifted = unshifted
        lock_shifts = bool(state & _LOCK_MASK) and _is_case_pair(unshifted, shifted)
        effective_shift = bool(state & _SHIFT_MASK) ^ lock_shifts
        keysym = shifted if effective_shift else unshifted
        if keysym == 0:
            keysym = self._keymap.keycode_to_keysym(keycode, 0)
        return int(keysym)

    def events(self) -> Iterator[RawEvent]:
        """Yield captured events until `stop` is called."""
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                return
            if isinstance(item, Exception):
                raise CaptureUnavailable(str(item)) from item
            yield item

    def drain(self) -> Iterator[RawEvent]:
        """Yield what the pump has already delivered, without waiting for more.

        The queue is read once, up to the size it had when this was called.
        Draining until it is empty would never return on a session that is
        still being used, and everything captured before this call is what a
        recording that ended mid-flight is owed: anything after it belongs to
        whatever happens next.

        The end-of-stream marker is put back rather than swallowed, so a reader
        that calls `events` afterwards still terminates. An error from the pump
        is skipped: `events` raises it, but here the run is already over and
        this exists to salvage what arrived before the failure -- raising would
        throw away the events queued behind it, which are the point.
        """
        for _ in range(self._queue.qsize()):
            try:
                item = self._queue.get_nowait()
            except queue.Empty:  # pragma: no cover - qsize raced a consumer
                return
            if item is _SENTINEL:
                self._queue.put(_SENTINEL)
                return
            if isinstance(item, Exception):
                continue
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


class _Keymap:
    """The keyboard and modifier mappings, as this recording has seen them.

    Kept here rather than read from a display connection's own cache: that
    cache is refreshed from the server *now*, and a key event has to be
    decoded by the mapping that was in force when it happened -- see
    `X11CaptureBackend._requested`. Answers the same two questions a display
    does, so `_group_switch_mask` reads either.
    """

    def __init__(
        self,
        keys: dict[int, list[int]] | None = None,
        modifiers: list[list[int]] | None = None,
    ) -> None:
        self.keys: dict[int, list[int]] = dict(keys or {})
        self.modifiers: list[list[int]] = modifiers or [[] for _ in range(8)]

    def keycode_to_keysym(self, keycode: int, index: int) -> int:
        row = self.keys.get(keycode, ())
        return row[index] if index < len(row) else 0

    def get_modifier_mapping(self) -> list[list[int]]:
        return self.modifiers

    def apply_keyboard(self, first: int, rows: list[list[int]]) -> None:
        for offset, row in enumerate(rows):
            self.keys[first + offset] = list(row)

    def apply_modifiers(self, modifiers: list[list[int]]) -> None:
        self.modifiers = [list(codes) for codes in modifiers]

    def load(self, display: Any) -> None:
        """Read both mappings from the server, whole, as capture starts."""
        info = display.display.info
        first = info.min_keycode
        self.load_keyboard(display, first, info.max_keycode - first + 1)
        self.load_modifiers(display)

    def load_keyboard(self, display: Any, first: int, count: int) -> None:
        self.apply_keyboard(first, display.get_keyboard_mapping(first, count))
        _discard_events(display)

    def load_modifiers(self, display: Any) -> None:
        self.apply_modifiers(display.get_modifier_mapping())
        _discard_events(display)


def _discard_events(display: Any) -> None:
    """Drop the events a round trip left queued on a connection nobody reads.

    Only the MappingNotify broadcast ever arrives there, and the record
    stream already carries it, in order; unread, the queue would only grow.
    """
    with contextlib.suppress(Exception):
        for _ in range(display.pending_events()):
            display.next_event()


def _record_range(**selected: Any) -> dict[str, Any]:
    """One XRecord range: nothing, except what `selected` names."""
    return {
        "core_requests": (0, 0),
        "core_replies": (0, 0),
        "ext_requests": (0, 0, 0, 0),
        "ext_replies": (0, 0, 0, 0),
        "delivered_events": (0, 0),
        "device_events": (0, 0),
        "errors": (0, 0),
        "client_started": False,
        "client_died": False,
        **selected,
    }


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


def _group_switch_mask(display: Any, xlib: dict[str, Any]) -> int | None:
    """The `event.state` bit that selects keyboard group 2 (AltGr), if any.

    Not assumed to be Mod5: `xmodmap` can bind Mode_switch/ISO_Level3_Shift to
    any of Mod1-Mod5, and a layout with no third level binds neither, in
    which case group 2 is simply unreachable and this returns None -- the
    caller then always reads group 1, unchanged from before this fix.
    Requires `_keysym_names` to have already loaded the "xkb" keysym group:
    `ISO_Level3_Shift` lives there, not in the sets `XK` loads by default
    (see that function's own docstring).
    """
    XK = xlib["XK"]
    targets = {
        keysym
        for keysym in (
            getattr(XK, "XK_Mode_switch", None),
            getattr(XK, "XK_ISO_Level3_Shift", None),
        )
        if keysym is not None
    }
    if not targets:
        return None
    with contextlib.suppress(Exception):
        for index, keycodes in enumerate(display.get_modifier_mapping()):
            if index < 3:  # Shift, Lock, Control are never the group switch
                continue
            for keycode in keycodes:
                if keycode and display.keycode_to_keysym(keycode, 0) in targets:
                    return 1 << index
    return None


def _is_case_pair(unshifted: int, shifted: int) -> bool:
    """Whether two keysyms are the same letter's lower- and upper-case forms.

    This is what CapsLock's own effect is conditioned on at the core-protocol
    level: it acts as a second Shift for a cased letter and does nothing for
    a digit or punctuation mark, which is not knowable from the keysym values
    alone without checking the pair actually is a case pair. Limited to the
    Latin-1 range `_printable` already covers -- this backend has no way to
    case-fold a keysym its own `keysym_to_string` cannot turn into text.
    """
    if unshifted == shifted:
        return False
    lower, upper = _printable(unshifted), _printable(shifted)
    return bool(lower and upper and lower != upper and lower.lower() == upper.lower())


_UNICODE_KEYSYM_BASE = 0x01000000
"""ICCCM's direct-Unicode keysym block: `keysym - this` is the codepoint.

`Xlib.XK.keysym_to_string` only ever covers Latin-1 (`keysym & 0xff00 == 0`)
plus a handful of named control keys -- nothing in this block, which is
exactly where a codepoint with no legacy X keysym of its own (all of CJK,
among others) is placed. Confirmed live: `xdotool type` of Chinese text
remaps an unused keycode to a keysym in this block for each character, and
without this branch every one of them fell through to `""`, so the
characters were captured as bare `key_press`/`key_release` events with no
text at all -- silently, the same shape of loss as the Windows `VK_PACKET`
bug this project already fixed (see status.md), on X11 instead.
"""


def _printable(keysym: int) -> str:
    """The character a keysym produces, or empty for a non-printing key."""
    if keysym >= _UNICODE_KEYSYM_BASE:
        codepoint = keysym - _UNICODE_KEYSYM_BASE
        # A lone UTF-16 surrogate (0xD800-0xDFFF) is a valid `chr()` argument
        # -- it does not raise -- but the string it produces cannot be
        # encoded as UTF-8, so it would otherwise crash a later write of the
        # recording rather than degrading to "" the way an out-of-range
        # codepoint already does below.
        if 0xD800 <= codepoint <= 0xDFFF:
            return ""
        try:
            return chr(codepoint)
        except ValueError:
            return ""
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
