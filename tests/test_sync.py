"""What each recorded pause was actually waiting for.

The rules under test are the recorder's whole reason to exist: a script that
sleeps for as long as the human did is a macro, and a macro is slow when the
machine is fast and broken when it is slow.
"""

from pyguitest_recorder.analyzer import SyncOptions, infer_synchronization
from pyguitest_recorder.model import (
    Click,
    ElementRef,
    KeyStroke,
    Pause,
    Target,
    TextInput,
    WaitForElement,
    WaitForIdle,
    WaitForWindow,
    WindowRef,
)

MAIN = WindowRef(
    title="Editor", app_id="org.x.Editor", pid=11, geometry=(0, 0, 800, 600)
)
DIALOG = WindowRef(title="Save As", app_id="org.x.Editor.Dialog", pid=11)
SAVE = ElementRef(role="push button", name="Save")
OK = ElementRef(role="push button", name="OK")


def click(t, window=MAIN, element=None, x=10, y=10):
    return Click(timestamp=t, target=Target(x=x, y=y, window=window, element=element))


def kinds(events):
    return [type(e).__name__ for e in events]


def test_a_pause_before_a_new_window_becomes_a_wait_for_that_window():
    events = [
        click(1.0, element=SAVE),
        Pause(timestamp=1.1, seconds=2.4),
        click(3.5, window=DIALOG, element=OK),
    ]
    out = infer_synchronization(events)
    waits = [e for e in out if isinstance(e, WaitForWindow)]
    assert [w.window.app_id for w in waits] == ["org.x.Editor", "org.x.Editor.Dialog"]
    assert not [e for e in out if isinstance(e, Pause)]
    # The timeout is headroom over what was actually observed, not the gap.
    assert waits[-1].timeout == 10.0
    assert "waited 2.4s here" in waits[-1].note


def test_a_long_wait_gets_a_timeout_scaled_to_what_it_took():
    events = [
        click(1.0),
        Pause(timestamp=1.1, seconds=25.0),
        click(30.0, window=DIALOG),
    ]
    out = infer_synchronization(events)
    assert [w.timeout for w in out if isinstance(w, WaitForWindow)][-1] == 75.0


def test_the_timeout_is_capped_however_long_the_human_took():
    events = [click(1.0), Pause(timestamp=1.1, seconds=600.0), click(602.0, DIALOG)]
    options = SyncOptions(max_timeout=120.0)
    out = infer_synchronization(events, options)
    assert [w.timeout for w in out if isinstance(w, WaitForWindow)][-1] == 120.0


def test_a_pause_before_a_new_element_becomes_a_wait_for_that_element():
    events = [
        click(1.0, element=SAVE),
        Pause(timestamp=1.1, seconds=1.5),
        click(3.0, element=OK),
    ]
    out = infer_synchronization(events)
    waits = [e for e in out if isinstance(e, WaitForElement)]
    assert [w.element.name for w in waits] == ["OK"]
    assert not [e for e in out if isinstance(e, Pause)]


def test_an_element_seen_before_is_not_waited_for_again():
    events = [
        click(1.0, element=SAVE),
        Pause(timestamp=1.1, seconds=1.5),
        click(3.0, element=SAVE),
    ]
    out = infer_synchronization(events, SyncOptions(idle=False))
    assert not [e for e in out if isinstance(e, WaitForElement)]
    assert [e for e in out if isinstance(e, Pause)]


def test_an_unexplained_pause_becomes_wait_for_idle_on_the_window():
    events = [click(1.0), Pause(timestamp=1.1, seconds=3.0), click(5.0)]
    out = infer_synchronization(events)
    idle = [e for e in out if isinstance(e, WaitForIdle)]
    assert len(idle) == 1
    assert idle[0].window is MAIN
    assert idle[0].pid == 11
    assert not [e for e in out if isinstance(e, Pause)]


def test_idle_inference_can_be_declined_since_it_costs_a_capability():
    events = [click(1.0), Pause(timestamp=1.1, seconds=3.0), click(5.0)]
    out = infer_synchronization(events, SyncOptions(idle=False))
    assert not [e for e in out if isinstance(e, WaitForIdle)]
    assert [e.seconds for e in out if isinstance(e, Pause)] == [3.0]


def test_a_pause_with_no_window_at_all_stays_a_sleep():
    bare = Target(x=1, y=2)
    events = [
        Click(timestamp=1.0, target=bare),
        Pause(timestamp=1.1, seconds=3.0),
        Click(timestamp=5.0, target=bare),
    ]
    out = infer_synchronization(events)
    assert [e.seconds for e in out if isinstance(e, Pause)] == [3.0]


def test_a_trailing_pause_has_nothing_to_wait_for_and_stays_a_sleep():
    out = infer_synchronization([click(1.0), Pause(timestamp=2.0, seconds=4.0)])
    assert isinstance(out[-1], Pause)


def test_a_keystroke_is_not_evidence_of_what_the_pause_waited_for():
    # A keystroke's target is wherever the pointer was left resting, which
    # says nothing about what appeared. Believing it names the wrong window.
    events = [
        click(1.0),
        Pause(timestamp=1.1, seconds=2.0),
        KeyStroke(timestamp=3.0, key="Return", target=Target(x=1, y=1, window=DIALOG)),
        click(4.0),
    ]
    out = infer_synchronization(events, SyncOptions(idle=False))
    assert not [e for e in out if isinstance(e, WaitForWindow) and e.note]
    assert [e.seconds for e in out if isinstance(e, Pause)] == [2.0]


def test_typing_does_not_count_as_seeing_a_window_either():
    typed = TextInput(timestamp=2.0, text="hi", target=Target(x=1, y=1, window=DIALOG))
    out = infer_synchronization([click(1.0), typed, click(3.0, window=DIALOG)])
    # The window is announced when a pointer event reaches it, not when a
    # keystroke happened to be resolved against it.
    assert kinds(out).count("WaitForWindow") == 2


def test_inference_is_idempotent_so_re_rendering_cannot_compound():
    events = [
        click(1.0, element=SAVE),
        Pause(timestamp=1.1, seconds=2.4),
        click(3.5, window=DIALOG, element=OK),
    ]
    once = infer_synchronization(events)
    twice = infer_synchronization(events)
    assert kinds(once) == kinds(twice)


def test_the_input_list_is_left_alone():
    events = [click(1.0), Pause(timestamp=1.1, seconds=3.0), click(5.0)]
    before = list(events)
    infer_synchronization(events)
    assert events == before


def test_disabled_returns_the_recording_unchanged():
    events = [click(1.0), Pause(timestamp=1.1, seconds=3.0), click(5.0)]
    assert kinds(infer_synchronization(events, SyncOptions(enabled=False))) == kinds(
        events
    )
