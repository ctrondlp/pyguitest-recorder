"""The XRecord parse path, driven by synthetic X events.

This backend needs a live X server to *capture*, which no test suite here has.
What it does not need one for is everything after the bytes arrive: decoding a
record reply into events, folding the wheel buttons into scrolls, resolving
keysyms under the shift state, and coming apart cleanly. Those are exercised
with real `Xlib.protocol.event` structures and stand-in connections, which is
the difference between this file being written and being known to work.
"""

import threading

import pytest

record = pytest.importorskip("Xlib.ext.record", reason="python-xlib is not installed")
from Xlib.protocol import display as pdisplay  # noqa: E402
from Xlib.protocol import event as xevent  # noqa: E402

from pyguitest_recorder.backends import x11 as x11_module  # noqa: E402
from pyguitest_recorder.backends.base import (  # noqa: E402
    CaptureBackend,
    CaptureUnavailable,
)
from pyguitest_recorder.backends.x11 import (  # noqa: E402
    X11CaptureBackend,
    _group_switch_mask,
    _is_case_pair,
    _keysym_names,
    _printable,
)
from pyguitest_recorder.stopkey import StopKey  # noqa: E402


class FakeProtocolDisplay:
    """Enough of a protocol display for EventField to decode against."""

    event_classes = pdisplay.Display.event_classes

    def get_resource_class(self, name):
        return None


class FakeConnection:
    """A display connection that answers only what the parse path asks it."""

    def __init__(self, keymap=None, modifier_mapping=None):
        self.display = FakeProtocolDisplay()
        self.keymap = keymap or {}
        self.modifier_mapping = modifier_mapping or [[] for _ in range(8)]
        self.closed = False
        self.disabled = threading.Event()
        self.pending = []
        self.refreshed = []
        self.sync_calls = 0

    def keycode_to_keysym(self, keycode, index):
        return self.keymap.get((keycode, index), 0)

    def get_modifier_mapping(self):
        return self.modifier_mapping

    def sync(self):
        """A round trip.

        The point at which a real connection would pull a broadcast
        MappingNotify off the wire. Counted rather than acted on: what
        matters to the tests here is that `_resolve_keysym` asks.
        """
        self.sync_calls += 1

    def pending_events(self):
        return len(self.pending)

    def next_event(self):
        return self.pending.pop(0)

    def refresh_keyboard_mapping(self, evt):
        self.refreshed.append(evt)

    def record_disable_context(self, context):
        self.disabled.set()

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeReply:
    """One `record_enable_context` reply carrying packed X events."""

    def __init__(self, *events, category=0, swapped=0, header=2):
        self.category = category
        self.client_swapped = swapped
        self.data = bytes([header]) + b"".join(e._binary for e in events)[1:]
        if events:
            # The first byte of a record reply is the element header, and it
            # overlaps the first event's type byte, which is why the real
            # stream is parsed from data[0] onwards rather than data[1:].
            self.data = b"".join(e._binary for e in events)


def press(detail=1, x=300, y=400, state=0):
    return xevent.ButtonPress(
        time=1,
        root=1,
        window=2,
        child=0,
        root_x=x,
        root_y=y,
        event_x=0,
        event_y=0,
        state=state,
        detail=detail,
        same_screen=1,
    )


def release(detail=1, x=300, y=400):
    return xevent.ButtonRelease(
        time=1,
        root=1,
        window=2,
        child=0,
        root_x=x,
        root_y=y,
        event_x=0,
        event_y=0,
        state=0,
        detail=detail,
        same_screen=1,
    )


def key(detail, state=0, kind=xevent.KeyPress):
    return kind(
        time=1,
        root=1,
        window=2,
        child=0,
        root_x=5,
        root_y=6,
        event_x=0,
        event_y=0,
        state=state,
        detail=detail,
        same_screen=1,
    )


def motion(x=7, y=8):
    return xevent.MotionNotify(
        time=1,
        root=1,
        window=2,
        child=0,
        root_x=x,
        root_y=y,
        event_x=0,
        event_y=0,
        state=0,
        detail=0,
        same_screen=1,
    )


def backend(keymap=None, screen=0, group_mask=None, stop_key=None):
    made = X11CaptureBackend(screen=screen, stop_key=stop_key)
    made._control = FakeConnection(keymap)
    made._pump = FakeConnection(keymap)
    made._keysyms = _keysym_names({"XK": __import__("Xlib.XK", fromlist=["XK"])})
    made._group_mask = group_mask
    return made


def escape_backend(stop_key=None):
    """A backend whose keycode 9 is Escape, which the stop key is by default."""
    from Xlib import XK

    return backend(keymap={(9, 0): XK.XK_Escape}, stop_key=stop_key)


def drain(made):
    events = []
    while not made._queue.empty():
        events.append(made._queue.get_nowait())
    return events


def test_a_press_and_release_pair_come_through_with_root_coordinates():
    made = backend()
    made._handle(FakeReply(press(), release()))
    got = drain(made)
    assert [e.kind for e in got] == ["button_press", "button_release"]
    assert (got[0].x, got[0].y) == (300, 400)
    assert got[0].button == 1


def test_every_event_packed_into_one_reply_is_parsed():
    made = backend()
    made._handle(FakeReply(motion(), press(), release(), motion()))
    assert [e.kind for e in drain(made)] == [
        "motion",
        "button_press",
        "button_release",
        "motion",
    ]


def test_the_wheel_buttons_become_scrolls_in_pyguitest_signs():
    # X11 reports the wheel as buttons 4-7. dy positive is up and dx positive
    # is right, matching Session.scroll -- the conversion happens here so
    # nothing downstream has to know X11's convention.
    made = backend()
    made._handle(FakeReply(press(4), press(5), press(6), press(7)))
    got = drain(made)
    assert [(e.dx, e.dy) for e in got] == [(0, 1), (0, -1), (-1, 0), (1, 0)]
    assert {e.kind for e in got} == {"scroll"}


def test_a_wheel_release_is_not_a_second_scroll():
    made = backend()
    made._handle(FakeReply(press(4), release(4)))
    assert [e.kind for e in drain(made)] == ["scroll"]


def test_a_key_resolves_to_its_keysym_name_and_the_character_it_typed():
    from Xlib import XK

    made = backend(keymap={(38, 0): XK.XK_a, (38, 1): XK.XK_A})
    made._handle(FakeReply(key(38)))
    got = drain(made)[0]
    assert (got.kind, got.keysym, got.text) == ("key_press", "a", "a")


def test_the_shift_state_picks_the_shifted_keysym():
    from Xlib import XK

    made = backend(keymap={(38, 0): XK.XK_a, (38, 1): XK.XK_A})
    made._handle(FakeReply(key(38, state=1)))
    got = drain(made)[0]
    assert (got.keysym, got.text) == ("A", "A")


def test_a_key_with_no_shifted_mapping_falls_back_to_the_unshifted_one():
    from Xlib import XK

    made = backend(keymap={(36, 0): XK.XK_Return})
    made._handle(FakeReply(key(36, state=1)))
    got = drain(made)[0]
    # Return is not printable, so it stays a named key rather than text --
    # which is what keeps `gui.tap_key("Return")` out of `gui.type_text`.
    assert (got.keysym, got.text) == ("Return", "")


def test_altgr_picks_the_group_2_keysym():
    from Xlib import XK

    # keycode 10 as a German "1" key: group 1 is 1/!, group 2 (AltGr) is a
    # currency sign with no shifted form of its own.
    made = backend(
        keymap={(10, 0): XK.XK_1, (10, 1): XK.XK_exclam, (10, 2): XK.XK_at},
        group_mask=1 << 4,  # Mod2, arbitrarily -- any Mod bit but the fixed ones
    )
    made._handle(FakeReply(key(10, state=1 << 4)))
    got = drain(made)[0]
    assert (got.keysym, got.text) == ("at", "@")


def test_no_group_switch_bound_always_reads_group_1():
    from Xlib import XK

    # Same keymap as above, but this backend never found a Mode_switch/
    # ISO_Level3_Shift key (group_mask=None, the default for every layout
    # with no third level) -- group 2 must stay unreachable even if the
    # state bit that would have selected it happens to be set for some
    # other reason.
    made = backend(
        keymap={(10, 0): XK.XK_1, (10, 1): XK.XK_exclam, (10, 2): XK.XK_at},
    )
    made._handle(FakeReply(key(10, state=1 << 4)))
    got = drain(made)[0]
    assert got.keysym == "1"


def test_capslock_shifts_a_letter_like_a_second_shift():
    from Xlib import XK

    made = backend(keymap={(38, 0): XK.XK_a, (38, 1): XK.XK_A})
    made._handle(FakeReply(key(38, state=x11_module._LOCK_MASK)))
    got = drain(made)[0]
    assert (got.keysym, got.text) == ("A", "A")


def test_shift_cancels_capslock_on_a_letter():
    from Xlib import XK

    made = backend(keymap={(38, 0): XK.XK_a, (38, 1): XK.XK_A})
    state = x11_module._LOCK_MASK | x11_module._SHIFT_MASK
    made._handle(FakeReply(key(38, state=state)))
    got = drain(made)[0]
    # Shift+CapsLock on a letter types lowercase, matching a real keyboard.
    assert (got.keysym, got.text) == ("a", "a")


def test_capslock_does_not_affect_digits_or_punctuation():
    from Xlib import XK

    made = backend(keymap={(10, 0): XK.XK_1, (10, 1): XK.XK_exclam})
    made._handle(FakeReply(key(10, state=x11_module._LOCK_MASK)))
    got = drain(made)[0]
    # Lock has no case pair to latch onto here, so it is simply ignored --
    # a real CapsLock does not turn "1" into "!".
    assert (got.keysym, got.text) == ("1", "1")


def test_an_unmapped_keycode_is_reported_rather_than_dropped():
    made = backend()
    made._handle(FakeReply(key(200)))
    assert drain(made)[0].keysym == "0x0"


def test_a_pending_mapping_notify_is_applied_before_the_keysym_lookup():
    """A remap mid-recording is picked up, not read off a stale cache.

    `xdotool type` (and several input methods) type a character outside the
    base layout by remapping an unused keycode to it, typing through that
    keycode, then moving on -- and this backend's keysym lookups run against
    a connection whose local keymap cache is built once, at `start()`, and
    never refreshed on its own. Found live recording real Chinese text: every
    character came back keysym `0x0`. `_resolve_keysym` now drains any
    `MappingNotify` waiting on `self._control` and hands it to
    `refresh_keyboard_mapping` before resolving -- this checks that it does,
    using the fake's own bookkeeping rather than a real remap.
    """
    made = backend()
    notice = xevent.MappingNotify(
        sequence_number=0, request=0, first_keycode=8, count=1
    )
    made._control.pending.append(notice)
    made._handle(FakeReply(key(38)))
    assert made._control.sync_calls == 1
    assert made._control.refreshed == [notice]


def test_resolving_a_keysym_syncs_even_with_nothing_pending():
    # The ordinary case: no remap happened, so the drain finds nothing -- but
    # the sync that would have picked one up still has to run every time,
    # since a keycode's mapping can change between any two key events.
    made = backend()
    made._handle(FakeReply(key(38)))
    assert made._control.sync_calls == 1
    assert made._control.refreshed == []


def test_a_key_release_is_kept_because_modifiers_need_it():
    from Xlib import XK

    made = backend(keymap={(37, 0): XK.XK_Control_L})
    made._handle(FakeReply(key(37, kind=xevent.KeyRelease)))
    got = drain(made)[0]
    assert (got.kind, got.keysym) == ("key_release", "Control_L")


def test_the_screen_number_is_stamped_on_what_is_captured():
    made = backend(screen=2)
    made._handle(FakeReply(press(), motion()))
    assert {e.screen for e in drain(made)} == {2}


def test_replies_that_are_not_recorded_input_are_ignored():
    made = backend()
    made._handle(FakeReply(press(), category=1))
    made._handle(FakeReply(press(), swapped=1))
    assert drain(made) == []


def test_a_byte_swapped_or_control_reply_does_not_raise():
    made = backend()
    empty = FakeReply()
    empty.data = b"\x00"
    made._handle(empty)
    assert drain(made) == []


def test_printable_rejects_control_characters():
    from Xlib import XK

    assert _printable(XK.XK_a) == "a"
    assert _printable(XK.XK_Return) == ""
    assert _printable(XK.XK_BackSpace) == ""
    assert _printable(XK.XK_Escape) == ""


def test_printable_decodes_the_direct_unicode_keysym_block():
    """ICCCM's 0x01000000+codepoint block.

    `keysym_to_string` never covers it -- confirmed missing live, recording
    `xdotool type` of Chinese text, where every character in this block came
    back empty. `chr('你')` is `0x4f60`, so its keysym is `0x1004f60`.
    """
    assert _printable(0x01004F60) == "你"
    assert _printable(0x0100_0041) == "A"  # low end of the block, sanity check


def test_printable_refuses_a_lone_surrogate_rather_than_crash_later():
    # VITAL -- keep this test. A keysym in 0x0100D800-0x0100DFFF is a valid
    # `chr()` argument -- it does not raise -- but the lone surrogate it
    # produces cannot be UTF-8 encoded, so it must degrade to "" here rather
    # than crash a later write of the recording (JSON, or the generated
    # script) with an unhandled UnicodeEncodeError.
    assert _printable(0x0100D800) == ""
    assert _printable(0x0100DFFF) == ""
    assert _printable(0x0100DC00) == ""  # mid-range, not just the edges


def test_stop_lets_the_pump_thread_out_before_closing_its_connection():
    # The bug this guards: `record_enable_context` blocks in a socket read on
    # the pump connection, so closing that connection first pulls the socket
    # out from under the thread reading it. Disabling the context through the
    # control connection is what makes the call return.
    made = backend()
    made._context = object()
    inside = threading.Event()
    order = []

    def blocking(context, callback):
        inside.set()
        # Only the control connection can end this; that is the whole point.
        made._control.disabled.wait(timeout=5)
        order.append("thread left record_enable_context")

    made._pump.record_enable_context = blocking
    made._thread = threading.Thread(target=made._run, daemon=True)
    made._thread.start()
    assert inside.wait(timeout=5)

    original_close = FakeConnection.close

    def close(self):
        order.append("connection closed")
        original_close(self)

    FakeConnection.close = close
    try:
        made.stop()
    finally:
        FakeConnection.close = original_close
    assert order[0] == "thread left record_enable_context"
    assert "connection closed" in order


class _StartFakeConnection(FakeConnection):
    """Adds the two calls `start()` makes that the shared fake doesn't."""

    def __init__(self, *, create_context_error=None):
        super().__init__()
        self._create_context_error = create_context_error

    def query_extension(self, name):
        return True

    def record_create_context(self, *args):
        if self._create_context_error is not None:
            raise self._create_context_error
        return object()


def test_start_closes_an_already_open_connection_when_the_second_fails(monkeypatch):
    # start() used to only clean up for the "no RECORD extension" case --
    # any other failure after the first connection succeeded (the second
    # connection refused, here) leaked it.
    opened = []

    def fake_connect(xlib, name):
        if opened:
            raise CaptureUnavailable("second connection refused")
        conn = _StartFakeConnection()
        opened.append(conn)
        return conn

    monkeypatch.setattr(x11_module, "_connect", fake_connect)
    made = X11CaptureBackend()
    with pytest.raises(CaptureUnavailable, match="second connection refused"):
        made.start()
    assert len(opened) == 1
    assert opened[0].closed
    assert made._control is None
    assert made._pump is None


def test_start_closes_both_connections_when_creating_the_context_fails(monkeypatch):
    # A failure this late used to leak both connections: nothing after the
    # RECORD-extension check ran through `stop()` on the way out.
    opened = []

    def fake_connect(xlib, name):
        conn = _StartFakeConnection(create_context_error=RuntimeError("no context"))
        opened.append(conn)
        return conn

    monkeypatch.setattr(x11_module, "_connect", fake_connect)
    made = X11CaptureBackend()
    with pytest.raises(RuntimeError, match="no context"):
        made.start()
    assert len(opened) == 2
    assert all(conn.closed for conn in opened)
    assert made._control is None
    assert made._pump is None
    assert made._context is None


def test_the_stream_ends_where_the_stop_chord_completed():
    # The reason capture recognises the chord at all: a recording that fell
    # behind live input used to answer the stop key only once it had worked
    # through the backlog in front of it, and while input outpaced consumption
    # that moment never came -- the run could not be stopped. Ending the stream
    # at the press means the recording always ends where it was asked to.
    made = escape_backend(StopKey())
    made._handle(FakeReply(key(9)))
    assert made.stop_pressed_at is None
    made._handle(FakeReply(key(9)))
    assert made.stop_pressed_at is not None
    # Both presses reach the consumer first, so the recording still ends at the
    # press the way it always did, and the presses are swallowed there.
    assert [e.kind for e in made.events()] == ["key_press", "key_press"]


def test_what_followed_the_stop_chord_in_the_same_reply_is_never_parsed():
    made = escape_backend(StopKey())
    made._handle(FakeReply(key(9), key(9), motion()))
    assert [e.kind for e in made.events()] == ["key_press", "key_press"]
    assert drain(made) == []


def test_a_reply_after_the_stop_chord_is_ignored_entirely():
    # Everything captured after the press belongs to the session, not to the
    # recording: handing it on would grow the queue for as long as someone
    # keeps using the desktop after asking the recording to stop.
    made = escape_backend(StopKey())
    made._handle(FakeReply(key(9), key(9)))
    assert [e.kind for e in made.events()] == ["key_press", "key_press"]
    made._handle(FakeReply(press(), motion()))
    assert drain(made) == []


def test_a_single_press_is_handed_on_and_capture_keeps_going():
    # One Escape belongs to the application being recorded, so nothing is
    # withheld from the queue while a run might still complete.
    made = escape_backend(StopKey())
    made._handle(FakeReply(key(9), motion()))
    assert made.stop_pressed_at is None
    assert [e.kind for e in drain(made)] == ["key_press", "motion"]


def test_without_a_recogniser_capture_never_ends_the_stream_itself():
    # A backend left to itself still hands over what it captures and waits to be
    # stopped; only the consumer's own view of the stop key ends that recording.
    made = escape_backend()
    made._handle(FakeReply(key(9), key(9)))
    assert made.stop_pressed_at is None
    assert [e.kind for e in drain(made)] == ["key_press", "key_press"]


def test_it_is_a_capture_backend():
    # The protocol is runtime-checkable so that a member added to it is checked
    # against every backend without anyone having to remember which they are --
    # the Windows one was missing two when this was written, and nothing on the
    # platform it was written on could see it.
    assert isinstance(backend(), CaptureBackend)


def test_events_yields_what_was_captured_and_stops_at_the_end():
    made = backend()
    made._handle(FakeReply(press()))
    made.stop()
    assert [e.kind for e in made.events()] == ["button_press"]


def test_drain_returns_what_was_already_captured_without_waiting():
    # `events` blocks, so it cannot be used to collect what a run that ended
    # mid-flight is owed: waiting for input would hang instead.
    made = backend()
    made._handle(FakeReply(press(), release()))
    assert [e.kind for e in made.drain()] == ["button_press", "button_release"]


def test_drain_takes_only_what_was_queued_when_it_was_called():
    # A session that keeps being used must not be able to extend the drain:
    # what is owed is what had already arrived, not what arrives next.
    made = backend()
    made._handle(FakeReply(press()))
    assert [e.kind for e in made.drain()] == ["button_press"]
    made._handle(FakeReply(release()))
    assert [e.kind for e in made.drain()] == ["button_release"]


def test_drain_leaves_the_end_of_stream_marker_for_a_reader():
    # Swallowing the sentinel would leave a later `events` call blocked
    # forever on a queue nothing else will ever fill.
    made = backend()
    made.stop()
    assert list(made.drain()) == []
    assert list(made.events()) == []


def test_drain_skips_the_pumps_own_error_and_keeps_what_is_behind_it():
    # `events` raises the pump's failure, which is right while it is running.
    # At the end of a run there is nothing left to raise it for, and the
    # events queued behind it are the whole reason to drain.
    made = backend()
    made._queue.put(RuntimeError("the X server went away"))
    made._handle(FakeReply(press()))
    assert [e.kind for e in made.drain()] == ["button_press"]


def test_an_error_on_the_pump_thread_surfaces_as_capture_unavailable():
    made = backend()
    made._queue.put(RuntimeError("the X server went away"))
    with pytest.raises(CaptureUnavailable, match="went away"):
        list(made.events())


def keysym_names():
    from Xlib import XK

    return _keysym_names({"XK": XK})


def test_ordinary_keysyms_are_named():
    names = keysym_names()
    assert names.get(0xFFBE) == "F1"
    assert names.get(0xFF0D) == "Return"


def test_multimedia_keysyms_are_named_too():
    # python-xlib loads only the core keysym groups into XK by default, so
    # every media key fell through to its hex value. A laptop whose F-row
    # sends media keys unless Fn is held recorded Ctrl+F1 as
    # `send_keys("^({0x1008ff12})")` -- a name press_key cannot resolve, and
    # one `validate()` cannot catch because it is a string argument.
    assert keysym_names().get(0x1008FF12) == "XF86_AudioMute"


def test_group_switch_mask_finds_iso_level3_shift_on_whichever_mod():
    from Xlib import XK

    XK.load_keysym_group("xkb")  # ISO_Level3_Shift lives there, not in the core set
    xlib = {"XK": XK}
    # keycode 108 as a real AltGr key does: bound under Mod3, not Mod5,
    # which is why this is not hardcoded to Mod5 in the implementation.
    mapping = [[] for _ in range(8)]
    mapping[5] = [108]  # index 5 == Mod3MapIndex
    display = FakeConnection(
        keymap={(108, 0): XK.XK_ISO_Level3_Shift},
        modifier_mapping=mapping,
    )
    assert _group_switch_mask(display, xlib) == 1 << 5


def test_group_switch_mask_accepts_mode_switch_too():
    from Xlib import XK

    xlib = {"XK": XK}
    mapping = [[] for _ in range(8)]
    mapping[7] = [203]  # index 7 == Mod5MapIndex
    display = FakeConnection(
        keymap={(203, 0): XK.XK_Mode_switch}, modifier_mapping=mapping
    )
    assert _group_switch_mask(display, xlib) == 1 << 7


def test_group_switch_mask_is_none_when_nothing_is_bound():
    from Xlib import XK

    xlib = {"XK": XK}
    display = FakeConnection(keymap={})
    assert _group_switch_mask(display, xlib) is None


def test_group_switch_mask_never_looks_at_shift_lock_or_control():
    from Xlib import XK

    xlib = {"XK": XK}
    mapping = [[] for _ in range(8)]
    # Put an actual Mode_switch keycode under Lock -- a nonsensical mapping,
    # but the point is that indices 0-2 are skipped unconditionally, not
    # that a real keyboard would ever do this.
    mapping[1] = [50]
    display = FakeConnection(
        keymap={(50, 0): XK.XK_Mode_switch}, modifier_mapping=mapping
    )
    assert _group_switch_mask(display, xlib) is None


def test_is_case_pair_recognizes_a_letters_two_cases():
    from Xlib import XK

    assert _is_case_pair(XK.XK_a, XK.XK_A) is True


def test_is_case_pair_rejects_a_digit_and_its_shifted_symbol():
    from Xlib import XK

    assert _is_case_pair(XK.XK_1, XK.XK_exclam) is False


def test_is_case_pair_rejects_two_identical_keysyms():
    from Xlib import XK

    assert _is_case_pair(XK.XK_a, XK.XK_a) is False
