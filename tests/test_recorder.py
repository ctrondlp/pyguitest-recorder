"""Recorder.start()'s own lifecycle: what it opens, and what happens on failure.

Nothing here drives a real X server or pyguitest session -- `_open_session`
and `_open_resolver` are overridden directly so this exercises `start()`'s
own wiring, not the connection logic underneath it (already covered
elsewhere).
"""

from __future__ import annotations

import time
from typing import Any
from unittest import mock

from conftest import FakeResolver
from pyguitest_recorder.analyzer import Normalizer
from pyguitest_recorder.backends import x11 as x11_module
from pyguitest_recorder.backends.base import CaptureUnavailable, RawEvent
from pyguitest_recorder.config import Settings
from pyguitest_recorder.model import ElementRef, Recording
from pyguitest_recorder.recorder import Recorder
from pyguitest_recorder.stopkey import StopKey
from pyguitest_recorder.windows import NullResolver


def _key(kind: str, ts: float, keysym: str = "Escape") -> RawEvent:
    return RawEvent(kind=kind, timestamp=ts, keysym=keysym)


class FakeBackend:
    """A capture backend whose start() fails after being asked to start."""

    name = "fake"

    def __init__(self, fails: bool = False) -> None:
        self.fails = fails
        self.started = False
        self.stopped = False

    def start(self) -> None:
        if self.fails:
            raise CaptureUnavailable("no RECORD extension")
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def events(self):
        return iter(())


class FakeSession:
    """A pyguitest Session stand-in with a close() a test can spy on."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _recorder(session: FakeSession | None) -> Recorder:
    settings = Settings(window_context=False, element_context=False)
    recorder = Recorder(settings=settings)
    recorder._open_session = mock.Mock(return_value=session)  # type: ignore[method-assign]
    recorder._open_resolver = mock.Mock(return_value=NullResolver())  # type: ignore[method-assign]
    return recorder


def test_a_successful_start_opens_the_session_and_the_backend() -> None:
    session = FakeSession()
    backend = FakeBackend(fails=False)
    recorder = _recorder(session)
    with mock.patch("pyguitest_recorder.recorder.choose_backend", return_value=backend):
        recorder.start()
    assert backend.started
    assert recorder._session is session
    assert not session.closed


def test_the_session_is_not_leaked_when_the_backend_fails_to_start() -> None:
    # Live-caught by a repo-wide bug audit, not live testing: the session
    # this opens is already real by the time backend.start() runs (RECORD
    # missing, the second display connection refused, the record thread
    # failing) -- without cleanup here, nothing else ever closes it, since
    # the CLI's own except CaptureUnavailable: return 1 never reaches
    # Recorder.stop() (that only runs in the finally around Recorder.run(),
    # which start() failing never lets the caller reach).
    session = FakeSession()
    backend = FakeBackend(fails=True)
    recorder = _recorder(session)
    with mock.patch("pyguitest_recorder.recorder.choose_backend", return_value=backend):
        try:
            recorder.start()
        except CaptureUnavailable:
            pass
        else:
            raise AssertionError("expected CaptureUnavailable")
    assert session.closed
    assert backend.stopped


class TestStopProgress:
    """`on_stop_progress`, and the interval that decides when a run resets.

    Exercises `_stop_sequence` directly -- it only reads `self.settings` and the
    recogniser those settings build, so a bare `Recorder` needs no backend or
    session at all to test the stop-key state machine in isolation. The machine
    itself is `StopKey`, which the capture backend runs over the same stream;
    it is tested in test_stopkey.
    """

    def _recorder(self, **settings_kwargs):
        return Recorder(settings=Settings(**settings_kwargs))

    def test_fires_on_a_press_that_does_not_yet_complete_the_run(self):
        calls = []
        recorder = self._recorder()
        recorder.on_stop_progress = lambda got, needed: calls.append((got, needed))
        recorder._stop_sequence(_key("key_press", 0.0))
        assert calls == [(1, 2)]

    def test_does_not_fire_on_the_press_that_completes_the_run(self):
        calls = []
        recorder = self._recorder()
        recorder.on_stop_progress = lambda got, needed: calls.append((got, needed))
        recorder._stop_sequence(_key("key_press", 0.0))
        recorder._stop_sequence(_key("key_release", 0.05))
        _, stop = recorder._stop_sequence(_key("key_press", 0.1))
        assert stop is True
        assert calls == [(1, 2)]

    def test_does_not_fire_on_a_release(self):
        calls = []
        recorder = self._recorder()
        recorder.on_stop_progress = lambda got, needed: calls.append((got, needed))
        recorder._stop_sequence(_key("key_press", 0.0))
        calls.clear()
        recorder._stop_sequence(_key("key_release", 0.05))
        assert calls == []

    def test_fires_again_after_a_too_slow_reset_starts_a_new_run(self):
        # Seen live: a first press outside the interval does not just vanish
        # silently -- it starts a fresh run of its own, which should also
        # tell the user their (now first) press registered.
        calls = []
        recorder = self._recorder(stop_key_interval=1.0)
        recorder.on_stop_progress = lambda got, needed: calls.append((got, needed))
        recorder._stop_sequence(_key("key_press", 0.0))
        recorder._stop_sequence(_key("key_release", 0.05))
        calls.clear()
        recorder._stop_sequence(_key("key_press", 2.0))
        assert calls == [(1, 2)]

    def test_none_is_the_default_and_nothing_calls_it(self):
        recorder = self._recorder()
        assert recorder.on_stop_progress is None
        # Must not raise with no listener attached.
        recorder._stop_sequence(_key("key_press", 0.0))

    def test_reflects_a_custom_stop_key_presses_setting(self):
        calls = []
        recorder = self._recorder(stop_key_presses=3)
        recorder.on_stop_progress = lambda got, needed: calls.append((got, needed))
        recorder._stop_sequence(_key("key_press", 0.0))
        assert calls == [(1, 3)]


class TestOnCheck:
    """`on_check`, fired the moment an Assertion is added to the recording."""

    def _recorder_with_normalizer(self, resolver) -> Recorder:
        recorder = Recorder(settings=Settings())
        recorder._normalizer = Normalizer(resolver=resolver, started=0.0)
        return recorder

    def _check_chord(self):
        return [
            RawEvent(kind="key_press", timestamp=1.0, keysym="Control_L", x=10, y=10),
            RawEvent(kind="key_press", timestamp=1.05, keysym="1", x=10, y=10),
        ]

    def test_fires_for_a_recorded_check(self):
        calls = []
        resolver = FakeResolver(
            elements=[((0, 0, 100, 100), ElementRef(role="label", name="Status"))],
            text="Saved",
        )
        recorder = self._recorder_with_normalizer(resolver)
        recorder.on_check = calls.append
        recorder._consume(self._check_chord())
        assert len(calls) == 1
        assert calls[0].check == "text"
        assert calls[0].expected == "Saved"

    def test_does_not_fire_for_an_ordinary_keystroke(self):
        calls = []
        recorder = self._recorder_with_normalizer(FakeResolver())
        recorder.on_check = calls.append
        recorder._consume(
            [RawEvent(kind="key_press", timestamp=1.0, keysym="a", text="a", x=0, y=0)]
        )
        recorder._consume([RawEvent(kind="key_release", timestamp=1.05, keysym="a")])
        assert calls == []

    def test_none_is_the_default_and_nothing_calls_it(self):
        recorder = self._recorder_with_normalizer(FakeResolver())
        assert recorder.on_check is None
        # Must not raise with no listener attached.
        recorder._consume(self._check_chord())


def test_stop_key_interval_default_is_loosened_from_the_original_1_0():
    # 1.0 measured live as too tight for a natural press-pause-press cadence
    # with no feedback that the first press registered -- a real capture
    # showed a 1.333s gap between two presses meant as one deliberate run.
    assert Settings().stop_key_interval == 2.0


def test_check_key_default_moved_off_a_bare_function_key():
    # ctrl+F1 measured live as unusable on a real laptop: its F1 sends the
    # XF86_AudioMute hardware keysym instead of the literal F1 the matcher
    # looks for, so the check silently never fires.
    assert Settings().check_key == "ctrl+1"


def test_announce_checks_defaults_on():
    assert Settings().announce_checks is True


class _SilentBackend:
    """A capture backend that hands over a fixed stream and nothing else.

    The three fakes below differ only in how the stream ends and in what the run
    is still owed when it does, so the parts that are the same live here: a
    name, a no-op lifecycle, and a `drain` that reports having been asked.
    """

    name = "fake"

    def __init__(self, *events: RawEvent) -> None:
        self._events = list(events)
        self.drained = False

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def events(self):
        return iter(self._events)

    def drain(self):
        self.drained = True
        return iter(())


class InterruptedBackend(_SilentBackend):
    """A capture backend interrupted while its queue still held events.

    The first event is what the recording loop had seen; the rest is everything
    capture had already delivered but not yet handed over -- which is the state
    a run that fell behind live input is in.
    """

    def events(self):
        delivered, self._events = self._events[0], self._events[1:]
        yield delivered
        raise KeyboardInterrupt

    def drain(self):
        self.drained = True
        return iter(self._events)


class InterruptedTwiceBackend(InterruptedBackend):
    """A capture backend interrupted again while its tail is being collected.

    The first tail event is delivered and the interrupt lands before the next,
    which is where an impatient Ctrl-C lands on a long backlog.
    """

    def drain(self):
        self.drained = True
        yield self._events[0]
        raise KeyboardInterrupt


class NoDrainBackend:
    """A capture backend that predates `drain`: interrupted, and nothing more."""

    name = "fake"

    def __init__(self, *events: RawEvent) -> None:
        self._events = list(events)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def events(self):
        yield self._events[0]
        raise KeyboardInterrupt


class StopKeyBackend(_SilentBackend):
    """A capture backend whose own events end the recording."""


class StoppedBackend(StopKeyBackend):
    """A capture backend that recognised the stop chord itself.

    `stop_pressed_at` is what such a backend sets when it ends the stream where
    the chord completed -- the moment the recording's own note is written from.
    """

    def __init__(self, stopped_at: float | None, *events: RawEvent) -> None:
        super().__init__(*events)
        self.stop_pressed_at = stopped_at


def _start(backend: Any) -> Recorder:
    """Start a recorder on `backend`, with no session behind it."""
    recorder = _recorder(FakeSession())
    with mock.patch("pyguitest_recorder.recorder.choose_backend", return_value=backend):
        recorder.start()
    return recorder


def _run(backend: Any) -> Recording:
    """Run one recording against `backend`, with no session behind it."""
    return _start(backend).run()


class TestTheCollectedTail:
    """What an interrupted run does with input capture already delivered."""

    def test_an_interrupted_run_records_what_was_already_captured(self):
        # Live: 39s of wall clock, 209 motions consumed, and every keystroke
        # still in the queue when the run ended -- so the script held none of
        # them. An interrupted run is one the user was still making, so what
        # capture already delivered belongs in it.
        backend = InterruptedBackend(
            RawEvent(kind="key_press", timestamp=1.0, keysym="a", text="a"),
            RawEvent(kind="key_press", timestamp=1.1, keysym="b", text="b"),
            RawEvent(kind="key_press", timestamp=1.2, keysym="c", text="c"),
        )
        recording = _run(backend)
        assert backend.drained
        assert [type(e).__name__ for e in recording.events] == ["TextInput"]
        assert recording.events[0].text == "abc"

    def test_a_stop_press_in_the_tail_still_ends_the_run(self):
        # The escape presses that could not be answered while the recorder was
        # behind are in that queue too. Collected the same way as live input
        # they end the recording, and they are the stop rather than keystrokes
        # in the script -- which is what a hundred of them, typed at a recorder
        # that was behind, would otherwise have become.
        backend = InterruptedBackend(
            RawEvent(kind="key_press", timestamp=1.0, keysym="a", text="a"),
            RawEvent(kind="key_press", timestamp=1.1, keysym="Escape"),
            RawEvent(kind="key_press", timestamp=1.2, keysym="Escape"),
            RawEvent(kind="key_press", timestamp=1.3, keysym="z", text="z"),
        )
        recording = _run(backend)
        assert [type(e).__name__ for e in recording.events] == ["TextInput"]
        assert recording.events[0].text == "a"

    def test_a_run_stopped_with_the_key_collects_nothing_after_it(self):
        # Once the stop key has been answered the queue holds input made
        # *after* the user asked to stop, which is not part of the recording.
        backend = StopKeyBackend(
            RawEvent(kind="key_press", timestamp=1.0, keysym="Escape"),
            RawEvent(kind="key_press", timestamp=1.1, keysym="Escape"),
        )
        _run(backend)
        assert not backend.drained

    def test_a_press_held_when_the_stream_ends_is_reported_as_a_keystroke(self):
        # The CLI's "presses were recorded as keystrokes, not as a stop" line is
        # built from this count, and a press still held when the run ended some
        # other way went into the script without it.
        backend = StopKeyBackend(
            RawEvent(kind="key_press", timestamp=1.0, keysym="Escape")
        )
        recorder = _start(backend)
        recording = recorder.run()
        assert [type(e).__name__ for e in recording.events] == ["KeyStroke"]
        assert recorder.unstopped_presses == 1

    def test_a_second_interrupt_gives_up_the_tail_but_keeps_the_recording(self):
        # Working through a tail costs what consuming anything costs, which on a
        # recording that fell behind is why the tail is long -- so the wait is
        # one a user cuts short by pressing Ctrl-C again. That used to raise out
        # of `run` past everything it had collected: the whole recording lost
        # to an interrupt made while it was being saved.
        backend = InterruptedTwiceBackend(
            RawEvent(kind="key_press", timestamp=1.0, keysym="a", text="a"),
            RawEvent(kind="key_press", timestamp=1.1, keysym="b", text="b"),
            RawEvent(kind="key_press", timestamp=1.2, keysym="c", text="c"),
        )
        recording = _run(backend)
        assert [type(e).__name__ for e in recording.events] == ["TextInput"]
        assert recording.events[0].text == "ab"
        note = " ".join(recording.environment.notes)
        assert "interrupted a second time" in note
        assert "end of this recording is missing" in note

    def test_a_backend_with_no_drain_still_gives_up_its_recording(self):
        # The protocol asks for one, but a backend that lacks it -- which the
        # Windows one did when this was written -- must not turn an ordinary
        # Ctrl-C into an AttributeError raised after the recording was made.
        backend = NoDrainBackend(
            RawEvent(kind="key_press", timestamp=1.0, keysym="a", text="a")
        )
        recording = _run(backend)
        assert [type(e).__name__ for e in recording.events] == ["TextInput"]
        assert recording.events[0].text == "a"


class TestFallingBehind:
    """`on_lag`, and the note the recording carries whether or not one is set."""

    def test_says_nothing_while_the_recorder_is_in_step(self):
        calls: list[float] = []
        recorder = Recorder(settings=Settings())
        recorder.on_lag = calls.append
        recorder._note_lag(_key("key_press", time.monotonic()))
        assert calls == []
        assert recorder._worst_lag == 0.0

    def test_reports_how_far_behind_it_is(self):
        calls: list[float] = []
        recorder = Recorder(settings=Settings())
        recorder.on_lag = calls.append
        recorder._note_lag(_key("key_press", time.monotonic() - 30.0))
        assert len(calls) == 1
        assert 29.0 < calls[0] < 31.0
        assert recorder._worst_lag >= 30.0

    def test_does_not_report_once_per_event(self):
        # A recorder minutes behind would otherwise print for every event it
        # finally reaches, which is the wall of text it most needs not to be.
        calls: list[float] = []
        recorder = Recorder(settings=Settings())
        recorder.on_lag = calls.append
        for _ in range(5):
            recorder._note_lag(_key("key_press", time.monotonic() - 30.0))
        assert len(calls) == 1

    def test_none_is_the_default_and_nothing_calls_it(self):
        recorder = Recorder(settings=Settings())
        assert recorder.on_lag is None
        # Must not raise with no listener attached.
        recorder._note_lag(_key("key_press", time.monotonic() - 30.0))
        assert recorder._worst_lag >= 30.0

    def test_the_recording_says_so_even_with_no_listener(self):
        # Whoever reads the generated script later is the one who needs to
        # know its end may be missing, and they were not at the terminal.
        backend = InterruptedBackend(
            RawEvent(
                kind="key_press",
                timestamp=time.monotonic() - 30.0,
                keysym="a",
                text="a",
            )
        )
        recording = _run(backend)
        assert [n for n in recording.environment.notes if "behind live input" in n]

    def test_a_recording_in_step_carries_no_such_note(self):
        backend = InterruptedBackend(
            RawEvent(kind="key_press", timestamp=time.monotonic(), keysym="a", text="a")
        )
        recording = _run(backend)
        assert not [n for n in recording.environment.notes if "behind live input" in n]

    def test_a_run_that_ended_at_the_stop_key_says_what_is_actually_missing(self):
        # The distinction the note exists to draw. A run ended by the stop key
        # has everything before that press in it -- the backend ends the stream
        # there -- so what is missing is what was done *while waiting* for the
        # recorder to catch up to the press, which is exactly what a stop that
        # appears not to work tempts someone into doing.
        behind = time.monotonic() - 30.0
        backend = StoppedBackend(
            behind,
            RawEvent(kind="key_press", timestamp=behind, keysym="a", text="a"),
        )
        recording = _run(backend)
        note = " ".join(recording.environment.notes)
        assert "ends at the press itself" in note
        assert "may be missing" not in note

    def test_a_run_ended_some_other_way_while_behind_still_warns(self):
        # No press to bound it: whatever was still queued when the run ended is
        # only as complete as the drain could collect, and that is worth saying.
        behind = time.monotonic() - 30.0
        backend = InterruptedBackend(
            RawEvent(kind="key_press", timestamp=behind, keysym="a", text="a")
        )
        recording = _run(backend)
        note = " ".join(recording.environment.notes)
        assert "may be missing" in note
        assert "ends at the press itself" not in note


def test_capture_is_given_the_stop_key_the_settings_ask_for():
    # Two recognisers, one implementation: capture ends the stream at the chord
    # the consumer ends the recording on, and it has to be the same chord, from
    # the same settings -- otherwise a recording stops in one place and claims
    # to have stopped in another.
    from pyguitest_recorder.recorder import choose_backend

    seen: dict[str, Any] = {}
    with (
        mock.patch.object(x11_module, "unavailable_reason", return_value=None),
        mock.patch.object(
            x11_module, "X11CaptureBackend", side_effect=lambda **kw: seen.update(kw)
        ),
    ):
        # The backend is named because "auto" is a platform decision: on native
        # Windows it is always win32, never xrecord, so this test built nothing
        # for it to look at there and failed with a KeyError on the one CI job
        # that runs on that platform. Naming it asks for exactly the backend
        # under test, on every platform.
        choose_backend(
            Settings(
                backend="xrecord",
                stop_key="Pause",
                stop_key_presses=1,
                stop_key_interval=0.5,
            )
        )
    assert isinstance(seen["stop_key"], StopKey)
    recogniser = seen["stop_key"]
    assert (recogniser.chord, recogniser.presses, recogniser.interval) == (
        "Pause",
        1,
        0.5,
    )


class TestWindowsEnvironmentSnapshot:
    """X11 facts must not describe a Windows recording.

    An X server (Xming, VcXsrv) or WSLg sets `DISPLAY` -- WSLg sets
    `WAYLAND_DISPLAY` beside it -- on a machine whose capture backend is
    `win32` and whose recording contains no X client at all. Left alone that
    put a foreign display into the session file and an "recorded through
    XWayland" note into a recording made entirely of native Windows input.
    """

    def _describe(self, platform, variables):
        from pyguitest_recorder import recorder as recorder_module

        with (
            mock.patch.object(recorder_module.sys, "platform", platform),
            mock.patch.dict(recorder_module.os.environ, variables, clear=False),
        ):
            return recorder_module.describe_environment(None, "win32")

    def test_a_stray_display_is_not_recorded_on_windows(self):
        env = self._describe("win32", {"DISPLAY": ":0"})
        assert env.display == ""

    def test_an_x_server_plus_wslg_does_not_claim_xwayland_on_windows(self):
        env = self._describe("win32", {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"})
        assert env.xwayland is False
        assert not any("XWayland" in note for note in env.notes)

    def test_the_same_pair_still_means_xwayland_off_windows(self):
        env = self._describe("linux", {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"})
        assert env.display == ":0"
        assert env.xwayland is True


class TestInjectedInputIsReported:
    """Input another process synthesised is marked, not silently included.

    `RawEvent.injected` comes from `LLKHF_INJECTED`, which XRecord has no
    equivalent of. The backend reads it and, until now, it went no further
    than the raw log -- which is off by default -- so a keystroke nobody
    pressed reached the script looking exactly like one that was. A real
    case: a keep-awake script sending `{F15}` once a minute.
    """

    def _note_for(self, keysyms):
        from pyguitest_recorder.recorder import _injected_note

        return _injected_note(keysyms)

    def test_the_note_names_the_key_and_the_count(self):
        note = self._note_for(["F15", "F15", "F15"])
        assert "3 keystroke" in note
        assert "F15" in note
        assert "injected" in note

    def test_repeated_keys_are_named_once(self):
        assert self._note_for(["F15", "F15"]).count("F15") == 1

    def test_many_distinct_keys_are_truncated(self):
        note = self._note_for(["F13", "F14", "F15", "F16", "F17", "F18", "F19"])
        assert "..." in note

    def test_only_injected_key_presses_are_counted(self):
        # An injected pointer move is what a screen-sharing or remote-control
        # tool does constantly; a note firing on every recording made over RDP
        # would be noise rather than a finding.
        from pyguitest_recorder.backends.base import RawEvent
        from pyguitest_recorder.config import Settings
        from pyguitest_recorder.recorder import Recorder

        made = Recorder(Settings())
        for raw in (
            RawEvent(kind="key_press", timestamp=1.0, keysym="F15", injected=True),
            RawEvent(kind="key_press", timestamp=2.0, keysym="a", injected=False),
            RawEvent(kind="motion", timestamp=3.0, x=1, y=1, injected=True),
        ):
            made._note_injected(raw)
        assert made._injected_keys == ["F15"]
