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
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from .analyzer import (
    MODIFIERS,
    Normalizer,
    NormalizerOptions,
    chord_matches,
    parse_chord,
)
from .backends.base import CaptureBackend, CaptureUnavailable
from .config import Settings
from .model import Environment, Recording
from .windows import ContextResolver, DesktopResolver, NullResolver

__all__ = [
    "ContextReport",
    "Recorder",
    "choose_backend",
    "describe_environment",
    "probe_context",
    "scoped_environment",
]


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


def choose_backend(settings: Settings) -> CaptureBackend:
    """Pick a capture backend, or explain why none can serve this session."""
    from .backends.x11 import X11CaptureBackend, unavailable_reason

    if settings.backend not in ("auto", "xrecord"):
        raise CaptureUnavailable(f"unknown capture backend {settings.backend!r}")
    reason = unavailable_reason()
    if reason is not None:
        raise CaptureUnavailable(reason)
    return X11CaptureBackend(display=settings.display, screen=settings.screen)


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
    environment.display = os.environ.get("DISPLAY", "")
    # Prefer what pyguitest detected; fall back to the environment only when
    # detection failed, since both variables being set does not by itself
    # prove the target application is an XWayland client.
    environment.xwayland = "XWAYLAND" in environment.session_type.upper() or (
        not environment.session_type
        and bool(os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY"))
    )
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

    _backend: CaptureBackend | None = field(default=None, init=False)
    _session: Any = field(default=None, init=False)
    _resolver: ContextResolver = field(default_factory=NullResolver, init=False)
    _normalizer: Normalizer | None = field(default=None, init=False)
    _stopping: bool = field(default=False, init=False)
    _stop_pending: list[Any] = field(default_factory=list, init=False)
    _mods: set[str] = field(default_factory=set, init=False)
    _stop_passed: int = field(default=0, init=False)
    """Stop-key presses that were handed on rather than ending the run."""

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
        a recording rather than discarding it.
        """
        if self._backend is None or self._normalizer is None:
            raise RuntimeError("start() must be called before run()")
        try:
            for raw in self._backend.events():
                ready, stop = self._stop_sequence(raw)
                if stop:
                    break
                self._consume(ready)
        except KeyboardInterrupt:
            pass
        # Presses held for a stop run that never completed are the
        # application's, not the recorder's, so they belong in the recording.
        self._consume(self._stop_pending)
        self._stop_pending = []
        for event in self._normalizer.flush():
            self.recording.add(event)
        self._collect_warnings()
        return self.recording

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
        return self._stop_passed

    def _consume(self, raws: list[Any]) -> None:
        """Record and normalize raw events that are known not to be the stop key."""
        if self._normalizer is None:
            return
        for raw in raws:
            if self.settings.record_raw:
                self.recording.raw.append(raw.to_dict())
            for event in self._normalizer.feed(raw):
                self.recording.add(event)

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

        The default is Escape pressed twice, which needs more than matching a
        keysym: a single Escape belongs to the application being recorded, so
        presses are held until the run either completes -- and the recording
        ends, discarding them -- or is broken, at which point they are handed
        on in order and recorded like any other key. That is what keeps
        "press Escape to close the dialog" recordable while Escape is also
        what stops the recording. Nothing is held across the end of the
        stream: `run` flushes whatever is pending before finishing.
        """
        held = self._modifiers_after(raw)
        if not self._is_stop_event(raw, held):
            pending, self._stop_pending = self._stop_pending, []
            self._stop_passed += sum(1 for e in pending if e.kind == "key_press")
            return ([*pending, raw], False)
        if self._stop_pending and (
            raw.timestamp - self._stop_pending[-1].timestamp
            > self.settings.stop_key_interval
        ):
            # Too slow to be one run, so the earlier presses were the
            # application's and this one starts a new run of its own.
            pending, self._stop_pending = self._stop_pending, [raw]
            self._stop_passed += sum(1 for e in pending if e.kind == "key_press")
            self._report_stop_progress(raw)
            return (pending, False)
        self._stop_pending.append(raw)
        presses = sum(1 for e in self._stop_pending if e.kind == "key_press")
        if presses >= max(1, self.settings.stop_key_presses):
            self._stop_pending = []
            return ([], True)
        self._report_stop_progress(raw)
        return ([], False)

    def _report_stop_progress(self, raw: Any) -> None:
        """Tell `on_stop_progress` how many stop-key presses have registered.

        Only on a press, not a release: a release enters `_stop_pending` too
        (see `_is_stop_event`), but what someone waiting to see whether their
        press was seen wants counted is presses, not the release in between
        them.
        """
        if self.on_stop_progress is None or raw.kind != "key_press":
            return
        presses = sum(1 for e in self._stop_pending if e.kind == "key_press")
        self.on_stop_progress(presses, max(1, self.settings.stop_key_presses))

    def _is_stop_event(self, raw: Any, held: set[str]) -> bool:
        """Whether this event belongs to a run of stop-key presses.

        Releases count as belonging to the run without advancing it, so the
        release between two presses does not read as the run being broken.
        """
        if raw.kind not in ("key_press", "key_release"):
            return False
        if raw.kind == "key_release":
            _, key = parse_chord(self.settings.stop_key)
            return bool(key) and raw.keysym == key and bool(self._stop_pending)
        return chord_matches(self.settings.stop_key, held, raw.keysym)

    def _modifiers_after(self, raw: Any) -> set[str]:
        """Track which modifiers are down, which the normalizer cannot do for us.

        The stop key is recognized before normalization by design, so its
        modifier state has to be tracked here as well as there.
        """
        modifier = MODIFIERS.get(getattr(raw, "keysym", "") or "")
        if modifier is not None:
            if raw.kind == "key_press":
                self._mods.add(modifier)
            elif raw.kind == "key_release":
                self._mods.discard(modifier)
        return self._mods

    def stop(self) -> None:
        """Stop capture and close everything opened by `start`."""
        if self._stopping:
            return
        self._stopping = True
        if self._backend is not None:
            self._backend.stop()
        self._resolver.close()
        if self._session is not None:
            # Teardown is best effort: a backend that already lost its
            # connection must not stop the recording being saved.
            with contextlib.suppress(Exception):
                self._session.close()
            self._session = None

    # -- setup ---------------------------------------------------------------

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
        """
        wanted = []
        if self.settings.window_context:
            wanted.append("x11")
        if self.settings.element_context:
            wanted.append("atspi")
        if not wanted:
            return None
        scoped = scoped_environment(display)
        session, failure = self._connect(wanted, scoped)
        if session is None:
            notes.append(
                f"window and element context off: no pyguitest session on "
                f"{display or '$DISPLAY'} ({failure}); clicks will carry bare "
                "coordinates"
            )
            return None
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
        """
        import pyguitest

        failure: Exception | None = None
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
                        None,
                    )
                except Exception as exc:  # noqa: BLE001 - context is optional
                    failure = exc
        return (None, failure)

    def _open_resolver(self) -> ContextResolver:
        """Build the resolver matching the settings and what actually opened."""
        if self._session is None:
            return NullResolver()
        return self._open_resolver_for(self._session)

    def _open_resolver_for(self, session: Any) -> DesktopResolver:
        """Build the resolver for one session, so a probe can build the same one."""
        return DesktopResolver(session=session, elements=self.settings.element_context)

    def _normalizer_options(self) -> NormalizerOptions:
        """Translate settings into the analyzer's thresholds."""
        return NormalizerOptions(
            motion_threshold=self.settings.motion_threshold,
            click_interval=self.settings.click_interval,
            double_click_interval=self.settings.double_click_interval,
            text_idle=self.settings.text_idle,
            pause_threshold=self.settings.pause_threshold,
            record_motion=self.settings.record_motion,
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
