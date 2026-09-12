"""What each recorded pause was actually waiting for.

The rules under test are the recorder's whole reason to exist: a script that
sleeps for as long as the user did is a macro, and a macro is slow when the
machine is fast and broken when it is slow.
"""

from pyguitest_recorder.analyzer import SyncOptions, infer_synchronization
from pyguitest_recorder.model import (
    Assertion,
    Click,
    Drag,
    ElementRef,
    KeyStroke,
    Pause,
    Recording,
    Target,
    TextInput,
    WaitForElement,
    WaitForIdle,
    WaitForWindow,
    WindowActivate,
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


def test_the_timeout_is_capped_however_long_the_user_took():
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


# -- window activation -------------------------------------------------------
# Nothing in the recorder ever built a WindowActivate: the model defined it,
# this analyzer read it and the generator rendered it, and no code path
# produced one. A recording spanning two windows replayed into whichever one
# happened to have focus.


def test_going_back_to_an_open_window_raises_it():
    events = [
        click(1.0, window=MAIN),
        click(2.0, window=DIALOG),
        click(3.0, window=MAIN),
    ]
    out = infer_synchronization(events)
    raised = [e for e in out if isinstance(e, WindowActivate)]
    assert [e.window.app_id for e in raised] == ["org.x.Editor"]
    # Before the click that needed it, not after.
    assert out.index(raised[0]) < out.index(events[2])


def test_a_window_seen_for_the_first_time_is_waited_for_not_raised():
    # It has just appeared, so it already has focus; raising it as well is
    # noise on the commonest path there is, a dialog opening.
    out = infer_synchronization([click(1.0, window=MAIN), click(2.0, window=DIALOG)])
    assert not [e for e in out if isinstance(e, WindowActivate)]
    assert len([e for e in out if isinstance(e, WaitForWindow)]) == 2


def test_staying_in_one_window_raises_nothing():
    out = infer_synchronization([click(1.0), click(2.0), click(3.0)])
    assert not [e for e in out if isinstance(e, WindowActivate)]


def test_a_drag_ending_in_another_window_does_not_make_the_next_click_a_return():
    # Found live on KDE. A press-drag-to-scroll inside the Kickoff menu began
    # inside the Xwayland Video Bridge's rectangle (the capture apparatus,
    # which happened to sit under the pointer) and ended below it on the
    # desktop -- Kickoff itself is a native-Wayland popup with no X11 window,
    # so hit-testing answers with whatever is behind it. Tracking the drag's
    # start left the following click looking like a return to the desktop,
    # which raised the desktop and dismissed the very menu the rest of the
    # script went on to click in: the recording replayed the scroll correctly
    # and then clicked on bare desktop.
    events = [
        click(1.0, window=MAIN),
        Drag(
            timestamp=2.0,
            start=Target(x=652, y=602, window=DIALOG),
            end=Target(x=653, y=927, window=MAIN),
        ),
        click(3.0, window=MAIN),
    ]
    out = infer_synchronization(events)
    assert not [e for e in out if isinstance(e, WindowActivate)]


def test_a_drag_between_two_windows_still_lets_the_next_click_raise_its_own():
    # The other half of the rule above: refusing to guess where an ambiguous
    # drag left the recording must not cost a genuine drag-and-drop its raise
    # -- the click that acts in the drop target still asks for one.
    events = [
        click(1.0, window=MAIN),
        click(1.5, window=DIALOG),
        click(1.8, window=MAIN),
        Drag(
            timestamp=2.0,
            start=Target(x=10, y=10, window=MAIN),
            end=Target(x=20, y=20, window=DIALOG),
        ),
        click(3.0, window=DIALOG),
    ]
    raised = [e for e in infer_synchronization(events) if isinstance(e, WindowActivate)]
    assert [e.window.app_id for e in raised] == [
        "org.x.Editor",
        "org.x.Editor.Dialog",
    ]


def test_a_drag_inside_one_window_still_tracks_that_window():
    # An unambiguous drag is unchanged: it still counts as acting in its
    # window, so a click back in another one is still a return.
    events = [
        click(1.0, window=MAIN),
        click(1.5, window=DIALOG),
        Drag(
            timestamp=2.0,
            start=Target(x=10, y=10, window=DIALOG),
            end=Target(x=20, y=20, window=DIALOG),
        ),
        click(3.0, window=MAIN),
    ]
    raised = [e for e in infer_synchronization(events) if isinstance(e, WindowActivate)]
    assert [e.window.app_id for e in raised] == ["org.x.Editor"]


def test_activation_can_be_declined():
    events = [click(1.0, MAIN), click(2.0, DIALOG), click(3.0, MAIN)]
    out = infer_synchronization(events, SyncOptions(activation=False))
    assert not [e for e in out if isinstance(e, WindowActivate)]


def test_a_window_with_no_identity_is_never_raised():
    bare = WindowRef()
    events = [click(1.0, MAIN), click(2.0, bare), click(3.0, MAIN)]
    out = infer_synchronization(events)
    assert all(e.window.addressable for e in out if isinstance(e, WindowActivate))


def test_typing_does_not_count_as_switching_windows():
    # A text run is anchored to the last clicked field, so believing its target
    # is a window change would raise a window the user never went back to.
    events = [
        click(1.0, window=MAIN),
        click(2.0, window=DIALOG),
        TextInput(timestamp=3.0, text="hi", target=Target(x=1, y=1, window=MAIN)),
        click(4.0, window=DIALOG),
    ]
    out = infer_synchronization(events)
    assert not [e for e in out if isinstance(e, WindowActivate)]


def test_the_raise_survives_into_the_generated_script():
    from pyguitest_recorder.generator import generate, validate

    events = [click(1.0, MAIN), click(2.0, DIALOG), click(3.0, MAIN)]
    recording = Recording(events=infer_synchronization(events))
    source = generate(recording)
    # focus_window, not activate_window: the raise confirms it took, and is
    # emitted only here, where the recording says the person switched.
    assert "gui.focus_window(" in source
    assert validate(source) == []


def test_a_pause_before_a_new_window_does_not_also_raise_it():
    # The pause rule marks the window seen so the announcement rule does not
    # duplicate its wait. Activation must not read that as "came back to":
    # the recording has not acted in that window yet.
    events = [
        click(1.0, window=MAIN),
        Pause(timestamp=1.1, seconds=2.0),
        click(4.0, window=DIALOG),
    ]
    out = infer_synchronization(events)
    assert [type(e).__name__ for e in out].count("WindowActivate") == 0
    assert any(isinstance(e, WaitForWindow) for e in out)


def test_going_back_after_a_pause_still_raises():
    events = [
        click(1.0, window=MAIN),
        Pause(timestamp=1.1, seconds=2.0),
        click(4.0, window=DIALOG),
        click(5.0, window=MAIN),
    ]
    out = infer_synchronization(events)
    raised = [e for e in out if isinstance(e, WindowActivate)]
    assert [e.window.app_id for e in raised] == ["org.x.Editor"]


# -- checks as evidence ------------------------------------------------------


def assertion(t, window=MAIN, element=None, check="showing", x=10, y=10):
    return Assertion(
        timestamp=t,
        check=check,
        target=Target(x=x, y=y, window=window, element=element),
    )


def test_a_pause_before_a_check_waits_for_what_is_being_checked():
    # "Click Save, wait for the dialog, verify what it says" is the shape a
    # check is most often recorded in, and a sleep there is exactly the
    # flakiness the inference exists to remove.
    events = [
        click(1.0, element=SAVE),
        Pause(timestamp=1.1, seconds=2.4),
        assertion(3.5, window=DIALOG, element=OK),
    ]
    out = infer_synchronization(events)
    waits = [e for e in out if isinstance(e, WaitForWindow)]
    assert [w.window.app_id for w in waits] == ["org.x.Editor", "org.x.Editor.Dialog"]
    assert not [e for e in out if isinstance(e, Pause)]


def test_a_check_in_a_background_window_does_not_raise_it():
    # Verifying something in a window the recording is not acting in is a
    # reasonable thing to record, and raising it would change what the
    # replayed application is looking at.
    events = [
        click(1.0, window=MAIN, element=SAVE),
        click(2.0, window=DIALOG, element=OK),
        assertion(3.0, window=MAIN),
    ]
    out = infer_synchronization(events)
    raised = [e for e in out if isinstance(e, WindowActivate)]
    assert [w.window.app_id for w in raised] == []


def test_a_reactivation_comment_names_the_window_a_reader_would_recognize():
    # `_window_key` prefers the app id because it does not drift, which makes
    # a poor name in prose: "moved back to 'zenity'" for a window every other
    # line calls "Recorder Check".
    dialog = WindowRef(title="Recorder Check", app_id="Zenity", pid=11)
    other = WindowRef(title="Second", app_id="Other", pid=12)
    events = [
        click(1.0, window=dialog, element=SAVE),
        click(2.0, window=other, element=OK),
        click(3.0, window=dialog, element=SAVE),
    ]
    out = infer_synchronization(events)
    raised = [e for e in out if isinstance(e, WindowActivate)]
    assert raised
    assert "Recorder Check" in raised[-1].note
    assert "Zenity" not in raised[-1].note


def test_the_same_element_is_not_waited_for_twice():
    # Two pauses in front of one element produced two identical
    # `wait_for_element` calls, seen in the first recording of a real
    # application: the window rule marked its window seen, the element rule
    # never marked its element.
    events = [
        click(1.0, element=SAVE),
        Pause(timestamp=1.1, seconds=1.5),
        click(3.0, element=OK),
        Pause(timestamp=3.1, seconds=1.5),
        click(5.0, element=OK),
    ]
    out = infer_synchronization(events)
    waits = [e for e in out if isinstance(e, WaitForElement)]
    assert [w.element.name for w in waits] == ["OK"]
