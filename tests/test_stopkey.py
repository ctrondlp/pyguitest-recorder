"""The stop chord, as one implementation shared by capture and consumption.

The state machine is exercised through `Recorder._stop_sequence` in
test_recorder; what is here is what a *second* caller needs from it, now that
the capture backend recognises the same chord over the same stream -- it has to
be able to end the stream where the chord completed, rather than only where the
consumer finally reaches it.
"""

from __future__ import annotations

from pyguitest_recorder.backends.base import RawEvent
from pyguitest_recorder.config import Settings
from pyguitest_recorder.stopkey import Outcome, StopKey


def key(ts: float, keysym: str = "Escape", kind: str = "key_press") -> RawEvent:
    return RawEvent(kind=kind, timestamp=ts, keysym=keysym)


def motion(ts: float) -> RawEvent:
    return RawEvent(kind="motion", timestamp=ts, x=1, y=2)


def test_two_presses_in_a_row_complete_and_are_swallowed():
    stop = StopKey()
    # The first press registers -- that is the moment somebody pressing once is
    # waiting to hear about -- and the second completes the run, which does not.
    assert stop.feed(key(1.0)) == Outcome([], registered=True)
    assert stop.feed(key(1.1)) == Outcome([], stop=True)


def test_one_press_is_handed_back_when_the_stream_moves_on():
    # A single Escape belongs to the application being recorded, so it is held
    # only until something else shows the run is over -- and then it is
    # recorded, in order, ahead of whatever ended it.
    stop = StopKey()
    stop.feed(key(1.0))
    outcome = stop.feed(key(1.2, keysym="a"))
    assert not outcome.stop
    assert [e.keysym for e in outcome.record] == ["Escape", "a"]
    assert stop.passed == 1
    assert stop.pending_presses == 0


def test_the_release_between_two_presses_does_not_break_the_run():
    stop = StopKey()
    stop.feed(key(1.0))
    stop.feed(key(1.05, kind="key_release"))
    assert stop.pending_presses == 1
    assert stop.feed(key(1.1)).stop


def test_a_press_too_slow_to_belong_starts_a_run_of_its_own():
    stop = StopKey(interval=1.0)
    stop.feed(key(0.0))
    outcome = stop.feed(key(5.0))
    assert not outcome.stop
    assert [e.timestamp for e in outcome.record] == [0.0]
    assert stop.passed == 1
    assert stop.pending_presses == 1
    assert stop.feed(key(5.1)).stop


def test_a_completed_run_leaves_nothing_held():
    stop = StopKey()
    stop.feed(key(1.0))
    stop.feed(key(1.1))
    assert stop.pending_presses == 0
    assert stop.release() == []


def test_release_hands_back_a_run_that_never_completed_in_order():
    stop = StopKey()
    press = key(1.0)
    release = key(1.05, kind="key_release")
    stop.feed(press)
    stop.feed(release)
    assert stop.release() == [press, release]
    assert stop.release() == []


def test_a_press_held_when_the_stream_ends_is_counted_as_handed_back():
    # Whatever `release` gives back goes into the recording, so it is a
    # keystroke and not a stop -- and that count is what the CLI's "presses were
    # recorded as keystrokes" line is built from. Releasing without counting
    # left this one press unreported.
    stop = StopKey()
    stop.feed(key(1.0))
    assert stop.release()
    assert stop.passed == 1


def test_a_non_key_event_ends_a_run_and_is_recorded_itself():
    stop = StopKey()
    stop.feed(key(1.0))
    outcome = stop.feed(motion(1.1))
    assert not outcome.stop
    assert [e.kind for e in outcome.record] == ["key_press", "motion"]


def test_a_chord_needs_its_modifier():
    stop = StopKey(chord="ctrl+Escape", presses=1)
    outcome = stop.feed(key(1.0))
    assert not outcome.stop
    assert [e.keysym for e in outcome.record] == ["Escape"]
    stop.feed(key(1.05, keysym="Control_L"))
    assert stop.feed(key(1.1)).stop


def test_an_empty_chord_matches_nothing_at_all():
    # How the key is turned off: nothing is ever swallowed, so every press of it
    # reaches the recording.
    stop = StopKey(chord="", presses=1)
    outcome = stop.feed(key(1.0))
    assert not outcome.stop
    assert [e.keysym for e in outcome.record] == ["Escape"]


def test_one_press_is_enough_where_the_settings_say_so():
    stop = StopKey(chord="Pause", presses=1)
    assert stop.feed(key(1.0, keysym="Pause")) == Outcome([], stop=True)


def test_registered_reports_progress_and_never_the_stop_itself():
    stop = StopKey()
    assert stop.feed(key(1.0)).registered
    assert not stop.feed(key(1.05, kind="key_release")).registered
    assert not stop.feed(key(1.1)).registered


def test_from_settings_reads_the_chord_and_its_timing():
    stop = StopKey.from_settings(
        Settings(stop_key="Pause", stop_key_presses=1, stop_key_interval=0.5)
    )
    assert (stop.chord, stop.presses, stop.interval) == ("Pause", 1, 0.5)
