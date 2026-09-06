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
    events = drain(Normalizer(resolver=resolver), [key(1.0, "F9")])
    assert [type(e).__name__ for e in events] == ["Assertion"]
    assert events[0].check == "text"
    assert events[0].expected == "Saved"


def test_the_check_key_can_be_switched_off(key):
    # With no check key the key belongs to the application again, and has to
    # record as the keystroke it is rather than disappearing.
    normalizer = Normalizer(options=NormalizerOptions(check_key=""))
    events = drain(normalizer, [key(1.0, "F9")])
    assert [type(e).__name__ for e in events] == ["KeyStroke"]
    assert events[0].key == "F9"


def test_a_check_lands_after_the_typing_it_verifies(key):
    # The shape this is used in: type a value, point at what should have
    # changed, press the key. A check emitted before the pending text would
    # replay as a check of the state before the typing happened.
    resolver = observed("entry", "Filename", text="report.txt")
    events = drain(
        Normalizer(resolver=resolver),
        [key(1.0, "h", "h"), key(1.1, "i", "i"), key(1.2, "F9")],
    )
    assert [type(e).__name__ for e in events] == ["TextInput", "Assertion"]


def test_a_password_field_check_is_marked_sensitive(key):
    resolver = observed("password text", "Password", text="hunter2")
    events = drain(Normalizer(resolver=resolver), [key(1.0, "F9")])
    assert events[0].sensitive is True


def test_a_checkbox_is_checked_rather_than_read(key):
    resolver = observed("check box", "Read only", text="", checked=True)
    events = drain(Normalizer(resolver=resolver), [key(1.0, "F9")])
    assert (events[0].check, events[0].expected) == ("checked", True)


def test_a_button_can_only_be_checked_for_being_there(key):
    resolver = observed("push button", "Save", text="Save")
    events = drain(Normalizer(resolver=resolver), [key(1.0, "F9")])
    assert events[0].check == "showing"


def test_an_empty_label_is_not_asserted_on(key):
    # A toolkit that publishes no text reports the empty string, and "this
    # label is empty" passes against an application that stopped drawing.
    resolver = observed("label", "Status", text="")
    events = drain(Normalizer(resolver=resolver), [key(1.0, "F9")])
    assert events[0].check == "showing"


def test_an_empty_entry_is_asserted_on(key):
    # The opposite case: clearing a field is a real state worth verifying.
    resolver = observed("entry", "Filename", text="")
    events = drain(Normalizer(resolver=resolver), [key(1.0, "F9")])
    assert (events[0].check, events[0].expected) == ("text", "")


def test_a_check_with_no_element_falls_back_to_the_window(key, window):
    resolver = FakeResolver(window=window)
    events = drain(Normalizer(resolver=resolver), [key(1.0, "F9")])
    assert (events[0].check, events[0].expected) == ("window", "Example")


def test_a_check_on_nothing_is_recorded_rather_than_dropped(key):
    # Silently discarding it would leave a script that looks like it verifies
    # something; the generator turns this into a warning in the header.
    events = drain(Normalizer(resolver=FakeResolver()), [key(1.0, "F9")])
    assert events[0].check == "nothing"
