"""Capture input on macOS through a `CGEventTap`.

XRecord's counterpart here is the event tap: `CGEventTapCreate` on the HID tap,
listen-only, whose callback is handed every event the window server routes. That
the two are the same idea is why this backend is a few hundred lines of translation
where the win32 one is a thousand: the tap reports `CGEvent`s rather than raw
`KEYBDINPUT` structures, and the interesting work is naming what arrived.

**The grant is Accessibility, and the tap is how that is checked.** Measured on
macOS 26.7: `CGEventTapCreate` returns a live tap for both the listen-only and the
active masks on a machine whose only grant is Accessibility, and returns `NULL`
without one -- TCC composes `kTCCServiceListenEvent` from Accessibility rather than
filing a row of its own, which is what `pyguitest`'s docs/validation.md records and
why `unavailable_reason` asks the window server directly rather than by preflight.
Input Monitoring is the pane a reader might go to for this and the one that says
`No Items` on a machine where the tap works.

**Keys are named with X11 keysyms, deliberately.** `Return`, `Control_L`,
`Super_L`, `bracketleft`, `apostrophe` -- the vocabulary `stopkey`, `normalize`'s
`MODIFIERS` table, the generator's `send_keys` strings and `Session.press_key` all
speak, because it is the vocabulary a recording made on Linux carries. It is *not*
the vocabulary `pyguitest`'s own `macquartz` uses for modifiers, which names keys
after the legend printed on them (`command`, `option`, `control`), so a *chord*
recorded here needs those spellings accepted there before it can be replayed. Keys
are unaffected -- `Return`, `Tab`, `Left` and the rest are matched case-insensitively
by that table already -- and `type_text` needs none of this, because it posts
Unicode.

Scrolling needs no translation at all: `kCGScrollWheelEventDeltaAxis1` is the same
number `pyguitest` feeds back into `CGEventCreateScrollWheelEvent`, so a recorded
scroll replays with the same sign.

`injected` is always False here, and that is the honest answer rather than an
oversight: a Mac's tap cannot tell a synthesised `CGEvent` from a real one --
`kCGEventSourceUserData` is 0 for both, and `pyguitest` posts through the same HID
system state a real event carries -- where `KBDLLHOOKSTRUCT` has a bit that says.
`win32` remains the one backend that can answer that question, and
`recorder._injected_note` will simply never fire on a Mac.

What has been run: the tap itself, on a granted macOS 26.7 VM, by a probe that
created one, fed it nothing and released it -- which is also what settles the grant
above. What has **not**: a recording session against a live desktop, and the Unicode
string `CGEventKeyboardGetUnicodeString` is supposed to hand back. `_text` folds
that to `""` on any failure rather than guessing, and a recording's typed text is
therefore the part most likely to need a second pass.
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
from .base import CaptureUnavailable, RawEvent, RawKind

__all__ = ["MacosCaptureBackend", "available", "unavailable_reason"]

_SENTINEL = object()
"""End-of-stream marker, as `x11`'s is: the queue carries events, errors and this."""

_JOIN_TIMEOUT = 2.0
"""How long `stop` waits for the tap thread, matching the x11 backend's bound."""

_QUARTZ_NEEDED = (
    "CGEventTapCreate",
    "CGEventTapEnable",
    "CGEventGetTimestamp",
    "CGEventGetLocation",
    "CGEventGetIntegerValueField",
    "CGEventGetFlags",
    "CGEventMaskBit",
    "CFMachPortCreateRunLoopSource",
    "CFRunLoopAddSource",
    "CFRunLoopRemoveSource",
    "CFRunLoopGetCurrent",
    "CFRunLoopRun",
    "CFRunLoopStop",
    "CFRunLoopWakeUp",
    "kCGHIDEventTap",
    "kCGHeadInsertEventTap",
    "kCGEventTapOptionListenOnly",
    "kCGEventLeftMouseDown",
    "kCGEventLeftMouseUp",
    "kCGEventRightMouseDown",
    "kCGEventRightMouseUp",
    "kCGEventOtherMouseDown",
    "kCGEventOtherMouseUp",
    "kCGEventMouseMoved",
    "kCGEventLeftMouseDragged",
    "kCGEventRightMouseDragged",
    "kCGEventOtherMouseDragged",
    "kCGEventScrollWheel",
    "kCGEventKeyDown",
    "kCGEventKeyUp",
    "kCGEventFlagsChanged",
    "kCGKeyboardEventKeycode",
    "kCGScrollWheelEventDeltaAxis1",
    "kCGScrollWheelEventDeltaAxis2",
)
"""The Quartz names this backend calls, checked rather than assumed.

A PyObjC present but older than these is a different problem from one that is
absent, and `unavailable_reason` says which it is.
"""

_TAPPED = (
    "kCGEventLeftMouseDown",
    "kCGEventLeftMouseUp",
    "kCGEventRightMouseDown",
    "kCGEventRightMouseUp",
    "kCGEventOtherMouseDown",
    "kCGEventOtherMouseUp",
    "kCGEventMouseMoved",
    "kCGEventLeftMouseDragged",
    "kCGEventRightMouseDragged",
    "kCGEventOtherMouseDragged",
    "kCGEventScrollWheel",
    "kCGEventKeyDown",
    "kCGEventKeyUp",
    "kCGEventFlagsChanged",
)
"""What the tap asks for, by name.

`CGEventMaskBit`-folded at `start`, so this reads as the list of event types the
callback may see rather than as a hexadecimal mask. Not `kCGEventMaskForAllEvents`:
the recorder has no use for tablet proximity, gesture or system-defined events, and
asking for them would put every one of them through a Python callback for nothing.
"""

_DISABLED = (
    "kCGEventTapDisabledByTimeout",
    "kCGEventTapDisabledByUserInput",
)
"""The two types a tap is handed when the window server turns it off.

Neither is a mask bit and neither translates, which is why they are not in `_TAPPED`.
Both are the same number's worth of trouble -- a disabled tap is not called again --
so `_re_enable` answers both. See its docstring for what was measured about when they
arrive, which is the reason it is guarded rather than unconditional.
"""

_MOUSE_KINDS: dict[str, tuple[RawKind, int]] = {
    "kCGEventLeftMouseDown": ("button_press", 1),
    "kCGEventLeftMouseUp": ("button_release", 1),
    "kCGEventRightMouseDown": ("button_press", 3),
    "kCGEventRightMouseUp": ("button_release", 3),
    "kCGEventOtherMouseDown": ("button_press", 2),
    "kCGEventOtherMouseUp": ("button_release", 2),
    "kCGEventMouseMoved": ("motion", 0),
    "kCGEventLeftMouseDragged": ("motion", 0),
    "kCGEventRightMouseDragged": ("motion", 0),
    "kCGEventOtherMouseDragged": ("motion", 0),
}
"""Event type name -> the `RawKind` and X11 button number it means.

X11's numbering, which this interface and `pyguitest` both use: 1 primary, 2
middle, 3 secondary. `CGEvent` calls the middle one `Other` in its event types and
`Center` in its button constants, which is why the numbers are written down rather
than computed from the type.
"""

# -- the vocabulary ---------------------------------------------------------
#
# macOS virtual keycodes -> the X11 keysym name of the key printed there, which is
# what the rest of this recorder and `Session.press_key` call it. The keycodes are
# `events.h`'s; the names are X11 `keysymdef.h`'s spellings.
#
# Derived from `pyguitest.backends.macquartz._KEYCODES` -- the table `press_key`
# actually presses -- inverted and renamed, so that what a recording names is what a
# replay can press. `tests/test_macos.py` asserts that agreement rather than trusting
# this comment: every name here that `macquartz` also has must map to the same
# keycode there, and the letters and digits must be the names it uses.
#
# Where the two vocabularies differ, X11's spelling wins, and the cost of that is
# stated in the module docstring: modifiers are `Super_L`/`Alt_L`/`Control_L` here
# and `command`/`option`/`control` there.
_KEYCODES: dict[int, str] = {
    # letters, in keycode order rather than alphabetically: this is a table read
    # against `events.h`, not a dictionary searched by name.
    0: "a",
    1: "s",
    2: "d",
    3: "f",
    4: "h",
    5: "g",
    6: "z",
    7: "x",
    8: "c",
    9: "v",
    11: "b",
    12: "q",
    13: "w",
    14: "e",
    15: "r",
    16: "y",
    17: "t",
    31: "o",
    32: "u",
    34: "i",
    35: "p",
    37: "l",
    38: "j",
    40: "k",
    45: "n",
    46: "m",
    # digits
    18: "1",
    19: "2",
    20: "3",
    21: "4",
    23: "5",
    22: "6",
    26: "7",
    28: "8",
    25: "9",
    29: "0",
    # punctuation, in X11's names rather than the legends -- `bracketleft` where the
    # key reads `[`, `apostrophe` where it reads `'`. pyguitest's `resolve_char_key`
    # exists to bridge exactly this difference and says so.
    24: "equal",
    27: "minus",
    30: "bracketright",
    33: "bracketleft",
    39: "apostrophe",
    41: "semicolon",
    42: "backslash",
    43: "comma",
    44: "slash",
    47: "period",
    50: "grave",
    36: "Return",
    48: "Tab",
    49: "space",
    51: "BackSpace",
    53: "Escape",
    117: "Delete",
    # modifiers
    55: "Super_L",
    54: "Super_R",
    56: "Shift_L",
    60: "Shift_R",
    58: "Alt_L",
    61: "Alt_R",
    59: "Control_L",
    62: "Control_R",
    57: "Caps_Lock",
    63: "Function",
    # function keys
    122: "F1",
    120: "F2",
    99: "F3",
    118: "F4",
    96: "F5",
    97: "F6",
    98: "F7",
    100: "F8",
    101: "F9",
    109: "F10",
    103: "F11",
    111: "F12",
    105: "F13",
    107: "F14",
    113: "F15",
    106: "F16",
    64: "F17",
    79: "F18",
    80: "F19",
    90: "F20",
    # navigation
    114: "Help",
    115: "Home",
    119: "End",
    116: "Page_Up",
    121: "Page_Down",
    123: "Left",
    124: "Right",
    125: "Down",
    126: "Up",
}
"""Virtual keycode -> X11 keysym name, for every key `macquartz` can press."""

_MODIFIER_MASKS = {
    55: "kCGEventFlagMaskCommand",
    54: "kCGEventFlagMaskCommand",
    56: "kCGEventFlagMaskShift",
    60: "kCGEventFlagMaskShift",
    58: "kCGEventFlagMaskAlternate",
    61: "kCGEventFlagMaskAlternate",
    59: "kCGEventFlagMaskControl",
    62: "kCGEventFlagMaskControl",
    57: "kCGEventFlagMaskAlphaShift",
}
"""Modifier keycode -> the Quartz flag that says whether it is down.

`kCGEventFlagsChanged` is the one tapped type that does not say which way the key
went: it fires for press and release alike and reports only the new flag set, so the
bit for this keycode is tested against `CGEventGetFlags`. That is the documented way
to read a modifier transition, and it is why this is a second table rather than a
pair of keycode ranges.
"""

_REFUSED = (
    "the window server refused a listen-only event tap, so nothing can be captured: "
    "grant Accessibility under System Settings > Privacy & Security > Accessibility, "
    "to the application responsible for this process -- Terminal, your IDE, or the "
    "sshd identity for a session reached over SSH"
)
"""What to say when a tap cannot be created.

A module constant rather than a paragraph in two places, because the reason and the
refusal are the same sentence: `unavailable_reason` is what `available()` answers
from, and `start` is where it is raised.
"""


def _import_quartz() -> Any:
    """Quartz, or a `CaptureUnavailable` naming what is missing.

    `importlib`-style rather than a module-level import, for `pyguitest`'s reason:
    this module is imported on every platform so the recorder can ask whether it
    applies, and a Mac-only binding cannot be a hard dependency of that question. A
    binding that is present and too old to have the entry points is told apart from
    one that is absent, because they have different fixes.
    """
    try:
        import Quartz  # noqa: PLC0415 - deliberately late, see the docstring
    except ImportError as exc:
        raise CaptureUnavailable(
            "capturing on macOS needs PyObjC's Quartz; install it with "
            "pip install 'pyguitest[macos]'"
        ) from exc
    missing = [name for name in _QUARTZ_NEEDED if not hasattr(Quartz, name)]
    if missing:
        raise CaptureUnavailable(
            f"the installed PyObjC has no {', '.join(missing)}; it is older than a "
            "CGEventTap needs"
        )
    return Quartz


def _tap_mask(quartz: Any) -> int:
    """The event mask for `_TAPPED`, folded through `CGEventMaskBit`."""
    mask = 0
    for name in _TAPPED:
        mask |= quartz.CGEventMaskBit(getattr(quartz, name))
    return mask


def _run_loop_mode(quartz: Any) -> Any:
    """`kCFRunLoopCommonModes`, from whichever module this PyObjC publishes it in.

    Quartz carries the `kCG*` names and the CoreFoundation *functions* the tap needs,
    and `CoreFoundation` carries the mode -- so this asks both rather than assuming,
    and the fallback is a real one rather than a guess about where it lives.
    """
    mode = getattr(quartz, "kCFRunLoopCommonModes", None)
    if mode is not None:
        return mode
    from CoreFoundation import kCFRunLoopCommonModes  # noqa: PLC0415 - see docstring

    return kCFRunLoopCommonModes


def _probe_tap(quartz: Any) -> bool:
    """Whether a listen-only tap can be created, released immediately afterwards.

    This *is* the permission question, asked rather than inferred: measured on macOS
    26.7, `CGEventTapCreate` answers with a live tap when Accessibility is granted and
    with `NULL` when it is not. It is also the question with no false positive -- a
    preflight would only say which service is involved, and the measurement says the
    service that answers is Accessibility, composed into `kTCCServiceListenEvent`
    rather than filed in a pane of its own.
    """

    def ignore(*_args: Any) -> None:
        """A callback that does nothing, for the probe tap's lifetime."""

    try:
        tap = quartz.CGEventTapCreate(
            quartz.kCGHIDEventTap,
            quartz.kCGHeadInsertEventTap,
            quartz.kCGEventTapOptionListenOnly,
            quartz.CGEventMaskBit(quartz.kCGEventKeyDown),
            ignore,
            None,
        )
    except Exception:  # noqa: BLE001 - a tap that will not be created
        return False
    return tap is not None


def unavailable_reason() -> str | None:
    """Why this machine cannot capture through a tap, or None if it can.

    Four questions in the order that gives the most useful answer: the platform, the
    binding, the entry points, and whether a tap can actually be created. Only the
    last is about permission, and it is the one a reader can act on -- which is why
    the sentence it produces names the pane and what the row is filed against, since
    a TCC row is recorded against the *responsible* process rather than against an
    interpreter path.
    """
    if sys.platform != "darwin":
        return (
            f"the macos backend needs a native macOS process; this is {sys.platform!r}"
        )
    try:
        quartz = _import_quartz()
    except CaptureUnavailable as exc:
        return str(exc)
    return None if _probe_tap(quartz) else _REFUSED


def available() -> bool:
    """Whether this machine can capture input through an event tap."""
    return unavailable_reason() is None


def _typed(text: str) -> str:
    """`text` where it is something somebody typed, `""` where it is a key's own code.

    Three ranges are not text, each for a reason this was measured on:

    - C0 and DEL, which is what macOS hands back for Return (0x0d), Tab (0x09),
      BackSpace (0x08), forward delete (0x7f) and Escape (0x1b). `x11.py`'s own
      `_printable` refuses exactly these, so this is the rule the pipeline
      already had -- a Mac recording just was not following it, and the way it
      broke was silent: `CGEventKeyboardGetUnicodeString` for the arrows answers
      0x1c to 0x1f, for Home and End 0x01 and 0x04, for Page Up/Down 0x0b and
      0x0c, and 0x10 for every one of F5, F6, F10 and F12 -- so a recording of
      them came out as a `gui.type_text` call carrying a literal control
      character, which uinput raises on and `macquartz` would post as itself.
    - U+E000-U+F8FF, where AppKit keeps its function-key characters --
      `NSUpArrowFunctionKey` is U+F700. The low byte is what this binding
      actually answered with live, and that is not a promise, so the range is
      refused whether the whole code point or its low byte arrives.
    - The UTF-16 surrogates, which is what an astral character arrives as. They
      cannot be encoded, so a recording holding one could not be written at all --
      the failure `x11.py`'s own comment already describes for the same reason.
    """
    for char in text:
        code = ord(char)
        if code < 32 or code == 127 or 0xD800 <= code <= 0xF8FF:
            return ""
    return text


def _now() -> float:
    """The recorder's clock, as `x11` reads it.

    `time.monotonic` and `CGEventGetTimestamp` are the same clock on a Mac -- both
    mach absolute time -- so a raw event's timestamp and one from here are directly
    comparable, which is what the normalizer's interval arithmetic needs.
    """
    return time.monotonic()


class MacosCaptureBackend:
    """Capture keyboard and pointer input through a `CGEventTap`.

    Structured as `X11CaptureBackend` is, because the recorder's contract is the hard
    part and the platform is the easy one: a thread owns the source, a queue carries
    events and errors out of it, and `stop` unwinds both in an order that lets the
    thread leave its blocking call.
    """

    name = "macos"

    def __init__(
        self,
        display: str | None = None,
        screen: int = 0,
        stop_key: StopKey | None = None,
    ) -> None:
        """Prepare a capture backend for this machine's one display space.

        `display` is accepted and unused: a Mac has no per-display capture target, and
        a tap watches the whole session. It stays in the signature because
        `choose_backend` builds every backend the same way, and a caller who set
        `display` in their config should get a working backend rather than a
        `TypeError` from this one.

        `stop_key` is the recogniser used to end the stream where the user's own press
        completed the chord -- see `stop_pressed_at`. Without one, capture ends when
        `stop` is called and only the consumer's view of the stop key decides where the
        recording does.
        """
        self.display_name = display
        self.screen = screen
        self.stop_pressed_at: float | None = None
        self._stop_key = stop_key
        self._finished = False
        self._q: Any = None
        self._tap: Any = None
        self._source: Any = None
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._queue: queue.Queue[Any] = queue.Queue()
        self._kinds: dict[int, str] = {}
        self._disabled: set[int] = set()

    def start(self) -> None:
        """Create the tap, then start the thread that owns its run loop.

        The tap is created here rather than on the thread so that a refusal is raised
        at the caller: `start` is where `CaptureUnavailable` belongs, and a tap that
        cannot be made is the ordinary way this fails. The run loop *source* is created
        on the thread instead, because a source belongs to the run loop it is added to
        and `CFRunLoopGetCurrent` means nothing anywhere else -- `x11` splits its two
        connections the same way for the same reason.

        Blocks until `_run` has stamped `self._loop`/`self._source` (or failed trying
        to), win32's own `_ready.wait()` pattern for the same reason: `stop()` reads
        both without a lock, and a `stop()` landing between this returning and the
        thread reaching that line would find a loop it does not know how to stop --
        disabling the tap, but leaving `CFRunLoopRun` with nothing to end it and the
        thread parked in it until the process exits.
        """
        self._q = _import_quartz()
        if not _probe_tap(self._q):
            raise CaptureUnavailable(_REFUSED)
        self._kinds = {getattr(self._q, name): name for name in _TAPPED}
        self._disabled = {getattr(self._q, name) for name in _DISABLED}
        self._tap = self._q.CGEventTapCreate(
            self._q.kCGHIDEventTap,
            self._q.kCGHeadInsertEventTap,
            self._q.kCGEventTapOptionListenOnly,
            _tap_mask(self._q),
            self._callback,
            None,
        )
        if self._tap is None:
            raise CaptureUnavailable(_REFUSED)
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="cgevent-tap-capture", daemon=True
        )
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        """Own a run loop, hang the tap on it, and pump until `stop` ends it."""
        try:
            quartz = self._q
            try:
                source = quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
                self._source = source
                loop = quartz.CFRunLoopGetCurrent()
                self._loop = loop
                quartz.CFRunLoopAddSource(loop, source, _run_loop_mode(quartz))
            finally:
                # Set once the loop is known to `self._loop` -- or, on the
                # exception path, once it is clear no loop is coming -- rather
                # than only after `CFRunLoopRun` returns: `start()` is waiting
                # on this, not on the whole capture.
                self._ready.set()
            quartz.CFRunLoopRun()
        except Exception as exc:  # noqa: BLE001 - surfaced through the queue
            self._queue.put(exc)
        finally:
            self._queue.put(_SENTINEL)

    def _callback(self, proxy: Any, kind: int, event: Any, user_info: Any) -> Any:
        """One tapped event: translate it, queue it, and keep the loop running.

        Returning the event is what tells the window server to carry on with it. A
        listen-only tap's return value is documented as ignored, and returning it anyway
        is what every example does and costs nothing -- the alternative, a callback
        returning None, is what *swallows* an event under a mask that included active
        types.

        An event that cannot be translated must not end the run: one unrecognised
        `CGEvent` is no reason to lose the rest of a recording. The failure is queued,
        where `events` raises it and `drain` skips it, exactly as `x11` treats an error
        from its pump.
        """
        if self._finished:
            return event
        if kind in self._disabled:
            self._re_enable()
            return event
        try:
            raw = self._translate(kind, event)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            self._queue.put(exc)
            return event
        if raw is None:
            return event
        self._queue.put(raw)
        if self._stop_key is not None and self._stop_key.feed(raw).stop:
            self._finish_at_stop(raw.timestamp)
        return event

    def _re_enable(self) -> None:
        """Put back a tap the window server turned off, unless the run is ending.

        Measured on macOS 26.7 over five live runs against a real tap: the
        notification this answers arrives once, after `stop()` has asked the window
        server to disable the tap, and in the run built to catch a mid-recording one
        all 48 posted events had already been delivered -- including across a
        three-second gap between two bursts. So the ordinary case needs nothing, and
        re-enabling blindly would make live again a tap that is deliberately going
        away. The guard is what that buys: teardown untouched, while a tap turned off
        *during* a recording comes back instead of leaving a recording that captures
        nothing from then on, with no error and nothing in the log.

        What could turn it off mid-recording is the timeout and secure input. The
        timeout is the one that cannot happen here: it is charged against a callback
        the window server is waiting on, and a listen-only tap is never waited on --
        a 1.5 s callback measured no disable. Secure input is unmeasured, and a tap
        that has been switched off for it answers `CGEventTapEnable(..., True)`
        anyway, which is the documented recovery and all this can do about it.
        """
        tap = self._tap
        if tap is None or self._finished:
            return
        with contextlib.suppress(Exception):
            self._q.CGEventTapEnable(tap, True)

    def _translate(self, kind: int, event: Any) -> RawEvent | None:
        """One tapped `CGEvent` in, or None when it says nothing.

        `kind` is matched through the same names the mask was built from, so the
        watching and the translating cannot disagree about what is being watched.
        """
        name = self._kinds.get(kind)
        if name is None:
            return None
        now = self._timestamp(event)
        x, y = self._location(event)
        if name in _MOUSE_KINDS:
            raw_kind, button = _MOUSE_KINDS[name]
            return RawEvent(
                kind=raw_kind,
                timestamp=now,
                x=x,
                y=y,
                screen=self.screen,
                button=button,
            )
        if name == "kCGEventScrollWheel":
            return RawEvent(
                kind="scroll",
                timestamp=now,
                x=x,
                y=y,
                screen=self.screen,
                dx=int(self._field(event, "kCGScrollWheelEventDeltaAxis2")),
                dy=int(self._field(event, "kCGScrollWheelEventDeltaAxis1")),
            )
        if name in ("kCGEventKeyDown", "kCGEventKeyUp"):
            return self._key(event, now, x, y, down=name == "kCGEventKeyDown")
        return self._flags_changed(event, now, x, y)

    def _key(self, event: Any, now: float, x: int, y: int, *, down: bool) -> RawEvent:
        """A key press or release, named by its keysym where the key is known.

        `keysym` is empty for a virtual keycode this table does not name -- a media key,
        a key only a JIS layout has -- rather than invented: nothing observed is dropped
        here, and what an unnamed key means is the normalizer's call.
        """
        code = int(self._field(event, "kCGKeyboardEventKeycode"))
        return RawEvent(
            kind="key_press" if down else "key_release",
            timestamp=now,
            x=x,
            y=y,
            screen=self.screen,
            keysym=_KEYCODES.get(code, ""),
            text=self._text(event) if down else "",
        )

    def _flags_changed(self, event: Any, now: float, x: int, y: int) -> RawEvent | None:
        """A modifier press or release: only the flag set can tell them apart.

        None for a keycode with no flag of its own: `Caps_Lock` has one and every other
        key does not, so a `FlagsChanged` outside `_MODIFIER_MASKS` is an event this
        backend has no reading of.
        """
        code = int(self._field(event, "kCGKeyboardEventKeycode"))
        mask_name = _MODIFIER_MASKS.get(code)
        if mask_name is None:
            return None
        down = bool(int(self._flags(event)) & int(getattr(self._q, mask_name)))
        return RawEvent(
            kind="key_press" if down else "key_release",
            timestamp=now,
            x=x,
            y=y,
            screen=self.screen,
            keysym=_KEYCODES.get(code, ""),
        )

    def _field(self, event: Any, name: str) -> int:
        """One integer field of an event, by the name PyObjC publishes it under.

        Coerced, because a PyObjC call's static type is `Any`: mypy cannot know what
        a binding hands back, and the return type here is a promise callers rely on.
        """
        return int(self._q.CGEventGetIntegerValueField(event, getattr(self._q, name)))

    def _flags(self, event: Any) -> int:
        """The event's modifier flag set."""
        return int(self._q.CGEventGetFlags(event))

    def _timestamp(self, event: Any) -> float:
        """When the window server stamped this event, in seconds.

        `CGEventGetTimestamp` is mach absolute time in nanoseconds, and
        `time.monotonic()` reads that same clock on a Mac, so dividing by `1e9` puts the
        two on one timeline with no offset to guess at.
        """
        return float(self._q.CGEventGetTimestamp(event)) / 1e9

    def _location(self, event: Any) -> tuple[int, int]:
        """The pointer's position when the event was stamped, as two ints.

        Global display points -- the space `Session.pointer_position` answers in and
        `Session.move_mouse` takes, so a recording's coordinates replay where they were
        taken, and the same space a window hit test is asked about.
        """
        point = self._q.CGEventGetLocation(event)
        return int(point.x), int(point.y)

    def _text(self, event: Any) -> str:
        """The character this keystroke produced, or "" where it made none.

        `CGEventKeyboardGetUnicodeString`, and deliberately *not* one of the names
        `_QUARTZ_NEEDED` requires: its PyObjC marshalling -- a buffer and a length by
        pointer -- is the one call in this backend that could not be faked, so a
        binding that answers something unexpected costs a typed character rather than
        the whole recording. It has since been run against a live tap, which is how
        the ranges `_typed` refuses were measured; the try/except stays because what
        is measured is PyObjC 12.2.2 on macOS 26.7, not a contract. An empty answer
        leaves the normalizer the key name, which is what a chord needs anyway.
        """
        try:
            length, buffer = self._q.CGEventKeyboardGetUnicodeString(
                event, 8, None, None
            )
        except Exception:  # noqa: BLE001 - a binding that will not answer
            return ""
        if not length or buffer is None:
            return ""
        try:
            return _typed(str(buffer[:length]))
        except Exception:  # noqa: BLE001 - a buffer of another shape
            return ""

    def _finish_at_stop(self, timestamp: float) -> None:
        """End the stream where the stop key completed, and say when that was.

        Everything captured after this press belongs to the session rather than to the
        recording, so none of it is handed over: the alternative is a queue growing for
        as long as someone keeps using the desktop after asking the recording to stop.
        """
        self.stop_pressed_at = timestamp
        self._finished = True
        self._queue.put(_SENTINEL)

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
        """Yield what the tap has already delivered, without waiting for more.

        Read once, up to the depth the queue had when this was called: draining until it
        is empty would never return on a session still being used, and everything
        captured before this call is what a run interrupted mid-flight is owed. The end
        marker is put back so a later `events` still terminates, and a queued error is
        skipped -- `events` raises it, and this exists to salvage what arrived ahead of
        it.
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
        """Stop the run loop, let the tap thread out, then drop the tap.

        Both halves are needed and the order matters. Disabling the tap stops events
        arriving; `CFRunLoopStop` plus a wake-up is what makes `CFRunLoopRun` return,
        and
        without the wake-up the loop can sit in a sleep it has no reason to leave --
        which is the difference between a recording that ends and a process that hangs
        on
        exit. The join is bounded, and the thread is a daemon either way.
        """
        tap, self._tap = self._tap, None
        if tap is not None and self._q is not None:
            with contextlib.suppress(Exception):
                self._q.CGEventTapEnable(tap, False)
        loop, self._loop = self._loop, None
        source, self._source = self._source, None
        if loop is not None and self._q is not None:
            with contextlib.suppress(Exception):
                self._q.CFRunLoopRemoveSource(loop, source, _run_loop_mode(self._q))
            with contextlib.suppress(Exception):
                self._q.CFRunLoopStop(loop)
            with contextlib.suppress(Exception):
                self._q.CFRunLoopWakeUp(loop)
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=_JOIN_TIMEOUT)
