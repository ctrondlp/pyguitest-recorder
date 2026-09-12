import pytest

from conftest import FakeResolver
from pyguitest_recorder.analyzer import Normalizer, NormalizerOptions
from pyguitest_recorder.backends.base import RawEvent
from pyguitest_recorder.model import (
    Click,
    Drag,
    ElementRef,
    HotKey,
    KeyStroke,
    Origin,
    Pause,
    Scroll,
    Target,
    TextInput,
)


def drain(normalizer, raws):
    out = []
    for raw in raws:
        out.extend(normalizer.feed(raw))
    out.extend(normalizer.flush())
    return out


def test_press_and_release_become_one_click(press, release):
    events = drain(Normalizer(), [press(1.0), release(1.05)])
    assert len(events) == 1
    assert isinstance(events[0], Click)
    assert events[0].count == 1


def test_two_fast_clicks_merge_into_a_double(press, release):
    events = drain(
        Normalizer(), [press(1.0), release(1.02), press(1.10), release(1.12)]
    )
    assert len(events) == 1
    assert events[0].count == 2


def test_two_slow_clicks_stay_separate(press, release):
    events = drain(Normalizer(), [press(1.0), release(1.02), press(3.0), release(3.02)])
    clicks = [e for e in events if isinstance(e, Click)]
    assert [c.count for c in clicks] == [1, 1]


def test_clicks_far_apart_do_not_merge(press, release):
    events = drain(
        Normalizer(),
        [
            press(1.0, x=10, y=10),
            release(1.01, x=10, y=10),
            press(1.05, x=400, y=400),
            release(1.06, x=400, y=400),
        ],
    )
    clicks = [e for e in events if isinstance(e, Click)]
    assert len(clicks) == 2


def test_press_move_release_is_a_drag(press, release):
    motion = RawEvent(kind="motion", timestamp=1.05, x=300, y=300)
    events = drain(
        Normalizer(), [press(1.0, x=10, y=10), motion, release(1.1, x=300, y=300)]
    )
    assert len(events) == 1
    assert isinstance(events[0], Drag)
    assert (events[0].start.x, events[0].end.x) == (10, 300)


def test_printable_keys_coalesce_into_one_string(key):
    events = drain(
        Normalizer(),
        [key(1.0, "h", "h"), key(1.1, "i", "i"), key(1.2, "exclam", "!")],
    )
    assert len(events) == 1
    assert isinstance(events[0], TextInput)
    assert events[0].text == "hi!"


def test_idle_gap_splits_a_text_run(key):
    options = NormalizerOptions(text_idle=0.5, pause_threshold=100.0)
    events = drain(
        Normalizer(options=options), [key(1.0, "a", "a"), key(5.0, "b", "b")]
    )
    texts = [e.text for e in events if isinstance(e, TextInput)]
    assert texts == ["a", "b"]


def test_named_key_becomes_a_keystroke_and_flushes_text(key):
    events = drain(Normalizer(), [key(1.0, "a", "a"), key(1.1, "Return")])
    assert isinstance(events[0], TextInput)
    assert isinstance(events[1], KeyStroke)
    assert events[1].key == "Return"


def test_modifier_plus_key_is_a_hotkey(key):
    events = drain(
        Normalizer(),
        [
            key(1.0, "Control_L"),
            key(1.05, "s"),
            key(1.1, "Control_L", kind="key_release"),
        ],
    )
    assert len(events) == 1
    assert isinstance(events[0], HotKey)
    assert events[0].keys == ("ctrl", "s")


def test_shift_does_not_make_text_a_hotkey(key):
    events = drain(
        Normalizer(),
        [
            key(1.0, "Shift_L"),
            key(1.05, "A", "A"),
            key(1.1, "Shift_L", kind="key_release"),
        ],
    )
    assert len(events) == 1
    assert isinstance(events[0], TextInput)
    assert events[0].text == "A"


def test_scroll_passes_through_in_detents():
    raw = RawEvent(kind="scroll", timestamp=1.0, x=5, y=5, dy=1)
    events = drain(Normalizer(), [raw])
    assert isinstance(events[0], Scroll)
    assert events[0].dy == 1


def test_motion_is_dropped_unless_asked_for():
    motion = RawEvent(kind="motion", timestamp=1.0, x=5, y=5)
    assert drain(Normalizer(), [motion]) == []
    options = NormalizerOptions(record_motion=True)
    assert len(drain(Normalizer(options=options), [motion])) == 1


def test_long_gap_becomes_a_pause(press, release):
    options = NormalizerOptions(pause_threshold=1.0)
    events = drain(
        Normalizer(options=options),
        [press(1.0), release(1.02), press(5.0), release(5.02)],
    )
    pauses = [e for e in events if isinstance(e, Pause)]
    assert len(pauses) == 1
    assert pauses[0].seconds == pytest.approx(3.98, abs=0.01)


def test_password_field_marks_text_sensitive(key):
    secret = ElementRef(role="password text", name="Password")
    resolver = FakeResolver(elements=[((0, 0, 1000, 1000), secret)])
    events = drain(Normalizer(resolver=resolver), [key(1.0, "a", "a")])
    assert events[0].sensitive is True


def test_sensitive_option_marks_everything(key):
    options = NormalizerOptions(sensitive=True)
    events = drain(Normalizer(options=options), [key(1.0, "a", "a")])
    assert events[0].sensitive is True


def test_click_carries_the_element_under_it(press, release, save_button):
    resolver = FakeResolver(elements=[((150, 150, 100, 100), save_button)])
    events = drain(Normalizer(resolver=resolver), [press(1.0), release(1.02)])
    assert events[0].target.element == save_button


def test_pause_is_emitted_after_the_click_that_preceded_it(press, release):
    options = NormalizerOptions(pause_threshold=1.0)
    events = drain(
        Normalizer(options=options),
        [press(1.0), release(1.02), press(5.0), release(5.02)],
    )
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["Click", "Pause", "Click"]
    # The gap starts the instant the click ended, so the timestamps may be
    # equal; what matters is that the pause never precedes the click.
    assert events[0].timestamp <= events[1].timestamp < events[2].timestamp
    assert events[1].origin is Origin.INFERRED


def test_typing_is_anchored_to_the_last_clicked_field(press, release, key):
    field = ElementRef(role="entry", name="Document")
    resolver = FakeResolver(elements=[((150, 150, 100, 100), field)])
    normalizer = Normalizer(resolver=resolver)
    # Click the field at (200, 200), then type with the pointer moved away.
    events = drain(
        normalizer,
        [
            press(1.0, x=200, y=200),
            release(1.02, x=200, y=200),
            RawEvent(kind="motion", timestamp=1.1, x=900, y=900),
            key(1.2, "h", "h"),
        ],
    )
    typed = [e for e in events if isinstance(e, TextInput)][0]
    assert typed.target.element == field


def test_typing_after_clicking_a_button_is_not_anchored_to_it(press, release, key):
    button = ElementRef(role="push button", name="Save")
    resolver = FakeResolver(elements=[((150, 150, 100, 100), button)])
    events = drain(
        Normalizer(resolver=resolver),
        [press(1.0, x=200, y=200), release(1.02, x=200, y=200), key(1.2, "h", "h")],
    )
    typed = [e for e in events if isinstance(e, TextInput)][0]
    assert typed.target.element is None


def test_a_wait_after_typing_is_detected(key):
    # It was not: pending text suppressed gap detection outright, so "type a
    # query, wait for results, click one" -- the shape the analyzer most wants
    # to see -- produced no Pause and therefore nothing to synchronize on.
    normalizer = Normalizer(started=0.0)
    out = []
    for raw in (
        key(1.0, "h", "h"),
        key(1.1, "i", "i"),
        RawEvent(kind="button_press", timestamp=4.0, x=5, y=5, button=1),
        RawEvent(kind="button_release", timestamp=4.05, x=5, y=5, button=1),
    ):
        out.extend(normalizer.feed(raw))
    out.extend(normalizer.flush())
    kinds = [type(e).__name__ for e in out]
    assert kinds == ["TextInput", "Pause", "Click"]
    # The text comes out before the pause that followed it, not after.
    assert out[0].text == "hi"
    assert out[1].seconds == 2.9


def test_hesitation_inside_a_word_is_not_a_pause(key):
    normalizer = Normalizer(
        options=NormalizerOptions(pause_threshold=1.0, text_idle=1.5), started=0.0
    )
    out = []
    for raw in (key(1.0, "h", "h"), key(2.2, "i", "i")):
        out.extend(normalizer.feed(raw))
    out.extend(normalizer.flush())
    assert [type(e).__name__ for e in out] == ["TextInput"]
    assert out[0].text == "hi"


# -- checks ------------------------------------------------------------------


def check_chord(key, t=1.0):
    """The default check key, ctrl+1, as the two presses it really is."""
    return [key(t, "Control_L"), key(t + 0.05, "1")]


def observed(role, name, text=None, checked=None, window=None):
    """A resolver whose one element covers everywhere, in a known state."""
    return FakeResolver(
        window=window,
        elements=[((0, 0, 10000, 10000), ElementRef(role=role, name=name))],
        text=text,
        checked=checked,
    )


def test_the_check_key_records_a_check_instead_of_a_keystroke(key):
    resolver = observed("label", "Status", text="Saved")
    events = drain(Normalizer(resolver=resolver), check_chord(key))
    assert [type(e).__name__ for e in events] == ["Assertion"]
    assert events[0].check == "text"
    assert events[0].expected == "Saved"


def test_the_check_key_can_be_switched_off(key):
    # With no check key the combination belongs to the application again, and
    # has to record as the hotkey it is rather than disappearing.
    normalizer = Normalizer(options=NormalizerOptions(check_key=""))
    events = drain(normalizer, check_chord(key))
    assert [type(e).__name__ for e in events] == ["HotKey"]
    assert events[0].keys == ("ctrl", "1")


def test_the_check_key_needs_its_modifier(key):
    # A bare 1 belongs to the application; only the exact combination fires.
    events = drain(
        Normalizer(resolver=observed("label", "Status", text="Saved")),
        [key(1.0, "1")],
    )
    assert [type(e).__name__ for e in events] == ["KeyStroke"]


def test_a_larger_combination_is_not_the_check_key(key):
    # Exact matching keeps ctrl+shift+1 usable in the application being
    # recorded, rather than swallowing everything built on ctrl+1.
    events = drain(
        Normalizer(resolver=observed("label", "Status", text="Saved")),
        [key(1.0, "Control_L"), key(1.05, "Shift_L"), key(1.1, "1")],
    )
    assert [type(e).__name__ for e in events] == ["HotKey"]


def test_a_check_lands_after_the_typing_it_verifies(key):
    # The shape this is used in: type a value, point at what should have
    # changed, press the key. A check emitted before the pending text would
    # replay as a check of the state before the typing happened.
    resolver = observed("entry", "Filename", text="report.txt")
    events = drain(
        Normalizer(resolver=resolver),
        [key(1.0, "h", "h"), key(1.1, "i", "i"), *check_chord(key, 1.2)],
    )
    assert [type(e).__name__ for e in events] == ["TextInput", "Assertion"]


def test_a_password_field_check_is_marked_sensitive(key):
    resolver = observed("password text", "Password", text="hunter2")
    events = drain(Normalizer(resolver=resolver), check_chord(key))
    assert events[0].sensitive is True


def test_a_checkbox_is_checked_rather_than_read(key):
    resolver = observed("check box", "Read only", text="", checked=True)
    events = drain(Normalizer(resolver=resolver), check_chord(key))
    assert (events[0].check, events[0].expected) == ("checked", True)


def test_a_button_can_only_be_checked_for_being_there(key):
    resolver = observed("push button", "Save", text="Save")
    events = drain(Normalizer(resolver=resolver), check_chord(key))
    assert events[0].check == "showing"


def test_an_empty_label_is_not_asserted_on(key):
    # A toolkit that publishes no text reports the empty string, and "this
    # label is empty" passes against an application that stopped drawing.
    resolver = observed("label", "Status", text="")
    events = drain(Normalizer(resolver=resolver), check_chord(key))
    assert events[0].check == "showing"


def test_an_empty_entry_is_asserted_on(key):
    # The opposite case: clearing a field is a real state worth verifying.
    resolver = observed("entry", "Filename", text="")
    events = drain(Normalizer(resolver=resolver), check_chord(key))
    assert (events[0].check, events[0].expected) == ("text", "")


def test_a_check_with_no_element_falls_back_to_the_window(key, window):
    resolver = FakeResolver(window=window)
    events = drain(Normalizer(resolver=resolver), check_chord(key))
    assert (events[0].check, events[0].expected) == ("window", "Example")


def test_a_check_on_nothing_is_recorded_rather_than_dropped(key):
    # Silently discarding it would leave a script that looks like it verifies
    # something; the generator turns this into a warning in the header.
    events = drain(Normalizer(resolver=FakeResolver()), check_chord(key))
    assert events[0].check == "nothing"


# -- typed text goes where focus is ------------------------------------------


def test_typing_follows_keyboard_focus_when_the_desktop_publishes_it(key, window):
    # The case no other rule sees: the field was reached by Tab, so nothing
    # was ever clicked and the pointer is wherever it was left.
    focus = Target(
        x=0,
        y=0,
        window=window,
        element=ElementRef(role="entry", name="Search"),
    )
    resolver = FakeResolver(window=window, focus=focus)
    events = drain(Normalizer(resolver=resolver), [key(1.0, "h", "h")])
    typed = [e for e in events if isinstance(e, TextInput)][0]
    assert typed.target.element.name == "Search"


def test_focus_beats_the_last_clicked_field(key, window):
    # Clicking one field and then tabbing to the next is ordinary, and the
    # clicked one is then the wrong answer.
    clicked = ElementRef(role="entry", name="First")
    focus = Target(
        x=0, y=0, window=window, element=ElementRef(role="entry", name="Second")
    )
    resolver = FakeResolver(
        window=window, elements=[((0, 0, 500, 500), clicked)], focus=focus
    )
    normalizer = Normalizer(resolver=resolver)
    events = drain(
        normalizer,
        [
            RawEvent(kind="button_press", timestamp=1.0, x=10, y=10, button=1),
            RawEvent(kind="button_release", timestamp=1.02, x=10, y=10, button=1),
            key(2.0, "h", "h"),
        ],
    )
    typed = [e for e in events if isinstance(e, TextInput)][0]
    assert typed.target.element.name == "Second"


def test_focus_on_something_that_does_not_take_text_is_ignored(key, window):
    # A button can hold focus while typing goes to the field behind it; only
    # a text role is evidence about where the characters went.
    clicked = ElementRef(role="entry", name="Name")
    focus = Target(
        x=0, y=0, window=window, element=ElementRef(role="push button", name="Save")
    )
    resolver = FakeResolver(
        window=window, elements=[((0, 0, 500, 500), clicked)], focus=focus
    )
    events = drain(
        Normalizer(resolver=resolver),
        [
            RawEvent(kind="button_press", timestamp=1.0, x=10, y=10, button=1),
            RawEvent(kind="button_release", timestamp=1.02, x=10, y=10, button=1),
            key(2.0, "h", "h"),
        ],
    )
    typed = [e for e in events if isinstance(e, TextInput)][0]
    assert typed.target.element.name == "Name"


def test_an_unnamed_focused_field_does_not_displace_a_named_clicked_one(key, window):
    # An unnamed element cannot be located at replay, so preferring it would
    # trade a locator that works for one that does not.
    clicked = ElementRef(role="entry", name="Name")
    focus = Target(x=0, y=0, window=window, element=ElementRef(role="entry", name=""))
    resolver = FakeResolver(
        window=window, elements=[((0, 0, 500, 500), clicked)], focus=focus
    )
    events = drain(
        Normalizer(resolver=resolver),
        [
            RawEvent(kind="button_press", timestamp=1.0, x=10, y=10, button=1),
            RawEvent(kind="button_release", timestamp=1.02, x=10, y=10, button=1),
            key(2.0, "h", "h"),
        ],
    )
    typed = [e for e in events if isinstance(e, TextInput)][0]
    assert typed.target.element.name == "Name"


def test_focus_is_asked_once_per_run_not_once_per_character(key, window):
    # It costs a walk of the accessible tree, and the answer that matters is
    # where the text started going.
    focus = Target(
        x=0, y=0, window=window, element=ElementRef(role="entry", name="Search")
    )

    class Counting(FakeResolver):
        calls = 0

        def focused(self):
            Counting.calls += 1
            return self.focus

    resolver = Counting(window=window, focus=focus)
    drain(
        Normalizer(resolver=resolver),
        [key(1.0, "h", "h"), key(1.1, "i", "i"), key(1.2, "!", "!")],
    )
    assert Counting.calls == 1


# -- the stop key ------------------------------------------------------------


def stop_recorder(**overrides):
    """A Recorder with capture and context switched off, for the key logic."""
    from pyguitest_recorder.config import Settings
    from pyguitest_recorder.recorder import Recorder

    settings = Settings(window_context=False, element_context=False, **overrides)
    return Recorder(settings=settings)


def feed_stop(recorder, raws):
    """Push raw events through the stop sequence, collecting what got through."""
    passed, stopped = [], False
    for raw in raws:
        ready, stop = recorder._stop_sequence(raw)
        passed.extend(ready)
        if stop:
            stopped = True
            break
    return [r.keysym for r in passed], stopped


def test_two_escapes_in_a_row_stop_the_recording(key):
    keysyms, stopped = feed_stop(
        stop_recorder(),
        [
            key(1.0, "Escape"),
            key(1.05, "Escape", kind="key_release"),
            key(1.1, "Escape"),
        ],
    )
    assert stopped
    assert keysyms == []


def test_one_escape_is_the_applications_and_is_recorded(key):
    # Closing a dialog has to stay recordable while Escape also stops the
    # recording, so a press that does not complete a run is handed on.
    keysyms, stopped = feed_stop(
        stop_recorder(),
        [
            key(1.0, "Escape"),
            key(1.05, "Escape", kind="key_release"),
            key(2.0, "a", "a"),
        ],
    )
    assert not stopped
    assert keysyms == ["Escape", "Escape", "a"]


def test_two_slow_escapes_are_two_ordinary_presses(key):
    keysyms, stopped = feed_stop(
        stop_recorder(stop_key_interval=0.5),
        [key(1.0, "Escape"), key(5.0, "Escape"), key(6.0, "a", "a")],
    )
    assert not stopped
    assert keysyms == ["Escape", "Escape", "a"]


def test_a_single_press_key_still_stops_on_one(key):
    # `Pause` was the old default and suits a keyboard that has one.
    keysyms, stopped = feed_stop(
        stop_recorder(stop_key="Pause", stop_key_presses=1), [key(1.0, "Pause")]
    )
    assert stopped and keysyms == []


def test_the_stop_key_can_carry_a_modifier(key):
    recorder = stop_recorder(stop_key="ctrl+Escape", stop_key_presses=1)
    keysyms, stopped = feed_stop(recorder, [key(1.0, "Escape")])
    assert not stopped and keysyms == ["Escape"]
    keysyms, stopped = feed_stop(recorder, [key(2.0, "Control_L"), key(2.1, "Escape")])
    assert stopped


def test_stop_presses_that_did_not_stop_are_counted(key):
    # Pressing Escape once when two are needed does nothing visible, so the
    # natural next move is to press it again -- and the first press is by then
    # a keystroke in the script. The CLI says so rather than leaving it to be
    # discovered in the generated file.
    recorder = stop_recorder()
    feed_stop(
        recorder,
        [
            key(1.0, "Escape"),
            key(1.05, "Escape", kind="key_release"),
            key(3.0, "a", "a"),
        ],
    )
    assert recorder.unstopped_presses == 1


def test_a_clean_stop_counts_nothing(key):
    recorder = stop_recorder()
    feed_stop(recorder, [key(1.0, "Escape"), key(1.1, "Escape")])
    assert recorder.unstopped_presses == 0


def test_shift_is_kept_in_a_combination_that_has_another_modifier(key):
    # Shift and AltGr decide only whether this is a hotkey at all, because
    # they make text rather than commands. Once Ctrl is down they are part of
    # the combination: Ctrl+Shift+S is a different shortcut from Ctrl+S, and
    # dropping the Shift turned a recorded "Save As" into "Save".
    events = drain(
        Normalizer(),
        [
            key(1.0, "Control_L"),
            key(1.05, "Shift_L"),
            key(1.1, "S"),
            key(1.2, "Shift_L", kind="key_release"),
            key(1.3, "Control_L", kind="key_release"),
        ],
    )
    assert [type(e).__name__ for e in events] == ["HotKey"]
    assert events[0].keys == ("ctrl", "shift", "S")


def test_shift_alone_is_still_text_not_a_hotkey(key):
    events = drain(
        Normalizer(),
        [
            key(1.0, "Shift_L"),
            key(1.05, "A", "A"),
            key(1.1, "Shift_L", kind="key_release"),
        ],
    )
    assert [type(e).__name__ for e in events] == ["TextInput"]


def test_a_press_and_release_at_one_point_is_a_click_not_a_drag(press, release):
    # Where the button went down and came up decides this, not whether the
    # pointer moved in between. A real recording produced
    # `gui.drag((x, y), (x, y))`, which moves nothing while looking like it
    # does -- the user had dragged out and come back.
    motion = RawEvent(kind="motion", timestamp=1.05, x=400, y=400)
    events = drain(
        Normalizer(),
        [press(1.0, x=100, y=100), motion, release(1.2, x=100, y=100)],
    )
    assert [type(e).__name__ for e in events] == ["Click"]
    assert "left and came back" in events[0].note


def test_a_press_and_release_at_different_points_is_still_a_drag(press, release):
    motion = RawEvent(kind="motion", timestamp=1.05, x=250, y=250)
    events = drain(
        Normalizer(),
        [press(1.0, x=100, y=100), motion, release(1.2, x=400, y=400)],
    )
    assert [type(e).__name__ for e in events] == ["Drag"]


def test_an_unnamed_password_field_still_makes_the_run_secret(press, release, key):
    # The leak this exists to stop. A GTK password entry commonly publishes no
    # accessible name -- its label is a sibling -- so it was rejected as a
    # locator and the run was attributed to the username box it had been
    # tabbed out of. A real network-share password went into a script verbatim.
    username = ElementRef(role="entry", name="Username")
    secret = Target(x=0, y=0, element=ElementRef(role="password text", name=""))
    resolver = FakeResolver(elements=[((0, 0, 500, 500), username)], focus=secret)
    events = drain(
        Normalizer(resolver=resolver),
        [
            press(1.0, x=10, y=10),
            release(1.02, x=10, y=10),
            key(2.0, "Tab"),
            key(2.1, "p", "p"),
        ],
    )
    typed = [e for e in events if isinstance(e, TextInput)][0]
    assert typed.sensitive is True


def test_a_named_password_field_is_still_secret_and_still_the_target(key):
    named = Target(x=0, y=0, element=ElementRef(role="password text", name="Password"))
    events = drain(Normalizer(resolver=FakeResolver(focus=named)), [key(1.0, "p", "p")])
    typed = [e for e in events if isinstance(e, TextInput)][0]
    assert typed.sensitive is True
    assert typed.target.element.name == "Password"


def test_focus_on_an_ordinary_field_is_not_secret(key):
    plain = Target(x=0, y=0, element=ElementRef(role="entry", name="Search"))
    events = drain(Normalizer(resolver=FakeResolver(focus=plain)), [key(1.0, "p", "p")])
    assert [e for e in events if isinstance(e, TextInput)][0].sensitive is False
