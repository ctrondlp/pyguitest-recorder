"""The macOS capture path, driven from a machine that is not a Mac.

This backend needs a live window server to *capture*, which no CI machine here has.
What it does not need one for is everything else: translating a `CGEvent` into a
`RawEvent`, naming a key by its keysym, reading a modifier transition out of a flag
set, and coming apart cleanly. Those are exercised against a stand-in `Quartz` in
`sys.modules` -- the shape `tests/test_x11.py` uses for `Xlib` -- which is the
difference between this file being written and being known to work.

The table test is the one that reaches across repositories: it checks this backend's
keycodes against `pyguitest.backends.macquartz._KEYCODES`, because a recording's key
names are only useful if the library that replays them can press them.
"""

from __future__ import annotations

import sys
import types

import pytest

from pyguitest_recorder import platforms, recorder
from pyguitest_recorder.backends import macos as macos_module
from pyguitest_recorder.backends.base import CaptureBackend, CaptureUnavailable
from pyguitest_recorder.backends.macos import (
    _KEYCODES,
    _MODIFIER_MASKS,
    _MOUSE_KINDS,
    _QUARTZ_NEEDED,
    _TAPPED,
    MacosCaptureBackend,
    _tap_mask,
)
from pyguitest_recorder.config.settings import Settings
from pyguitest_recorder.stopkey import StopKey


def _macquartz():
    """The installed `macquartz`, or a skip when the binding is absent.

    Also asserts the one attribute the cross-repository checks below read.
    `_key_name` is what 0.14.0 added -- the step `_keycode`, `press_key` and
    `release_key` all resolve a name through -- and it sits below the floor
    `pyproject.toml` declares, so a machine this far behind should be told
    that rather than get an `AttributeError` out of a test about a key table
    whose release it never installed.
    """
    macquartz = pytest.importorskip("pyguitest.backends.macquartz")
    assert hasattr(macquartz, "_key_name"), (
        "no `_key_name` on the installed pyguitest: it arrived in 0.14.0, and "
        "pyproject.toml's floor is newer than that"
    )
    return macquartz


class Constant:
    """One fake `kCG*`/`kCF*` name, remembered by name for readable failures."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


class FakeEvent:
    """One fake `CGEvent`: whatever fields a test set, and nothing else."""

    def __init__(
        self,
        *,
        keycode: int | None = None,
        point: tuple[float, float] = (10.0, 20.0),
        stamp: float = 1_000_000_000.0,
        flags: int = 0,
        fields: dict[str, int] | None = None,
        text: str | None = None,
    ) -> None:
        self.keycode = keycode
        self.point = types.SimpleNamespace(x=point[0], y=point[1])
        self.stamp = stamp
        self.flags = flags
        self.fields = dict(fields or {})
        self.text = text


_MISSING = object()
"""Default for `FakeQuartz`'s tap, so `tap=None` can mean a *refused* tap.

The distinction matters: `None` from `CGEventTapCreate` is the permission answer, and a
fixture that quietly substituted a created tap for it would make every refusal test pass
by testing nothing.
"""


class FakeQuartz:
    """Stands in for PyObjC's `Quartz`, with the names `macos.py` calls.

    `__getattr__` answers any `kCG*`/`kCF*` name with a stable `Constant`, so the
    fake needs no table of constants while the backend still reads its names off the
    module -- the trick `pyguitest`'s own `tests/test_macos.py` plays with the same
    names. Everything the backend *calls* is defined explicitly, and one test deletes
    each name in turn to prove `_QUARTZ_NEEDED` is what gates `available()`.
    """

    def __init__(self, tap: object = _MISSING) -> None:
        self.constants: dict[str, Constant] = {}
        self.flags: dict[str, int] = {}
        self.made: list[tuple] = []
        self.removed: list[tuple] = []
        self.stopped: list[object] = []
        self.enabled: list[tuple] = []
        self.added: list[tuple] = []
        self.ran = 0
        self._tap = object() if tap is _MISSING else tap

    def __getattr__(self, name: str) -> object:
        # The flag masks are integers, because the backend and-s one against a flag set
        # -- `int(Constant(...))` would raise, and the real binding publishes them as
        # numbers. Every other `kCG*`/`kCF*` name is a stable opaque constant.
        if name.startswith("kCGEventFlagMask"):
            if name not in self.flags:
                self.flags[name] = 1 << (len(self.flags) + 8)
            return self.flags[name]
        if name.startswith("kCG") or name.startswith("kCF"):
            if name not in self.constants:
                self.constants[name] = Constant(name)
            return self.constants[name]
        raise AttributeError(name)

    def constant(self, name: str) -> Constant:
        """The fake's constant for `name`, created on first use."""
        return self.constants.setdefault(name, Constant(name))

    def CGEventMaskBit(self, kind: object) -> int:
        """A distinct integer per event kind, as the real one is a distinct bit."""
        return 1 << (abs(hash(getattr(kind, "name", str(kind)))) % 24)

    def CGEventTapCreate(self, *args: object) -> object | None:
        self.made.append(args)
        return self._tap

    def CGEventTapEnable(self, tap: object, enabled: bool) -> None:
        self.enabled.append((tap, enabled))

    def CFMachPortCreateRunLoopSource(self, allocator: object, tap: object, order: int):
        return Constant("run-loop-source")

    def CFRunLoopGetCurrent(self) -> Constant:
        return Constant("run-loop")

    def CFRunLoopAddSource(self, loop: object, source: object, mode: object) -> None:
        self.added.append((loop, source, mode))

    def CFRunLoopRemoveSource(self, loop: object, source: object, mode: object) -> None:
        self.removed.append((loop, source, mode))

    def CFRunLoopRun(self) -> None:
        """Return at once: the backend's thread ends, as a stopped loop's would."""
        self.ran += 1

    def CFRunLoopStop(self, loop: object) -> None:
        self.stopped.append(loop)

    def CFRunLoopWakeUp(self, loop: object) -> None:
        self.stopped.append(("wake", loop))

    def CGEventGetTimestamp(self, event: FakeEvent) -> float:
        return event.stamp

    def CGEventGetLocation(self, event: FakeEvent) -> object:
        return event.point

    def CGEventGetFlags(self, event: FakeEvent) -> int:
        return event.flags

    def CGEventGetIntegerValueField(self, event: FakeEvent, field: Constant) -> int:
        if field.name == "kCGKeyboardEventKeycode":
            return -1 if event.keycode is None else event.keycode
        return event.fields.get(field.name, 0)

    def CGEventKeyboardGetUnicodeString(
        self, event: FakeEvent, length: int, actual: object, buffer: object
    ) -> tuple[int, str | None]:
        if event.text is None:
            return (0, None)
        return (len(event.text), event.text)


def install(monkeypatch: pytest.MonkeyPatch, quartz: FakeQuartz) -> None:
    """Make `quartz` the module `macos.py` will import, on this platform."""
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr(macos_module.sys, "platform", "darwin")


def started(monkeypatch: pytest.MonkeyPatch, quartz: FakeQuartz, **kwargs: object):
    """A started backend over `quartz`, with its queue empty and held up to date.

    The fake's `CFRunLoopRun` returns immediately, so the capture thread finishes on
    its own and leaves an end marker in the queue -- an artefact of a loop that never
    ran. Joining the thread and draining that marker leaves a test with a queue it
    owns, and `_kinds` is populated by `start` before the thread is even made, which is
    all a translation test needs.
    """
    install(monkeypatch, quartz)
    backend = MacosCaptureBackend(**kwargs)
    backend.start()
    if backend._thread is not None:
        backend._thread.join(timeout=2.0)
    while not backend._queue.empty():
        backend._queue.get_nowait()
    return backend


class TestTheKeyTable:
    """The vocabulary: what a recording names, against what a replay can press."""

    def test_every_key_pyguitest_can_press_has_a_keycode_here(self):
        # Derived from the other side's table, so a key added there and not here is a
        # key that records as nothing. `macquartz` is the table `press_key` reads.
        macquartz = _macquartz()
        theirs = macquartz._KEYCODES
        ours = set(_KEYCODES.values())
        shared = {name for name in theirs if name in {n.lower() for n in ours}}
        self_check = {name for name in theirs if name.isalpha() and len(name) == 1}
        assert self_check <= {name for name in ours if len(name) == 1}
        assert len(shared) > 50, "the two tables should overlap on the printable keys"

    def test_every_name_this_backend_emits_resolves_through_pyguitest(self):
        # Both directions of the vocabulary at once, and the reason this file reaches
        # across repositories: what a recording names is what a replay presses.
        # `_key_name` is the step `_keycode`, `press_key` and `release_key` all resolve
        # a name through, so a name that does not come out of it as this keycode is a
        # key this backend records and a replay cannot press -- which fifteen of them
        # were, every one of them a name with two words in the X11 spelling (`Super_L`,
        # `Page_Up`, `BackSpace`) or a legend label this table spells differently
        # (`command` for `Super_L`, `quote` for `apostrophe`).
        macquartz = _macquartz()
        for code, name in _KEYCODES.items():
            assert macquartz._KEYCODES.get(macquartz._key_name(name)) == code, name

    def test_a_recorded_modifier_is_held_as_the_key_it_names(self):
        # The half a keycode test cannot see. The flags on every event posted after a
        # modifier goes down are read off pyguitest's held set, which is keyed by *its*
        # vocabulary, so a recorded `Super_L` that resolved its keycode but not its held
        # entry posted the Command key and then sent the next key unmodified -- a chord
        # that reads as a shortcut and acts as a plain key, which is what a recording of
        # Command+S was doing.
        macquartz = _macquartz()
        for code in _MODIFIER_MASKS:
            name = _KEYCODES[code]
            assert macquartz._key_name(name) in macquartz._MODIFIER_FLAGS, name

    def test_the_one_collision_resolves_to_the_forward_delete_key(self):
        # The pair the two vocabularies disagree on rather than merely spell
        # differently. X11 names the forward-delete key `Delete` (117) and the backspace
        # key `BackSpace` (51); pyguitest's macOS table names the *backspace* key
        # `delete`, after the legend a Mac keyboard prints on it, and calls 117
        # `forwarddelete`. Lowercased, `Delete` and `delete` are one string, so this is
        # the one name no case-insensitive alias table can hold: the resolution has to
        # be case-sensitive, with X11's spelling winning. Until it was, a recorded
        # forward delete replayed as a backspace -- wrong, silent, and the failure mode
        # pyguitest's ADR 004 §7 refuses.
        macquartz = _macquartz()
        assert macquartz._key_name("Delete") == "forwarddelete"
        assert macquartz._KEYCODES["forwarddelete"] == 117
        assert _KEYCODES[117] == "Delete"
        # And the legend keeps meaning what it always meant: the backspace key, which
        # is what `{BAC}`, `{BS}` and `{BKS}` resolve to as well.
        assert macquartz._key_name("delete") == "delete"
        assert macquartz._key_name("BackSpace") == "delete"
        assert macquartz._KEYCODES["delete"] == 51
        assert _KEYCODES[51] == "BackSpace"

    def test_no_keycode_is_named_twice(self):
        # Two keycodes sharing a name would make the reverse lookup ambiguous, which is
        # how a right-hand modifier would silently record as its left-hand pair.
        names = list(_KEYCODES.values())
        assert len(names) == len(set(names))

    def test_modifier_masks_cover_every_modifier_keycode(self):
        # `_flags_changed` reads the flag set to tell a press from a release, so a
        # modifier without a mask is a key that records as nothing at all.
        modifiers = {
            code
            for code, name in _KEYCODES.items()
            if name.endswith(("_L", "_R"))
            and name.split("_")[0] in {"Super", "Alt", "Control", "Shift"}
        }
        assert modifiers <= set(_MODIFIER_MASKS)


class TestTheMask:
    """What the tap asks the window server for."""

    def test_the_mask_covers_every_name_in_the_tapped_list(self, monkeypatch):
        quartz = FakeQuartz()
        install(monkeypatch, quartz)
        expected = 0
        for name in _TAPPED:
            expected |= quartz.CGEventMaskBit(quartz.constant(name))
        assert _tap_mask(quartz) == expected
        assert expected != 0

    def test_mouse_kinds_use_x11s_button_numbers(self):
        # 1 primary, 2 middle, 3 secondary -- the numbering this interface and
        # pyguitest both use, which is why the middle one is not `Other` here.
        assert _MOUSE_KINDS["kCGEventLeftMouseDown"] == ("button_press", 1)
        assert _MOUSE_KINDS["kCGEventOtherMouseDown"] == ("button_press", 2)
        assert _MOUSE_KINDS["kCGEventRightMouseDown"] == ("button_press", 3)
        assert _MOUSE_KINDS["kCGEventLeftMouseUp"] == ("button_release", 1)
        assert _MOUSE_KINDS["kCGEventMouseMoved"] == ("motion", 0)
        assert _MOUSE_KINDS["kCGEventLeftMouseDragged"][0] == "motion"

    def test_a_dragged_mouse_is_motion_rather_than_a_button_event(self):
        # The drag types carry the button in their name only because the pointer
        # happens to be down; folding them into button events would turn every drag
        # into a click in the recording.
        for name in ("kCGEventLeftMouseDragged", "kCGEventRightMouseDragged"):
            assert _MOUSE_KINDS[name] == ("motion", 0)


def event_type(backend: MacosCaptureBackend, name: str) -> int:
    """The integer `backend` recorded for one event-type name.

    Read back off the backend rather than built here, which is also what proves the
    names were matched through the same table the mask was built from.
    """
    return next(key for key, value in backend._kinds.items() if value == name)


class TestTranslation:
    """One `CGEvent` in, one `RawEvent` out."""

    def test_a_mouse_press_carries_its_position_and_x11_button(self, monkeypatch):
        backend = started(monkeypatch, FakeQuartz())
        raw = backend._translate(
            event_type(backend, "kCGEventRightMouseDown"),
            FakeEvent(point=(300.0, 400.0), stamp=5_000_000_000.0),
        )
        assert raw.kind == "button_press"
        assert raw.button == 3
        assert (raw.x, raw.y) == (300, 400)

    def test_the_timestamp_is_the_events_own_nanoseconds_in_seconds(self, monkeypatch):
        # `CGEventGetTimestamp` is mach absolute time in nanoseconds, and
        # `time.monotonic` is that same clock, so the division is the whole conversion.
        backend = started(monkeypatch, FakeQuartz())
        raw = backend._translate(
            event_type(backend, "kCGEventMouseMoved"),
            FakeEvent(stamp=123_456_789_000.0),
        )
        assert raw.timestamp == pytest.approx(123.456789)

    def test_a_key_press_is_named_by_keysym_and_carries_its_text(self, monkeypatch):
        backend = started(monkeypatch, FakeQuartz())
        raw = backend._translate(
            event_type(backend, "kCGEventKeyDown"), FakeEvent(keycode=0, text="a")
        )
        assert raw.kind == "key_press"
        assert raw.keysym == "a"
        assert raw.text == "a"

    def test_a_special_key_carries_no_text(self, monkeypatch):
        """The keysym names it; the code macOS answers with is not a character typed.

        Measured on a granted macOS 26.7 VM: `CGEventKeyboardGetUnicodeString`
        answers 0x1c-0x1f for the four arrows, 0x01/0x04 for Home and End,
        0x0b/0x0c for Page Up and Page Down, the same 0x10 for every one of F5,
        F6, F10 and F12, and 0x0d, 0x09, 0x08, 0x7f, 0x1b for Return, Tab,
        backspace, forward delete and Escape. Taken as text -- which is what this
        backend did -- a recording of them came out as a `gui.type_text` call
        carrying a literal control character: a script that replays as nothing at
        all through uinput, which raises on the character, and as that control
        character through macquartz. `x11.py`'s `_printable` has always refused
        those ranges, so this is the rule the pipeline already had.
        """
        backend = started(monkeypatch, FakeQuartz())
        special = {
            123: "\x1c",  # Left
            124: "\x1d",  # Right
            125: "\x1e",  # Down
            126: "\x1f",  # Up
            115: "\x01",  # Home
            119: "\x04",  # End
            116: "\x0b",  # Page Up
            121: "\x0c",  # Page Down
            96: "\x10",  # F5, and F6, F10 and F12 all answer this too
            36: "\r",  # Return
            48: "\t",  # Tab
            51: "\x08",  # backspace
            117: "\x7f",  # forward delete
            53: "\x1b",  # Escape
        }
        for keycode, code in special.items():
            raw = backend._translate(
                event_type(backend, "kCGEventKeyDown"),
                FakeEvent(keycode=keycode, text=code),
            )
            assert raw.text == "", f"keycode {keycode} reported {code!r} as text"
            assert raw.keysym, "the keysym is what names the key"

    def test_a_function_keys_private_use_code_is_not_text_either(self, monkeypatch):
        # What arrives is the low byte of AppKit's own code point, measured -- but
        # that is not a promise, so the whole range is refused whether the binding
        # hands back `\x1c` for Left or `\uf702`.
        backend = started(monkeypatch, FakeQuartz())
        raw = backend._translate(
            event_type(backend, "kCGEventKeyDown"),
            FakeEvent(keycode=123, text="\uf702"),
        )
        assert raw.text == ""

    def test_a_key_release_carries_no_text(self, monkeypatch):
        # The character belongs to the press: a release that repeated it would make the
        # normalizer see every keystroke twice.
        backend = started(monkeypatch, FakeQuartz())
        raw = backend._translate(
            event_type(backend, "kCGEventKeyUp"), FakeEvent(keycode=36, text="\r")
        )
        assert raw.kind == "key_release"
        assert raw.keysym == "Return"
        assert raw.text == ""

    def test_an_unnamed_keycode_records_without_a_name(self, monkeypatch):
        # A media key, or one only some layouts have: the event is kept and unnamed
        # rather than dropped, and inventing a name would put a wrong key in a script.
        backend = started(monkeypatch, FakeQuartz())
        raw = backend._translate(
            event_type(backend, "kCGEventKeyDown"), FakeEvent(keycode=0x7F)
        )
        assert raw.keysym == ""

    def test_a_modifier_press_and_release_are_read_from_the_flag_set(self, monkeypatch):
        # `kCGEventFlagsChanged` reports only the new flags, so the mask for this
        # keycode is what says which way the key went.
        quartz = FakeQuartz()
        backend = started(monkeypatch, quartz)
        shift = quartz.kCGEventFlagMaskShift
        pressed = FakeEvent(keycode=56, flags=shift)
        released = FakeEvent(keycode=56, flags=0)
        changed = event_type(backend, "kCGEventFlagsChanged")
        assert backend._translate(changed, pressed).kind == "key_press"
        assert backend._translate(changed, pressed).keysym == "Shift_L"
        assert backend._translate(changed, released).kind == "key_release"

    def test_a_flags_changed_for_a_key_with_no_flag_is_ignored(self, monkeypatch):
        backend = started(monkeypatch, FakeQuartz())
        assert (
            backend._translate(
                event_type(backend, "kCGEventFlagsChanged"), FakeEvent(keycode=0)
            )
            is None
        )

    def test_a_scroll_carries_both_axes_in_pyguitests_order(self, monkeypatch):
        # Axis1 is dy and Axis2 is dx, passed straight through: `Session.scroll(dy=)`
        # feeds the same number back into `CGEventCreateScrollWheelEvent`, so a
        # recorded scroll replays with the same sign rather than an inverted one.
        quartz = FakeQuartz()
        backend = started(monkeypatch, quartz)
        raw = backend._translate(
            event_type(backend, "kCGEventScrollWheel"),
            FakeEvent(
                fields={
                    quartz.constant("kCGScrollWheelEventDeltaAxis1").name: 3,
                    quartz.constant("kCGScrollWheelEventDeltaAxis2").name: -1,
                }
            ),
        )
        assert (raw.dx, raw.dy) == (-1, 3)
        assert raw.kind == "scroll"

    def test_an_event_outside_the_mask_is_ignored(self, monkeypatch):
        backend = started(monkeypatch, FakeQuartz())
        assert backend._translate(-999, FakeEvent()) is None


class TestTheStream:
    """Starting, stopping and draining, which is the recorder's actual contract."""

    def test_it_satisfies_the_capture_protocol(self):
        # The protocol is runtime-checkable, so this is the same check `choose_backend`
        # and the recorder's loop rely on rather than a comment claiming conformance.
        assert isinstance(MacosCaptureBackend(), CaptureBackend)

    def test_start_creates_a_listen_only_tap_at_the_hid_tap(self, monkeypatch):
        quartz = FakeQuartz()
        started(monkeypatch, quartz)
        args = quartz.made[0]
        assert args[0] is quartz.kCGHIDEventTap
        assert args[1] is quartz.kCGHeadInsertEventTap
        assert args[2] is quartz.kCGEventTapOptionListenOnly
        assert isinstance(args[3], int) and args[3] != 0

    def test_start_hangs_a_source_on_the_threads_own_run_loop(self, monkeypatch):
        # The source belongs to the run loop it is added to, which is why it is created
        # inside the thread rather than in `start`.
        quartz = FakeQuartz()
        backend = started(monkeypatch, quartz)
        assert quartz.added and quartz.added[0][0] is backend._loop

    def test_a_refused_tap_is_a_capture_unavailable_naming_the_grant(self, monkeypatch):
        # `CGEventTapCreate` returning NULL is the permission answer, and the sentence
        # it produces is the one a reader can act on.
        quartz = FakeQuartz(tap=None)
        install(monkeypatch, quartz)
        with pytest.raises(CaptureUnavailable) as caught:
            MacosCaptureBackend().start()
        assert "Accessibility" in str(caught.value)
        assert "System Settings" in str(caught.value)

    def test_stop_stops_the_loop_and_wakes_it(self, monkeypatch):
        # Both are needed: the stop is what makes `CFRunLoopRun` return, and the wake-up
        # is what keeps a sleeping loop from never noticing.
        quartz = FakeQuartz()
        backend = started(monkeypatch, quartz)
        backend.stop()
        assert quartz.stopped
        assert quartz.enabled and quartz.enabled[0][1] is False

    def test_stop_is_safe_to_call_twice_and_before_start(self, monkeypatch):
        MacosCaptureBackend().stop()
        quartz = FakeQuartz()
        backend = started(monkeypatch, quartz)
        backend.stop()
        backend.stop()

    def test_a_disabled_tap_is_put_back_while_the_run_is_live(self, monkeypatch):
        # A disabled tap is not called again, so nothing else here would ever notice
        # it had happened: the recovery is one call, and the guard around it is the
        # whole design -- see the test below for the case the guard exists for.
        quartz = FakeQuartz()
        backend = started(monkeypatch, quartz)
        for name in ("kCGEventTapDisabledByUserInput", "kCGEventTapDisabledByTimeout"):
            backend._callback(None, backend._q.constant(name), FakeEvent(), None)
        assert quartz.enabled[-2:] == [(backend._tap, True), (backend._tap, True)]

    def test_a_disabled_tap_is_left_alone_once_it_is_going_away(self, monkeypatch):
        # `stop` disables the tap itself, and the notification that produces arrives
        # right then: measured on macOS 26.7, in every one of five live runs, after
        # every event that was going to be captured had been. Putting the tap back
        # there would fight the teardown it is part of.
        quartz = FakeQuartz()
        backend = started(monkeypatch, quartz)
        backend.stop()
        before = list(quartz.enabled)
        backend._callback(
            None,
            backend._q.constant("kCGEventTapDisabledByUserInput"),
            FakeEvent(),
            None,
        )
        assert quartz.enabled == before

    def test_events_ends_at_the_end_marker(self, monkeypatch):
        quartz = FakeQuartz()
        backend = started(monkeypatch, quartz)
        raw = backend._translate(event_type(backend, "kCGEventMouseMoved"), FakeEvent())
        backend._queue.put(raw)
        backend._finish_at_stop(raw.timestamp)
        assert list(backend.events()) == [raw]

    def test_events_raises_a_failure_that_arrived_from_the_thread(self, monkeypatch):
        backend = started(monkeypatch, FakeQuartz())
        backend._queue.put(RuntimeError("the tap went away"))
        with pytest.raises(CaptureUnavailable) as caught:
            next(iter(backend.events()))
        assert "the tap went away" in str(caught.value)

    def test_drain_returns_what_arrived_and_keeps_the_marker(self, monkeypatch):
        # A run interrupted mid-flight is owed what the queue held; the marker goes back
        # so
        # a later `events` still terminates, and a queued error is skipped rather than
        # throwing away the events behind it.
        backend = started(monkeypatch, FakeQuartz())
        first = backend._translate(
            event_type(backend, "kCGEventMouseMoved"), FakeEvent()
        )
        backend._queue.put(first)
        backend._queue.put(RuntimeError("boom"))
        backend._queue.put(macos_module._SENTINEL)
        assert list(backend.drain()) == [first]
        assert list(backend.events()) == []

    def test_a_recorded_stop_chord_ends_the_stream_where_it_completed(
        self, monkeypatch
    ):
        # The arrangement x11 uses: capture recognises the chord as the presses arrive,
        # records them, and ends the stream there -- so a recording that fell behind
        # live
        # input still ends at the press rather than where the consumer had got to.
        stop = StopKey(chord="Escape", presses=2, interval=2.0)
        backend = started(monkeypatch, FakeQuartz(), stop_key=stop)
        press = event_type(backend, "kCGEventKeyDown")
        escape = FakeEvent(keycode=53, stamp=7_000_000_000.0)
        backend._callback(None, press, escape, None)
        backend._callback(None, press, escape, None)
        assert backend.stop_pressed_at == pytest.approx(7.0)
        assert backend._finished is True
        assert [raw.keysym for raw in backend.drain()] == ["Escape", "Escape"]

    def test_an_untranslatable_event_does_not_end_the_run(self, monkeypatch):
        # One unrecognised CGEvent is no reason to lose the rest of a recording.
        backend = started(monkeypatch, FakeQuartz())
        backend._callback(None, -999, FakeEvent(), None)
        assert backend._finished is False
        assert backend._queue.empty()


class TestAvailability:
    """What `available()` and `unavailable_reason()` answer, and where they point."""

    def test_off_a_mac_the_reason_is_the_platform(self, monkeypatch):
        monkeypatch.setattr(macos_module.sys, "platform", "linux")
        reason = macos_module.unavailable_reason()
        assert reason is not None
        assert "native macOS process" in reason

    def test_without_the_binding_the_reason_names_the_extra(self, monkeypatch):
        monkeypatch.setattr(macos_module.sys, "platform", "darwin")
        monkeypatch.setitem(sys.modules, "Quartz", None)
        reason = macos_module.unavailable_reason()
        assert reason is not None
        assert "pyguitest[macos]" in reason

    def test_an_older_binding_is_told_apart_from_a_missing_one(self, monkeypatch):
        # Two different problems with two different fixes, which is why `_QUARTZ_NEEDED`
        # is a list checked name by name rather than one bare import.
        monkeypatch.setattr(macos_module.sys, "platform", "darwin")
        quartz = types.ModuleType("Quartz")
        for name in _QUARTZ_NEEDED:
            setattr(quartz, name, object())
        monkeypatch.setitem(sys.modules, "Quartz", quartz)
        reason = macos_module.unavailable_reason()
        # Every entry point is present, so the complaint is not about one of them: the
        # probe then fails because `object()` is not callable, which is the grant
        # sentence rather than the older-binding one.
        assert reason is not None and "CGEventTapCreate" not in reason
        del quartz.CGEventTapCreate
        reason = macos_module.unavailable_reason()
        assert reason is not None and "CGEventTapCreate" in reason

    def test_a_refused_tap_is_the_grant_and_names_the_pane(self, monkeypatch):
        install(monkeypatch, FakeQuartz(tap=None))
        reason = macos_module.unavailable_reason()
        assert reason is not None
        assert "Accessibility" in reason
        assert macos_module.available() is False

    def test_a_created_tap_is_available(self, monkeypatch):
        install(monkeypatch, FakeQuartz())
        assert macos_module.unavailable_reason() is None
        assert macos_module.available() is True


class TestSelection:
    """Which backend the recorder picks, and what it says when it cannot.

    The rest of the selection rules live in `tests/test_choose_backend.py`; what is here
    is the part macOS adds to them.
    """

    def test_auto_on_a_mac_means_the_event_tap(self, monkeypatch):
        # Not xrecord, and for the reason Windows is not: a Mac can run an X server
        # (XQuartz), and `auto` picking it would record a fraction of the desktop with
        # no
        # error to show for it.
        monkeypatch.setattr(recorder.sys, "platform", "darwin")
        assert recorder.selected_backend_name(Settings()) == "macos"
        assert recorder.selected_backend_name(Settings(backend="macos")) == "macos"
        # Naming xrecord is still how an XQuartz desktop is recorded.
        assert recorder.selected_backend_name(Settings(backend="xrecord")) == "xrecord"

    def test_naming_macos_elsewhere_refuses_with_a_reason(self, monkeypatch):
        monkeypatch.setattr(recorder.sys, "platform", "linux")
        monkeypatch.setattr(macos_module.sys, "platform", "linux")
        with pytest.raises(CaptureUnavailable) as caught:
            recorder.choose_backend(Settings(backend="macos"))
        assert "native macOS process" in str(caught.value)

    def test_a_macos_recording_is_not_a_windows_desktop(self):
        # `_windows_desktop` decides which platform's advice and window vocabulary a
        # recording is written in, and a Mac is neither of the two older answers.
        assert recorder._windows_desktop("macos") is False
        assert recorder._windows_desktop("win32") is True
        assert recorder._windows_desktop("xrecord") is False

    def test_the_platform_names_say_accessibility_on_a_mac(self):
        assert platforms.is_macos("SessionType.DARWIN") is True
        assert platforms.is_macos("SessionType.X11") is False
        assert platforms.element_api("SessionType.DARWIN") == "the Accessibility API"
        reason = platforms.foreign_focus_reason("SessionType.DARWIN")
        assert "Accessibility API" in reason
        elements = platforms.foreign_element_reason("SessionType.DARWIN")
        assert "XPC" in elements


class TestTheContextOnAMac:
    """Which pyguitest backends a Mac recording asks, and in what words.

    Before this, a Mac asked for `x11` + `atspi` like any other non-Windows
    host: a Mac has no X server and no accessibility bus, so no session opened
    and every click in a recording came out `window: null, element: null` --
    the recorder losing the one thing it exists to do. The note it left named
    `$DISPLAY` on top of that, which is a variable a Mac does not have.

    macOS is not Windows with a different spelling, either: there is one
    backend for both halves there, not a pair, and `xrecord` (XQuartz) is
    still X11's to describe.
    """

    def _recorder(self, monkeypatch, platform="darwin", **overrides):
        monkeypatch.setattr(recorder.sys, "platform", platform)
        return recorder.Recorder(settings=Settings(**overrides))

    def test_a_mac_names_one_backend_for_both_halves(self, monkeypatch):
        # The window list and the Accessibility elements both come out of
        # `macos`, so there is no second name to compose with.
        assert self._recorder(monkeypatch)._context_backends() == ("macos",)

    def test_either_half_alone_still_asks_for_it(self, monkeypatch):
        assert self._recorder(
            monkeypatch, element_context=False
        )._context_backends() == ("macos",)
        assert self._recorder(
            monkeypatch, window_context=False
        )._context_backends() == ("macos",)
        # Asking for neither half still asks pyguitest for nothing at all.
        assert (
            self._recorder(
                monkeypatch, window_context=False, element_context=False
            )._context_backends()
            == ()
        )

    def test_an_xquartz_recording_on_a_mac_is_still_x11s(self, monkeypatch):
        # `backend = "xrecord"` on a Mac records the X clients drawn into
        # XQuartz: X11's window list, X11's pids, and a display to name.
        made = self._recorder(monkeypatch, backend="xrecord")
        assert made._context_backends() == ("x11", "atspi")
        assert made._session_locator(":0") == "on :0"

    def test_the_session_locator_names_no_display_on_a_mac(self, monkeypatch):
        # This string goes into every generated script's footer and every
        # session file, where it sent a Mac reader after a variable their
        # machine does not have.
        assert self._recorder(monkeypatch)._session_locator("") == "for this desktop"

    def test_the_resolver_is_built_for_the_recording(self, monkeypatch):
        made = self._recorder(monkeypatch, backend="macos")._open_resolver_for(None)
        assert made.macos is True
        assert made.windows is False

    def test_an_xquartz_recording_builds_an_x11_resolver(self, monkeypatch):
        made = self._recorder(monkeypatch, backend="xrecord")._open_resolver_for(None)
        assert made.macos is False
        assert made.windows is False

    def test_a_windows_host_cannot_reach_the_macos_pair(self, monkeypatch):
        # `auto` on Windows is `win32`, and naming `macos` there is refused by
        # `choose_backend`, so the macOS context is only reachable on a Mac.
        made = self._recorder(monkeypatch, platform="win32")
        assert made._context_backends() == ("win32", "uia")
        assert made._on_macos() is False

    def test_the_macos_question_follows_the_backend_not_the_host(self, monkeypatch):
        assert recorder._macos_desktop("macos") is True
        assert recorder._macos_desktop("xrecord") is False
        assert recorder._macos_desktop("win32") is False
        # A name this recorder does not know, or none yet, is the host's
        # answer -- which is what `probe_context` relies on.
        monkeypatch.setattr(recorder.sys, "platform", "darwin")
        assert recorder._macos_desktop("") is True
        monkeypatch.setattr(recorder.sys, "platform", "linux")
        assert recorder._macos_desktop("") is False


class TestTheMacEnvironmentSnapshot:
    """A Mac recording's header must not name an X display it never had.

    Installing XQuartz puts `DISPLAY` into the login environment, so a Mac
    recording its own desktop would otherwise carry a display it recorded
    nothing from -- the same bug Windows had with Xming and VcXsrv, one
    platform over, and just as misleading about where its coordinates came
    from.
    """

    def _describe(self, monkeypatch, backend, env, platform="darwin"):
        """The environment a recording through `backend` would carry.

        Detection is stood in for rather than left to run: what it answers is a
        fact about the machine running the suite, and this class is about what
        `describe_environment` does with the answer -- the same reasoning
        `TestWindowsEnvironmentSnapshot` gives. The XWayland probe is stubbed
        for the same reason, since it asks a real X server.
        """
        import pyguitest

        monkeypatch.setattr(recorder.sys, "platform", platform)
        monkeypatch.setattr(
            pyguitest,
            "detect",
            lambda environment=None: types.SimpleNamespace(
                session_type="SessionType.DARWIN", compositor="", desktop=""
            ),
        )
        monkeypatch.setattr(recorder, "_is_xwayland_display", lambda display: None)
        return recorder.describe_environment(None, backend, env)

    def test_a_stray_display_is_not_recorded_on_a_mac(self, monkeypatch):
        env = self._describe(monkeypatch, "macos", {"DISPLAY": ":0"})
        assert env.display == ""
        assert env.xwayland is False

    def test_an_xquartz_recording_on_a_mac_keeps_its_display(self, monkeypatch):
        # The recording the X server is *for*, so the display stays in its
        # header -- the fact explaining where its coordinates came from.
        env = self._describe(monkeypatch, "xrecord", {"DISPLAY": ":0"})
        assert env.display == ":0"
