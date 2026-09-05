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
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from .analyzer import Normalizer, NormalizerOptions
from .backends.base import CaptureBackend, CaptureUnavailable
from .config import Settings
from .model import Environment, Recording
from .windows import ContextResolver, DesktopResolver, NullResolver

__all__ = [
    "Recorder",
    "choose_backend",
    "describe_environment",
    "scoped_environment",
]


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
    try:
        import pyguitest

        environment.recorder_version = _version()
        environment.pyguitest_version = pyguitest.__version__
        detected = pyguitest.detect(env) if env else pyguitest.detect()
        environment.session_type = str(getattr(detected, "session_type", "") or "")
        environment.compositor = str(getattr(detected, "compositor", "") or "")
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

    _backend: CaptureBackend | None = field(default=None, init=False)
    _session: Any = field(default=None, init=False)
    _resolver: ContextResolver = field(default_factory=NullResolver, init=False)
    _normalizer: Normalizer | None = field(default=None, init=False)
    _stopping: bool = field(default=False, init=False)

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
        self._backend.start()

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
                if self._is_stop_key(raw):
                    break
                if self.settings.record_raw:
                    self.recording.raw.append(raw.to_dict())
                for event in self._normalizer.feed(raw):
                    self.recording.add(event)
        except KeyboardInterrupt:
            pass
        for event in self._normalizer.flush():
            self.recording.add(event)
        self._collect_warnings()
        return self.recording

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

    def _is_stop_key(self, raw: Any) -> bool:
        """Whether this event is the panic key that ends the recording.

        A recorder that can only be stopped from its own terminal is a
        recorder you cannot stop while driving another application full
        screen, which is most of the time.
        """
        return (
            bool(self.settings.stop_key)
            and raw.kind == "key_press"
            and raw.keysym == self.settings.stop_key
        )

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
        """Open a pyguitest session for window context, if it was asked for.

        Named `x11` rather than left to selection, because it is the only
        backend that is scoped to a display at all. A compositor backend
        answers over D-Bus and knows nothing about which X server is being
        recorded, so on a Wayland desktop a plain `connect()` returns the
        *compositor's* window list -- observed here, recording a private Xvfb
        and resolving every click onto the editor this was written in. The
        failure is silent and total: every coordinate comes out relative to
        another desktop's window, and the generated script validates clean.

        It is also the right answer where nothing is leaking. XRecord sees X
        clients, so the window list has to be the X server's: a compositor
        lists native Wayland toplevels that can never appear in a recording,
        and reports geometry in scaled logical coordinates that do not match
        the root coordinates XRecord reports.
        """
        if not self.settings.window_context:
            return None
        try:
            import pyguitest

            scoped = scoped_environment(display)
            # The display goes through the environment rather than
            # `backend_options`: pyguitest's x11 factory takes only the
            # environment, so `display_name` -- which `X11Backend.__init__`
            # does accept -- is a TypeError from `connect`. The backend opens
            # `$DISPLAY`, so setting it around the call is what reaches it.
            with _environment(scoped):
                return pyguitest.connect(
                    backend="x11", environment=pyguitest.detect(scoped)
                )
        except Exception as exc:  # noqa: BLE001 - context is optional
            notes.append(
                f"window context off: no pyguitest session on {display or '$DISPLAY'}"
                f" ({exc}); clicks will carry coordinates and no window"
            )
            return None

    def _open_resolver(self) -> ContextResolver:
        """Build the resolver matching the settings and what actually opened."""
        if self._session is None and not self.settings.element_context:
            return NullResolver()
        return DesktopResolver(
            session=self._session, elements=self.settings.element_context
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
            sensitive=self.settings.sensitive,
        )


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
