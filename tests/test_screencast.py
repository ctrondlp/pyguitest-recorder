"""The process-isolated cursor source's fail-safety, without a portal.

The portal + PipeWire half needs an interactive consent click and a real
stream, so it is not unit-testable here; what IS testable, and what matters
for not degrading a recording, is that the parent-side `CursorSource` reads
positions when they come and tolerates a helper that emits garbage, emits
nothing, or dies -- never raising into the recorder.
"""

from __future__ import annotations

import io
import os
import threading

import pytest

from pyguitest_recorder.backends import _screencast as screencast_module
from pyguitest_recorder.backends import _screencast_portal as portal_module
from pyguitest_recorder.backends._screencast import CursorSource, _helper_main


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class _LiveStdout:
    """Some lines, then an open stream: a helper that is still running.

    `_read` clears the position when the stream *ends* -- a dead helper's last
    position is a lie that only gets staler -- so asserting what arrived means
    holding the stream open past the final line, the way a live helper does.
    """

    def __init__(self, text, consumed, hold):
        self._text, self._consumed, self._hold = text, consumed, hold

    def __iter__(self):
        yield from io.StringIO(self._text)
        self._consumed.set()
        self._hold.wait(timeout=5)


def _position_while_streaming(text):
    """The position once `text`'s lines arrived, with the helper still alive."""
    src = CursorSource()
    consumed, hold = threading.Event(), threading.Event()
    src._proc = _FakeProc(_LiveStdout(text, consumed, hold))
    thread = threading.Thread(target=src._read, daemon=True)
    thread.start()
    assert consumed.wait(timeout=5), "the fake stream was never consumed"
    pos = src.position()
    hold.set()
    thread.join(timeout=5)
    return pos


def test_no_position_before_anything_arrives():
    assert CursorSource().position() is None


def test_latest_position_wins():
    assert _position_while_streaming("100 200\n300 400\n") == (300, 400)


def test_garbage_lines_are_ignored():
    # A partial line, a non-numeric line, a wrong-arity line: all skipped,
    # and the last good one stands.
    assert _position_while_streaming("10 20\nnonsense\n1 2 3\n\n30 40\n") == (30, 40)


def test_all_garbage_leaves_no_position():
    assert _position_while_streaming("nope\n\nx y\n") is None


def test_a_dead_helpers_position_is_cleared_not_frozen():
    # The design's own anticipated failure is the helper dying mid-recording
    # (a compositor quirk away from a bad struct offset). Its last position
    # must not stand: `_stamp()` prefers the cursor source unconditionally,
    # so a frozen point would pin every later click, scroll and motion to it
    # for the rest of the recording -- strictly worse than the delta tracking
    # the docstring promises to fall back to.
    src = CursorSource()
    src._proc = _FakeProc(io.StringIO("100 200\n"))
    src._read()  # synchronous: the stream ends, which is the helper dying
    assert src.position() is None


def test_stop_is_safe_with_no_process():
    src = CursorSource()
    src.stop()  # never started -- must not raise
    assert src.position() is None


def test_stop_terminates_the_helper():
    src = CursorSource()
    proc = _FakeProc(io.StringIO(""))
    src._proc = proc
    src.stop()
    assert proc.terminated


class TestPortalControlFlow:
    """`open_cursor_stream`'s orchestration, with the bus + reader stubbed out.

    `_negotiate_portal` needs an interactive consent click and the real reader
    needs a live PipeWire stream, so neither is driven here -- doing so would
    pop a consent dialog mid-test. What is tested is the surrounding plumbing:
    failure is reported (never raised), and the stream fd and the session
    keepalive are always released.
    """

    def test_a_portal_that_yields_nothing_reports_failure(self, monkeypatch):
        # No consent / no portal -> (None, None, None) -> non-zero, and the
        # reader is never reached (there is no fd to read or to close).
        monkeypatch.setattr(
            portal_module, "_negotiate_portal", lambda: (None, None, None)
        )
        reached = []
        monkeypatch.setattr(
            portal_module, "_stream_cursor", lambda *a: reached.append(a) or 0
        )
        assert portal_module.open_cursor_stream(lambda x, y: None) == 1
        assert reached == []

    def test_the_fd_and_keepalive_are_always_released(self, monkeypatch):
        # The portal hands back a live fd and a session keepalive; whatever the
        # reader does, neither may leak out of the helper process.
        read_fd, write_fd = os.pipe()
        closed = []

        class _FakeKeepalive:
            def close(self):
                closed.append(True)

        monkeypatch.setattr(
            portal_module,
            "_negotiate_portal",
            lambda: (read_fd, 42, _FakeKeepalive()),
        )
        monkeypatch.setattr(portal_module, "_stream_cursor", lambda *a: 0)
        try:
            assert portal_module.open_cursor_stream(lambda x, y: None) == 0
            assert closed == [True], "the session keepalive was not closed"
            with pytest.raises(OSError):  # already closed by open_cursor_stream
                os.close(read_fd)
        finally:
            os.close(write_fd)


class TestHelperFailSafety:
    """`_helper_main` is the subprocess entry point and must never raise."""

    def test_a_portal_that_raises_exits_non_zero(self, monkeypatch):
        def boom(_emit):
            raise RuntimeError("no session bus")

        monkeypatch.setattr(portal_module, "open_cursor_stream", boom)
        assert _helper_main() == 1

    def test_a_clean_stream_exit_is_passed_through(self, monkeypatch):
        monkeypatch.setattr(portal_module, "open_cursor_stream", lambda _emit: 0)
        assert _helper_main() == 0

    def test_an_import_failure_exits_non_zero(self, monkeypatch):
        # The import of the portal module is itself guarded: if gi or the module
        # cannot be imported in the subprocess, the helper still exits cleanly.
        monkeypatch.delitem(
            screencast_module.sys.modules,
            "pyguitest_recorder.backends._screencast_portal",
            raising=False,
        )
        import builtins

        real_import = builtins.__import__

        def fail(name, *args, **kwargs):
            if name.endswith("_screencast_portal") or "_screencast_portal" in name:
                raise ImportError("gi missing")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fail)
        assert _helper_main() == 1
