"""Tie capture, context resolution and normalization into one recording.

The orchestration is deliberately thin: the capture backend produces raw
events, the resolver answers what each one pointed at, and the normalizer
decides what they mean. This module owns only the lifecycle and the
environment block.

One hazard is handled here rather than left to the caller. Opening a pyguitest
session composes live backends, and on a Wayland desktop that negotiation can
raise a consent dialog at the user -- in the middle of the recording they just
started, which is both startling and a corrupted recording. So the session is
opened before capture begins, never during, and `window_context = False`
turns it off entirely.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from .analyzer import Normalizer, NormalizerOptions
from .backends.base import CaptureBackend, CaptureUnavailable
from .config import Settings
from .model import Assertion, Environment, Recording
from .stopkey import StopKey
from .windows import ContextResolver, DesktopResolver, NullResolver

__all__ = [
    "ContextReport",
    "Recorder",
    "choose_backend",
    "describe_environment",
    "probe_context",
    "scoped_environment",
    "selected_backend_name",
]


_LAG_WARN_SECONDS = 1.0
"""How far behind live input the recorder may fall before it says so.

Consuming an event is not free -- a window lookup, and for a hover or a click
the element hit-test behind it as well -- so a busy recording consumes slower
than the hand making it. Two things make that worth saying rather than hiding.
The stop key is read off the same stream, so until the backlog has been worked
through a stop press cannot be answered at all, which looks exactly like a stop
key that does not work. And the queue lives only in memory: whatever is still
in it when the run ends is gone.
"""

_LAG_REPORT_INTERVAL = 5.0
"""Seconds between "still behind" reports.

Once per event would be a wall of them on a recording that is behind by
minutes, which is precisely the recording that produces them.
"""


def _injected_note(keysyms: list[str]) -> str:
    """Say which keystrokes came from another process, and how many.

    Named rather than counted alone: a reader who sees `F15` here recognises
    their own keep-awake script instantly, where "3 injected keystrokes" would
    send them looking through the script for something they did.
    """
    seen: list[str] = []
    for keysym in keysyms:
        if keysym not in seen:
            seen.append(keysym)
    named = ", ".join(seen[:5]) + (", ..." if len(seen) > 5 else "")
    return (
        f"{len(keysyms)} keystroke(s) in this recording were injected by "
        f"another process rather than typed ({named}); they are in the script "
        "because this recorder does not drop what it saw, but nothing pressed "
        "them -- a keep-awake or macro tool is the usual source, and stopping "
        "it while recording is the fix"
    )


def _lag_note(lag: float, stopped_at: float | None) -> str:
    """What to say about a recording that fell behind the input it consumed.

    Two endings, and they are not the same story. A run that ended at the stop
    key's own press has everything before that press in it, because the backend
    ends the stream where the chord completed -- so what is missing is what was
    done *after* the press, while the recorder was still catching up to it,
    which is exactly the input a late stop tempts someone into making. A run
    that ended any other way has no such boundary: whatever was still queued
    when it ended is only as complete as `CaptureBackend.drain` could collect,
    and saying so is the whole point of the note.
    """
    if stopped_at is not None:
        return (
            f"the recorder fell {lag:.1f}s behind live input, so the stop key "
            "took about that long to answer: this recording ends at the press "
            "itself, and anything done while waiting for the recorder to catch "
            "up to it is not in this recording"
        )
    return (
        f"the recorder fell {lag:.1f}s behind live input before the run ended, "
        "so the end of this recording may be missing: an event costs a window "
        "lookup to consume, more when it names an element, and only what "
        "capture had already delivered could be collected"
    )


@dataclass
class ContextReport:
    """What a recording made right now would actually be able to say.

    Capture answers "can input be seen at all"; this answers the question that
    decides what the generated script looks like. A recording with neither is
    still a recording, of bare screen coordinates -- which is the outcome most
    worth knowing about *before* spending ten minutes making one.
    """

    windows: bool = False
    """Whether toplevels can be identified, so a coordinate is window-relative."""

    elements: bool = False
    """Whether a click can be named, which is the reason this tool exists."""

    notes: list[str] = field(default_factory=list)
    """Everything that degraded, in the words the recording would carry."""


def probe_context(settings: Settings) -> ContextReport:
    """Open the context a recording would use, report on it, and close it.

    Deliberately the same code path `start()` takes rather than a lighter
    imitation of it: a probe that answers differently from the real thing is
    worse than no probe. Capture is not started, so this takes no input and
    leaves no RECORD context behind.
    """
    recorder = Recorder(settings=settings)
    notes: list[str] = []
    display = settings.display or os.environ.get("DISPLAY", "")
    session = recorder._open_session(display, notes)
    report = ContextReport(notes=notes)
    if session is None:
        return report
    try:
        report.windows = _lists_windows(session)
        resolver = recorder._open_resolver_for(session)
        report.elements = resolver.resolves_elements
        report.notes.extend(w for w in resolver.warnings if w not in report.notes)
    finally:
        with contextlib.suppress(Exception):
            session.close()
    return report


def selected_backend_name(settings: Settings) -> str:
    """Which capture backend these settings select, before it is built.

    Split out of `choose_backend` because a second question depends on this
    answer and must not depend on the *host* instead: whether what is being
    recorded is a native Windows desktop. Windows can run an X server -- Xming,
    VcXsrv, WSLg -- and `backend = "xrecord"` there records X clients, a
    recording whose windows, elements and pid corroboration are all X11's even
    though the process making it is a native Windows one. See `choose_backend`
    for the selection rules themselves.

    Empty for a name this recorder does not know. `choose_backend` is where
    that is refused, with a sentence; the callers here fall back to the host
    platform rather than inventing a second refusal for it.
    """
    if settings.backend == "win32" or (
        settings.backend == "auto" and sys.platform == "win32"
    ):
        return "win32"
    if settings.backend in ("auto", "xrecord"):
        return "xrecord"
    return ""


def _is_xwayland_display(display: str) -> bool | None:
    """Whether `display` is served by XWayland, or None if it cannot be asked.

    A thin wrapper so that `describe_environment` stays importable on a host
    with no X11 extra installed: the capture backend module is only reached
    when there is a display to ask about at all.
    """
    if not display:
        return None
    try:
        from .backends.x11 import is_xwayland
    except ImportError:  # pragma: no cover - the x11 extra is not installed
        return None
    return is_xwayland(display)


def _windows_desktop(backend_name: str) -> bool:
    """Whether a recording through `backend_name` is about a Windows desktop.

    The backend decides, not the host process. `xrecord` records X clients, so
    the desktop it is a recording of is an X11 one whatever machine it runs on;
    `win32` records the native desktop and nothing else. Any other name -- no
    backend yet, or one this recorder does not know -- falls back to the host,
    which is the answer every one of these questions used to be.
    """
    if backend_name == "win32":
        return True
    if backend_name == "xrecord":
        return False
    return sys.platform == "win32"


def choose_backend(settings: Settings) -> CaptureBackend:
    """Pick a capture backend, or explain why none can serve this session.

    `"auto"` prefers the platform's own backend rather than trying every one
    in turn: on native Windows that is always `win32`, never `xrecord`, even
    though an X server genuinely can be present there (Xming, VcXsrv, WSLg).
    Selecting `xrecord` on such a machine would record only the X clients
    drawing into that server -- a recording of a phantom desktop, produced
    with no error at all, which is the same trap a pure Wayland session is
    for this same auto-selection on Linux. Naming a backend explicitly
    bypasses that judgment call and asks for exactly what was named,
    refusing outright on the wrong platform rather than silently degrading
    to whichever backend the platform actually offers.
    """
    name = selected_backend_name(settings)
    if not name:
        raise CaptureUnavailable(f"unknown capture backend {settings.backend!r}")
    if name == "win32":
        return _choose_win32(settings)
    return _choose_xrecord(settings)


def _choose_win32(settings: Settings) -> CaptureBackend:
    """The win32 backend, or explain why this machine cannot offer it."""
    if sys.platform != "win32":
        raise CaptureUnavailable(
            "the win32 backend needs a native Windows process; this is "
            f"{sys.platform!r}"
        )
    from .backends.win32 import Win32CaptureBackend
    from .backends.win32 import unavailable_reason as win32_unavailable_reason

    reason = win32_unavailable_reason()
    if reason is not None:
        raise CaptureUnavailable(reason)
    return Win32CaptureBackend(display=settings.display, screen=settings.screen)


def _choose_xrecord(settings: Settings) -> CaptureBackend:
    """The xrecord backend, or explain why this machine cannot offer it."""
    from .backends.x11 import X11CaptureBackend, unavailable_reason

    reason = unavailable_reason()
    if reason is not None:
        raise CaptureUnavailable(reason)
    # Handed the stop key so capture can end the stream where the chord
    # completed, rather than only where the consumer finally reaches it -- see
    # `stopkey`. It is a second recogniser over the same stream, from the same
    # settings, so the two cannot disagree about what stops a recording.
    return X11CaptureBackend(
        display=settings.display,
        screen=settings.screen,
        stop_key=StopKey.from_settings(settings),
    )


def describe_environment(
    session: Any, backend_name: str, env: dict[str, str] | None = None
) -> Environment:
    """Snapshot the desktop the recording is being made on.

    `env` describes the session actually being *recorded*, which on a Wayland
    desktop is not the ambient one -- see `Recorder._open_session`. Detecting
    against the ambient environment here would put a compositor in the header
    of a recording made on a private X server.
    """
    environment = Environment(capture_backend=backend_name)
    now = datetime.now().astimezone()
    environment.recorded_at = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    try:
        import pyguitest

        environment.recorder_version = _version()
        environment.pyguitest_version = pyguitest.__version__
        detected = pyguitest.detect(env) if env else pyguitest.detect()
        environment.session_type = str(getattr(detected, "session_type", "") or "")
        environment.compositor = str(getattr(detected, "compositor", "") or "")
        environment.desktop = str(getattr(detected, "desktop", "") or "")
    except Exception as exc:  # noqa: BLE001 - detection is diagnostic, not required
        environment.notes.append(f"environment detection failed: {exc}")
    # Both of these are X11 facts, and on a Windows recording neither describes
    # the session even when the variables are set. An X server (Xming, VcXsrv)
    # or WSLg will happily set `DISPLAY` -- and WSLg sets `WAYLAND_DISPLAY`
    # beside it -- on a machine whose capture backend is `win32` and whose
    # recording contains no X client at all. Left alone, that put a foreign
    # display into the session file and, with both variables present, an
    # "recorded through XWayland" note into a recording made entirely of
    # native Windows input.
    #
    # Asked of the backend rather than of `sys.platform`, because on that
    # machine the two are not the same question: `backend = "xrecord"` there
    # records X clients through that same X server, and clearing the display
    # out of *that* recording's header would delete the one fact explaining
    # where its coordinates came from.
    windows = _windows_desktop(backend_name)
    # From the environment this was *handed*, not the ambient one. `env`
    # already carries the display actually being recorded -- `scoped_environment`
    # puts `--display` into it -- and reading `os.environ` instead recorded the
    # developer's own `:0` in the header of a recording made on a private Xvfb,
    # which is the one fact that header exists to carry. It also decides which
    # server the XWayland probe below asks.
    source = env if env is not None else os.environ
    environment.display = "" if windows else source.get("DISPLAY", "")
    # Ask the server being recorded, and only guess where it cannot be asked.
    #
    # Detection alone cannot answer this, because the environment it is handed
    # has had `WAYLAND_DISPLAY` removed on purpose -- see `scoped_environment`,
    # which removes it so that a recording of X clients is not described as a
    # Wayland session. `_classify` needs that variable to say XWAYLAND at all,
    # so every recording made on a real Wayland desktop came back `x11` with
    # `xwayland` false: no note, no header block, and a script that looked
    # exactly like one recorded on Xorg while native Wayland clients had been
    # invisible to it the whole time. `--doctor` said "xwayland" in the same
    # breath, because it detects against the ambient environment instead --
    # two paths disagreeing, with the wrong one going into the file.
    #
    # `is_xwayland` asks the display itself, which is the only thing that can
    # tell a session's own XWayland from a private Xvfb started on that same
    # session -- both have the two variables set, and only the first is an
    # XWayland recording. The environment heuristic stays as the fallback for
    # a host with no python-xlib, where nothing can be asked.
    probed = _is_xwayland_display(environment.display) if not windows else None
    if probed is not None:
        environment.xwayland = probed
    else:
        environment.xwayland = not windows and (
            "XWAYLAND" in environment.session_type.upper()
            or (
                not environment.session_type
                and bool(
                    os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY")
                )
            )
        )
    if environment.xwayland:
        # The session the *recording* is of, said the way a reader of the
        # header expects to see it. Left as detection found it, this read
        # "Recorded on: x11 (mutter, GNOME)" -- a line naming a Wayland
        # compositor and an X11 session in the same breath.
        environment.session_type = "xwayland"
    if environment.xwayland:
        environment.notes.append(
            "recorded through XWayland: native Wayland clients are invisible "
            "to XRecord and nothing they received appears in this recording"
        )
    if session is not None:
        try:
            # A property, not a method. Calling it raised "'CapabilitySet'
            # object is not callable" and the note went into every recording's
            # environment block, where it read as a probe that had failed.
            environment.capabilities = sorted(str(c) for c in session.capabilities)
            # (index, width, height, scale). pyguitest's Screen carries no
            # origin -- this read `s.x` and `s.y` and threw on every desktop
            # there is, so no recording ever had a screen block in it. Scale is
            # kept because it is what says whether the captured coordinates and
            # the compositor's agree.
            environment.screens = [
                (int(s.index), int(s.width), int(s.height), float(s.scale))
                for s in session.screens()
            ]
        except Exception as exc:  # noqa: BLE001
            environment.notes.append(f"capability probe failed: {exc}")
    return environment


@dataclass
class Recorder:
    """Run one recording from start to stop."""

    settings: Settings
    recording: Recording = field(default_factory=Recording)

    on_stop_progress: Callable[[int, int], None] | None = None
    """Called with (presses so far, presses needed) on a stop-key press that
    registers but does not yet complete the run.

    `None` (the default) reports nothing -- `Recorder` itself has no UI
    concerns, matching `unstopped_presses` being read after the fact rather
    than printed here. Seen live: someone presses the stop key once, sees no
    visible effect, and naturally pauses to check before pressing again --
    long enough to exceed `stop_key_interval` and have the first press
    discarded as the recorded application's. A caller wanting to head that
    off in real time (the CLI does) sets this rather than `Recorder` needing
    to know how to print anything.
    """

    on_check: Callable[[Assertion], None] | None = None
    """Called with each `Assertion` the moment it is added to the recording.

    Pressing `check_key` has no other visible effect at all, so without this
    the only way to find out whether a check actually recorded something
    useful is to read the generated script afterward -- and a check that
    silently found nothing under the pointer looks, live, identical to one
    that worked. `None` (the default) reports nothing, matching
    `on_stop_progress`'s reasoning: `Recorder` has no UI concerns of its
    own, so a caller wanting live feedback (the CLI does, by default) sets
    this rather than `Recorder` needing to know how to print anything.
    """

    on_lag: Callable[[float], None] | None = None
    """Called with how many seconds behind live input the recorder is.

    Fired when that crosses `_LAG_WARN_SECONDS`, and then at most once per
    `_LAG_REPORT_INTERVAL` for as long as it stays there. `None` (the default)
    reports nothing, matching `on_stop_progress`: the recording carries the
    fact in its own notes either way, so a caller wanting it live sets this.
    """

    _backend: CaptureBackend | None = field(default=None, init=False)
    _session: Any = field(default=None, init=False)
    _resolver: ContextResolver = field(default_factory=NullResolver, init=False)
    _normalizer: Normalizer | None = field(default=None, init=False)
    _stopping: bool = field(default=False, init=False)
    _consuming: bool = field(default=False, init=False)
    _lifecycle: threading.Lock = field(default_factory=threading.Lock, init=False)
    """Guards `_stopping`, `_consuming`, and the decision each makes about who
    closes the context. `stop` is called from another thread by design -- a
    watchdog, a timer, the command line's interrupt -- so "is a run consuming
    right now?" and "then I will not close this" have to be one indivisible
    step. Read and acted on separately, a `stop` arriving in the gap between
    `run` starting and `run` marking itself as consuming saw `_consuming`
    false, closed the session, and left the run to consume its backlog against
    a dead one: the very failure `_consuming` was added to prevent, in a
    smaller window."""
    """Whether `run` is still working through events. See `stop`.

    `stop` is documented as the way to end a run from another thread, and it
    used to close the resolver and its pyguitest session the moment it was
    called -- while `run` was still consuming whatever capture had already
    delivered. Every event left in that backlog then resolved against a closed
    session, so it kept its coordinates and lost its window and its element:
    the recording came out as bare `gui.move_mouse(...)`/`gui.click()` with no
    `expect_window`, validated clean, and said nothing about what it had lost.
    Reproduced on a live GNOME Shell 51.rc session, where the same gedit
    interaction generated `gui.button("Open").click()` when the consumer was
    allowed to catch up first and a bare coordinate when it was not.
    """
    _stop_key: StopKey = field(init=False)
    """The stop-key recogniser, from the settings this recorder was built with.

    One here and another inside the capture backend, both from the same
    settings and both fed the same stream -- see `stopkey`. Built up front
    rather than on demand because the settings cannot change afterwards:
    `merged` returns a new `Settings` and the caller builds the recorder with
    it, so a recogniser that disagreed with the file would be a bug, not a
    feature.
    """

    _worst_lag: float = field(default=0.0, init=False)
    _injected_keys: list[str] = field(default_factory=list, init=False)
    """Keysyms another process synthesised during this run. See
    `_note_injected`."""
    """The furthest behind live input consumption ever fell during this run."""

    _lag_reported_at: float = field(default=0.0, init=False)
    """When `on_lag` was last called, so it is not called per event."""

    def __post_init__(self) -> None:
        """Build the recogniser this recorder's settings ask for."""
        self._stop_key = StopKey.from_settings(self.settings)

    def __enter__(self) -> Recorder:
        """Open the capture backend and the context session."""
        self.start()
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        """Stop capture and release everything."""
        self.stop()
        return False

    def start(self) -> None:
        """Prepare capture. Raises CaptureUnavailable if this session cannot."""
        self._backend = choose_backend(self.settings)
        display = self.settings.display or os.environ.get("DISPLAY", "")
        # Notes are collected before the environment block replaces it, and
        # carried across. Building it first threw away everything `_open_session`
        # had to say -- including "window context off", the one note that
        # explains why a recording has no windows in it.
        opening: list[str] = []
        self._session = self._open_session(display, opening)
        self._resolver = self._open_resolver()
        # Before capture starts, so that every window already open is known by
        # the title it had before anything was recorded into it -- see
        # `DesktopResolver.prime`.
        self._resolver.prime()
        self.recording.environment = describe_environment(
            self._session, self._backend.name, scoped_environment(display)
        )
        self.recording.environment.notes.extend(opening)
        if isinstance(self._resolver, DesktopResolver):
            self.recording.environment.notes.extend(self._resolver.warnings)
        self._normalizer = Normalizer(
            options=self._normalizer_options(),
            resolver=self._resolver,
            started=_now(),
        )
        try:
            self._backend.start()
        except Exception:
            # The pyguitest session above is already open by this point --
            # X11CaptureBackend.start() failing after it (RECORD missing,
            # the second display connection refused, the thread failing to
            # start) must not leak it. Mirrors that backend's own
            # try/except-then-stop pattern one layer up.
            self.stop()
            raise

    def run(self) -> Recording:
        """Capture until the backend stops, returning the finished recording.

        Blocks. The caller stops it by calling `stop` from another thread, or
        by interrupting -- which is what the command line does, so Ctrl-C ends
        a recording rather than discarding it. A second interrupt, while the
        tail an interrupted run is owed is still being worked through, gives
        that tail up and ends the recording where it stands.
        """
        if self._backend is None or self._normalizer is None:
            raise RuntimeError("start() must be called before run()")
        interrupted = False
        # From here until the last event has been consumed, `stop` leaves the
        # resolver and its session alone -- see `stop`. Everything below still
        # asks them which window and which element an event landed in, and a
        # `stop` from another thread arriving mid-backlog used to answer those
        # questions with nothing at all.
        with self._lifecycle:
            # If a stop got in first it has already closed the context, so
            # this run must not claim it -- the backend is stopped and the
            # loop below will simply find nothing to consume.
            already_stopped = self._stopping
            self._consuming = not already_stopped
        try:
            try:
                for raw in self._backend.events():
                    if self._absorb(raw):
                        break
            except KeyboardInterrupt:
                interrupted = True
            if interrupted:
                self._collect_the_tail()
            # Presses held for a stop run that never completed are the
            # application's, not the recorder's, so they belong in the recording.
            self._consume(self._stop_key.release())
            for event in self._normalizer.flush():
                self.recording.add(event)
        finally:
            with self._lifecycle:
                was_consuming = self._consuming
                self._consuming = False
                # Only where a `stop` has already been asked for and deferred
                # to this run. A run that ended on its own stop key is still
                # owed a `stop` by its caller, and closing the session early
                # here would take the context away from anything that reads the
                # recording afterwards; a run that never claimed the context
                # (a stop that beat it to admission) must not close it twice.
                mine = was_consuming and self._stopping
            if mine:
                self._close_context()
        if self._worst_lag > _LAG_WARN_SECONDS:
            self.recording.environment.notes.append(
                _lag_note(self._worst_lag, self._stop_pressed_at())
            )
        if self._injected_keys:
            self.recording.environment.notes.append(_injected_note(self._injected_keys))
        self._collect_warnings()
        return self.recording

    def _collect_the_tail(self) -> None:
        """Sort in what capture had already delivered when the run was interrupted.

        Nothing has been consumed since the interrupt, so the backend's queue is
        the only copy of it. An interrupted run is one the user was still making,
        so its tail belongs in the recording -- and sorting it in the same way as
        live input means a stop press that could not be answered while the
        recorder was behind still ends the run, instead of landing in the script
        as a hundred keystrokes.

        Working through it costs what consuming anything costs, which on a
        recording that fell behind is the very thing that made the tail long --
        so this is a wait someone will try to cut short. A second interrupt does:
        the recording is worth more than the rest of its tail, and losing the
        whole run to an interrupt raised while saving it is the worst way for it
        to end. It is kept as it stands, and says so.

        A backend with no `drain` has no tail to give. The protocol asks for one,
        but nothing a backend leaves out should cost the recording.
        """
        drain = getattr(self._backend, "drain", None)
        if drain is None:
            return
        try:
            for raw in drain():
                if self._absorb(raw):
                    break
        except KeyboardInterrupt:
            self.recording.environment.notes.append(
                "interrupted a second time while collecting what capture had "
                "already delivered, so the end of this recording is missing"
            )

    @property
    def unstopped_presses(self) -> int:
        """Stop-key presses that were recorded rather than ending the recording.

        Worth telling the user about afterwards. Pressing Escape once when two
        are needed does nothing visible, so the natural response is to press it
        again -- and the first press, having been handed on, is now a keystroke
        in their script. That is the right default (it keeps "press Escape to
        close the dialog" recordable) but it surprises people exactly once, and
        a line at the end costs nothing.
        """
        return self._stop_key.passed

    def _stop_pressed_at(self) -> float | None:
        """When the backend's own stop recognition ended the stream, if it did.

        Read off the backend rather than tracked here, because on a recording
        that fell behind, the press that ended it was *consumed* long after it
        was made -- so what capture saw is the only honest answer about which
        way the run ended. `getattr` because a backend is free not to recognise
        the chord itself, in which case this recogniser is what ends the run and
        the note has to read the other way round.
        """
        return getattr(self._backend, "stop_pressed_at", None)

    def _absorb(self, raw: Any) -> bool:
        """Sort one raw event in, live or collected. True means stop.

        The two sources of events differ only in where they came from, so they
        are sorted the same way: a stop press in the collected tail has to mean
        what it would have meant live, or an interrupted run would hand the
        application's escape presses to the script as keystrokes.
        """
        self._note_lag(raw)
        keep, stop = self._stop_sequence(raw)
        if not stop:
            self._consume(keep)
        return stop

    def _note_injected(self, raw: Any) -> None:
        """Count input that another process synthesised rather than a person.

        `RawEvent.injected` comes from `LLKHF_INJECTED`/`LLMHF_INJECTED`, which
        XRecord has no equivalent of -- Windows is telling this recorder
        something X11 structurally cannot. The backend already reads it, and
        until now it went no further than the raw log, which is off by default:
        so a keystroke no one pressed reached the generated script looking
        exactly like one that was.

        Not dropped, which is this package's rule about everything it saw --
        marked. A real case: a keep-awake script sending `{F15}` once a minute
        put a `gui.tap_key("F15")` in the middle of a recording of the Run
        dialog, and nothing in the script or its notes said where it came from.
        Counting keystrokes only, deliberately: an injected *pointer* move is
        what a screen-sharing or remote-control tool does constantly, and a
        note firing on every recording made over RDP would be noise rather
        than a finding.
        """
        if getattr(raw, "injected", False) and getattr(raw, "kind", "") == "key_press":
            self._injected_keys.append(getattr(raw, "keysym", "") or "?")

    def _note_lag(self, raw: Any) -> None:
        """Notice when consumption has fallen behind the input it is consuming.

        Capture stamps every event with the same monotonic clock, so the age of
        the event in hand *is* the backlog: the recording is that far behind
        the hand that made it. Worth knowing live, because the stop key cannot
        be answered from behind a backlog, and worth keeping afterwards,
        because whatever is still queued when the run ends is lost.
        """
        now = _now()
        lag = now - getattr(raw, "timestamp", now)
        if lag <= _LAG_WARN_SECONDS:
            return
        self._worst_lag = max(self._worst_lag, lag)
        if self.on_lag is None or now - self._lag_reported_at < _LAG_REPORT_INTERVAL:
            return
        self._lag_reported_at = now
        self.on_lag(lag)

    def _consume(self, raws: list[Any]) -> None:
        """Record and normalize raw events that are known not to be the stop key."""
        if self._normalizer is None:
            return
        for raw in raws:
            # Counted here, where a raw event actually enters the recording,
            # and not when it arrives: a press that completes the stop chord
            # is absorbed but never consumed, and the note says these keys
            # are *in the script*. Held presses handed on by
            # `StopKey.release()` come through here too, so they count once
            # they are really recorded.
            self._note_injected(raw)
            if self.settings.record_raw:
                self.recording.raw.append(raw.to_dict())
            for event in self._normalizer.feed(raw):
                self.recording.add(event)
                if self.on_check is not None and isinstance(event, Assertion):
                    self.on_check(event)

    def _collect_warnings(self) -> None:
        """Carry what the resolver learned while recording into the notes.

        Collected again at the end, not only at `start`. The resolver's most
        useful warnings are the ones it can only raise once it has been asked
        about a real point -- a toolkit whose hit-testing does not agree with
        its own extents, an element from a process no window on this display
        owns -- and those all happen after the environment block was built.
        """
        if not isinstance(self._resolver, DesktopResolver):
            return
        notes = self.recording.environment.notes
        notes.extend(w for w in self._resolver.warnings if w not in notes)

    def _stop_sequence(self, raw: Any) -> tuple[list[Any], bool]:
        """Sort one raw event into what to record and whether to stop.

        A recorder that can only be stopped from its own terminal is a
        recorder you cannot stop while driving another application full
        screen, which is most of the time. So the stop key is read here,
        before normalization, and swallowed.

        The state machine itself is `StopKey.feed`, which the capture backend
        runs over the same stream -- see `stopkey`. The default is Escape
        pressed twice, and what that needs beyond matching a keysym is there:
        a single Escape belongs to the application being recorded, so presses
        are held until the run either completes -- and the recording ends,
        discarding them -- or is broken, at which point they are handed on in
        order and recorded like any other key. That is what keeps "press Escape
        to close the dialog" recordable while Escape is also what stops the
        recording. Nothing is held across the end of the stream: `run` releases
        whatever is pending before finishing.

        What lives here is what the answer means for a *recording*, and the one
        thing a stream cannot know: that a press which registered but did not
        complete the run is worth saying out loud, because a single Escape has
        no visible effect and silence reads as the key not working.
        """
        outcome = self._stop_key.feed(raw)
        if outcome.registered:
            self._report_stop_progress()
        return (outcome.record, outcome.stop)

    def _report_stop_progress(self) -> None:
        """Tell `on_stop_progress` how many stop-key presses have registered.

        Only on a press, and never on the one that completes the run: what
        someone waiting to see whether their press was seen wants counted is
        presses, not the release in between them -- and a completed run needs no
        line, because the recording ending is the line.
        """
        if self.on_stop_progress is None:
            return
        needed = max(1, self.settings.stop_key_presses)
        self.on_stop_progress(self._stop_key.pending_presses, needed)

    def stop(self) -> None:
        """Stop capture, and close what `start` opened once `run` is done with it.

        Capture stops immediately -- that is what ending a recording means, and
        what the stop key, the command line's interrupt and any watchdog are
        asking for. The *context* behind it does not, while `run` is still
        consuming: resolving a click to its window and its element is a
        question asked of that pyguitest session at consume time, not at
        capture time, so closing it out from under a consumer that has fallen
        behind silently strips the window and the element off every event still
        in hand. `run` closes it instead when it has finished, and this closes
        it here when there is no run to wait for -- `start`'s own failure path
        being the case that needs that.
        """
        with self._lifecycle:
            if self._stopping:
                return
            self._stopping = True
            # Decided here, while the flags cannot move: a run that is
            # consuming closes the context itself when it is done, and if
            # there is no such run this call owns it.
            mine = not self._consuming
        if self._backend is not None:
            self._backend.stop()
        if mine:
            self._close_context()

    def _close_context(self) -> None:
        """Close the resolver and the pyguitest session behind it.

        Split out of `stop` because two paths reach it: `stop` itself where no
        run is in flight, and `run` once its last event has been consumed.
        Idempotent, since both can happen for one recording.
        """
        self._resolver.close()
        if self._session is not None:
            # Teardown is best effort: a backend that already lost its
            # connection must not stop the recording being saved.
            with contextlib.suppress(Exception):
                self._session.close()
            self._session = None

    # -- setup ---------------------------------------------------------------

    def _on_windows(self) -> bool:
        """Whether what is being recorded is a native Windows desktop.

        From the backend, not from the host -- see `_windows_desktop` -- and
        asked by everything that has to describe or interpret the recording in
        one platform's terms. Before there is a backend, which is
        `probe_context`'s case (it opens the context a recording *would* use
        without starting capture), the settings answer instead, through the
        same selection rules `choose_backend` applies.
        """
        name = getattr(self._backend, "name", "") or selected_backend_name(
            self.settings
        )
        return _windows_desktop(name)

    def _context_backends(self) -> tuple[str, ...]:
        """The pyguitest backends that answer "which window, which element".

        Named per platform rather than probed, because the *pair* is what has
        to compose: the window half must answer in the same coordinate space
        the capture backend reports, and the element half must be the one this
        desktop actually publishes to. On Windows that is `win32` + `uia`;
        everywhere else `x11` + `atspi`. Window first in both, so it keeps
        every window capability when the element backend joins it.

        Which of the two is asked of the capture backend rather than of the
        host -- see `_on_windows`. On a Windows machine running Xming, VcXsrv
        or WSLg with `backend = "xrecord"`, the host answers Windows while the
        recording is of an X server: `win32` for the window half would ask
        pyguitest about a native desktop none of whose windows are in the
        recording, and `uia` for the element half would ask a bus the X11
        clients in it never publish to.
        """
        window, element = ("win32", "uia") if self._on_windows() else ("x11", "atspi")
        wanted = []
        if self.settings.window_context:
            wanted.append(window)
        if self.settings.element_context:
            wanted.append(element)
        return tuple(wanted)

    def _session_locator(self, display: str) -> str:
        """Where the session was looked for, in this platform's own terms.

        `$DISPLAY` is an X11 idea and names nothing on a native Windows
        desktop, where the session is the desktop this process is attached to
        -- so a Windows recording used to carry "no pyguitest session on
        $DISPLAY" into every generated script and session file, sending a
        reader after a variable their machine does not have. The other way
        round, an `xrecord` recording made *on* Windows (Xming, VcXsrv, WSLg)
        is of an X display and says so, which is the whole reason this asks
        the backend rather than the host.
        """
        if self._on_windows():
            return "for this desktop"
        return f"on {display or '$DISPLAY'}"

    def _open_session(self, display: str, notes: list[str]) -> Any:
        """Open the pyguitest session the resolver asks its questions of.

        The backends are named rather than left to selection. `x11` is the
        only one scoped to a display at all: a compositor backend answers
        over D-Bus and knows nothing about which X server is being recorded,
        so on a Wayland desktop a plain `connect()` returns the
        *compositor's* window list -- observed here, recording a private Xvfb
        and resolving every click onto the editor this was written in. The
        failure is silent and total: every coordinate comes out relative to
        another desktop's window, and the generated script validates clean.

        It is also the right answer where nothing is leaking. XRecord sees X
        clients, so the window list has to be the X server's: a compositor
        lists native Wayland toplevels that can never appear in a recording,
        and reports geometry in scaled logical coordinates that do not match
        the root coordinates XRecord reports.

        `atspi` joins it for elements, second so that x11 keeps every window
        capability. Composing the two is what lets one session answer both
        halves of a click; naming a backend that cannot build raises rather
        than being skipped, which is why the shorter lists are tried after.

        **Windows names its own pair**, and every sentence above is about the
        other platform. `win32` is the window half there and `uia` the element
        half -- the same split, through the backends that exist on it. Asking
        for `x11`/`atspi` on Windows cannot succeed: there is no X server for
        the first and no accessibility bus for the second, so the session
        never opened and every click in a Windows recording came out as a bare
        screen coordinate with no window and no element. Confirmed on a real
        Windows 11 recording before this was fixed -- thirteen events, every
        one of them `window: null, element: null`, which is the recorder
        losing the thing it exists to do.
        """
        wanted = list(self._context_backends())
        if not wanted:
            return None
        scoped = scoped_environment(display)
        session, failure = self._connect(wanted, scoped)
        if session is None:
            notes.append(
                f"window and element context off: no pyguitest session "
                f"{self._session_locator(display)} ({failure}); clicks will "
                "carry bare coordinates"
            )
            return None
        if failure is not None:
            notes.append(
                f"opened a smaller context than asked for: connecting "
                f"{' + '.join(wanted)} failed ({type(failure).__name__}: {failure}), "
                "so only part of it is in use -- clicks may lose their element "
                "names. It has been seen to come and go between runs, so "
                "recording again is worth trying"
            )
        if self.settings.window_context and not _lists_windows(session):
            notes.append(
                "window context off: the session that opened lists no windows; "
                "clicks will carry absolute coordinates and no window"
            )
        return session

    def _connect(
        self, wanted: list[str], scoped: dict[str, str]
    ) -> tuple[Any, Exception | None]:
        """Compose `wanted`, then each name alone, returning the first session.

        A recording is worth making with whichever half opened: element
        resolution without window context still names buttons, and window
        context without elements still gives window-relative coordinates.
        The resolver says in the recording's notes which one it lost.

        Returns the session and the failure that came *before* it, if any: the
        error of the first attempt, which is the composed list whenever there
        is more than one name, or None when that attempt worked. With no
        session at all it is the last failure. The first failure is the one
        worth keeping, and not that of whichever singleton failed just before
        the one that opened: the note built from it says the composed list
        failed, so it has to be the composed list's own error. A session that
        opens with less than was asked for is otherwise indistinguishable from
        one that was never going to have more, and the reason is the only clue
        to a drop-out that comes and goes.
        """
        import pyguitest

        failure: Exception | None = None
        first_failure: Exception | None = None
        # The display goes through the environment rather than
        # `backend_options`: pyguitest's x11 factory takes only the
        # environment, so `display_name` -- which `X11Backend.__init__` does
        # accept -- is a TypeError from `connect`. The backends open
        # `$DISPLAY`, so setting it around the call is what reaches them.
        with _environment(scoped):
            for names in ([wanted] if len(wanted) > 1 else []) + [[n] for n in wanted]:
                try:
                    return (
                        pyguitest.connect(
                            backend=names, environment=pyguitest.detect(scoped)
                        ),
                        first_failure,
                    )
                except Exception as exc:  # noqa: BLE001 - context is optional
                    if first_failure is None:
                        first_failure = exc
                    failure = exc
        return (None, failure)

    def _open_resolver(self) -> ContextResolver:
        """Build the resolver matching the settings and what actually opened."""
        if self._session is None:
            return NullResolver()
        return self._open_resolver_for(self._session)

    def _open_resolver_for(self, session: Any) -> DesktopResolver:
        """Build the resolver for one session, so a probe can build the same one.

        The recording's own platform goes with it: the resolver corroborates a
        pid mismatch differently on Windows than on X11 (`is_windows`, which
        reads the host), and an `xrecord` recording made on a Windows machine
        is X11's to interpret. See `_on_windows`.
        """
        return DesktopResolver(
            session=session,
            elements=self.settings.element_context,
            windows=self._on_windows(),
        )

    def _normalizer_options(self) -> NormalizerOptions:
        """Translate settings into the analyzer's thresholds."""
        return NormalizerOptions(
            motion_threshold=self.settings.motion_threshold,
            click_interval=self.settings.click_interval,
            double_click_interval=self.settings.double_click_interval,
            text_idle=self.settings.text_idle,
            pause_threshold=self.settings.pause_threshold,
            record_motion=self.settings.record_motion,
            hover_threshold=self.settings.hover_threshold,
            sensitive=self.settings.sensitive,
            check_key=self.settings.check_key,
        )


def _lists_windows(session: Any) -> bool:
    """Whether this session can say which window a point is in."""
    try:
        return "WINDOW_LIST" in {c.name for c in session.capabilities}
    except Exception:  # noqa: BLE001 - anything unreadable is a no
        return False


@contextlib.contextmanager
def _environment(values: dict[str, str]) -> Iterator[None]:
    """Run a block with `os.environ` replaced, then put it back.

    Backends open their own connections from the environment, so detecting
    against one mapping and connecting under another would pin the selection
    to the right display and the connection to the wrong one.
    """
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def scoped_environment(display: str) -> dict[str, str]:
    """The environment as it looks from the display being recorded.

    `WAYLAND_DISPLAY` goes because a recording made through XRecord contains X
    clients and nothing else, so describing it as a Wayland session would put
    a compositor in the header of a recording that compositor never saw.
    """
    scoped = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
    if display:
        scoped["DISPLAY"] = display
    return scoped


def _version() -> str:
    """This recorder's version, without importing the package eagerly."""
    from . import __version__

    return __version__


def _now() -> float:
    """The recording clock, matching the capture backend's."""
    return time.monotonic()
