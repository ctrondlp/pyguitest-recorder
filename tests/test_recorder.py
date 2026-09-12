"""Recorder.start()'s own lifecycle: what it opens, and what happens on failure.

Nothing here drives a real X server or pyguitest session -- `_open_session`
and `_open_resolver` are overridden directly so this exercises `start()`'s
own wiring, not the connection logic underneath it (already covered
elsewhere).
"""

from __future__ import annotations

from unittest import mock

from pyguitest_recorder.backends.base import CaptureUnavailable, RawEvent
from pyguitest_recorder.config import Settings
from pyguitest_recorder.recorder import Recorder
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

    Exercises `_stop_sequence` directly -- it only reads `self.settings` and
    `self._stop_pending`, so a bare `Recorder` needs no backend or session at
    all to test the stop-key state machine in isolation.
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


def test_stop_key_interval_default_is_loosened_from_the_original_1_0():
    # 1.0 measured live as too tight for a natural press-pause-press cadence
    # with no feedback that the first press registered -- a real capture
    # showed a 1.333s gap between two presses meant as one deliberate run.
    assert Settings().stop_key_interval == 2.0
