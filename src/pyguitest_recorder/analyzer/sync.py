"""Turn dead time into synchronization.

This is the step that separates a recorder from a macro player. A macro player
reproduces the gaps it saw: you waited 2.4 seconds for a dialog, so it sleeps
2.4 seconds. That script is slow when the machine is fast and broken when the
machine is slow, and it is the single reason recorded GUI tests have the
reputation they have.

What the normalizer hands over is honest but useless: a `Pause` wherever the
user stopped for longer than the gap threshold. This module asks, for each of
those, *what were they waiting for* -- and answers it from what the events
themselves saw, since every recorded event carries the window and element that
were under the pointer at the time.

The rules, in the order they are tried:

1. The next action is in a window no earlier event saw. The user was waiting
   for that window to open, so the pause becomes `wait_for_window`.
2. The next action is on an element no earlier event saw, in a window that was
   already open. A dialog filled itself in, a list finished loading: the pause
   becomes `wait_for_element`.
3. Nothing observable changed. The application was most likely busy, so the
   pause becomes `wait_for_idle` on the window's process -- which costs
   nothing when it was not busy, because a process that is already idle
   satisfies the wait immediately. This is the one rule that can be wrong
   about *why* the user waited, and it is the one that fails safe.
4. Nothing above applies, so the sleep stands, and the generator says in a
   comment that it could not do better.

Two deliberate asymmetries. A *window* first seen mid-recording gets a
`wait_for_window` whether or not the user paused for it, because the generator
has to bind that window to a variable anyway and this only decides where the
binding lands. A first-seen *element* gets a wait only where there was a pause
to explain, or every click on a new button would grow one.

Nothing here is persisted. Inference runs between the saved recording and the
generated script, so a recording made today is re-analyzed by whatever rules
exist when it is next rendered, and running it twice cannot compound.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model import (
    Assertion,
    Click,
    Drag,
    Event,
    MouseMove,
    Origin,
    Pause,
    Scroll,
    Target,
    WaitForElement,
    WaitForIdle,
    WaitForWindow,
    WindowActivate,
    WindowRef,
)

__all__ = ["SyncOptions", "infer_synchronization", "strip_inferred"]


@dataclass
class SyncOptions:
    """Which inferences to draw, and how patient the result should be."""

    enabled: bool = True
    """Infer at all. Off leaves every pause as the sleep it was recorded as."""

    windows: bool = True
    elements: bool = True

    activation: bool = True
    """Raise a window the recording went back to before acting in it.

    Separate from `windows` because it answers a different question. That rule
    is about a window that did not exist yet; this one is about one that did,
    and that the user brought forward by clicking on it -- which replay has to
    do deliberately, since injected input goes wherever focus already is.
    """

    idle: bool = True
    """Turn an unexplained pause into `wait_for_idle` on the window's process.

    Needs `WINDOW_PID`, which is a compositor-tier capability rather than a
    universal one, so this is the inference most likely to make a generated
    script demand more than a given desktop can offer.
    """

    min_timeout: float = 10.0
    max_timeout: float = 120.0
    factor: float = 3.0
    """Headroom over the observed gap. A wait that took 4s is given 12s."""


def infer_synchronization(
    events: list[Event], options: SyncOptions | None = None
) -> list[Event]:
    """Return `events` with dead time replaced by synchronization.

    The input is left alone; inferred events are new objects carrying the
    timestamp and delay of whatever they replaced, so the sequence still reads
    in recorded order.
    """
    options = options or SyncOptions()
    if not options.enabled:
        return list(events)
    return _Inferencer(options).run(events)


@dataclass
class _Walk:
    """What the walk has to remember from one event to the next."""

    window: str | None = None
    """Identity of the window the last pointer action happened in."""

    visited: set[str] = field(default_factory=set)
    """Windows a pointer action has already happened in.

    Deliberately not the `windows` set the other rules share. That one also
    counts a window merely *named* by an inferred wait, and a window the
    recording has never acted in is not one it can have come back to.
    """


@dataclass
class _Inferencer:
    """One inference pass, carrying what the recording has seen so far."""

    options: SyncOptions

    def run(self, events: list[Event]) -> list[Event]:
        """Walk the events, rewriting pauses and announcing window changes."""
        windows: set[str] = set()
        elements: set[tuple[str, str, str]] = set()
        state = _Walk()
        out: list[Event] = []
        for index, event in enumerate(events):
            if isinstance(event, Pause):
                out.append(self._resolve(event, events[index + 1 :], windows, elements))
                continue
            out.extend(self._announce(event, windows))
            out.extend(self._activation(event, state))
            _observe(event, windows, elements)
            out.append(event)
        return out

    def _activation(self, event: Event, state: _Walk) -> list[Event]:
        """Raise a window the recording came back to, before acting in it.

        Clicking a window that is already open both raises it and acts in it,
        and only the second half of that gets recorded. Replay does neither:
        `move_mouse` and `click` go to a coordinate whatever is in front of it,
        so a recording that moves between two open windows replays entirely
        into whichever one happened to have focus.

        A window seen for the *first* time is deliberately not this. It has
        just appeared, so it already has focus, and `_announce` has put a
        `wait_for_window` there instead -- raising it as well would be noise on
        the common path of a dialog opening.
        """
        if not self.options.activation or not isinstance(event, _POINTER_EVENTS):
            return []
        target = _target_of(event)
        window = target.window if target is not None else None
        key = _window_key(window)
        if not key or key == state.window:
            return []
        seen = key in state.visited
        state.window = key
        state.visited.add(key)
        if window is None or not window.addressable or not seen:
            # First time acting in this window: it has just been reached, so
            # whatever put it in front is already done, and `_announce` or the
            # pause rule has put a `wait_for_window` there instead.
            return []
        return [
            WindowActivate(
                timestamp=event.timestamp,
                window=window,
                origin=Origin.INFERRED,
                note=f"the recording moved back to {_window_label(window)!r} here",
            )
        ]

    # -- rules ---------------------------------------------------------------

    def _resolve(
        self,
        pause: Pause,
        rest: list[Event],
        windows: set[str],
        elements: set[tuple[str, str, str]],
    ) -> Event:
        """Decide what a pause was actually waiting for."""
        target = _next_target(rest)
        if target is None:
            return pause
        window = target.window
        key = _window_key(window)
        if self.options.windows and window is not None and key and key not in windows:
            # Mark it seen here, not only when the event that follows is
            # observed: the announcement rule runs first and would otherwise
            # emit a second wait for the window this one already covers.
            windows.add(key)
            return self._wait_for_window(pause, window)
        element = target.element
        if (
            self.options.elements
            and element is not None
            and element.addressable
            and (key, element.role, element.name) not in elements
        ):
            return WaitForElement(
                timestamp=pause.timestamp,
                delay=pause.delay,
                element=element,
                timeout=self._timeout(pause.seconds),
                note=_waited(pause, f"for {element.role} {element.name!r}"),
            )
        if self.options.idle and window is not None and window.addressable:
            return WaitForIdle(
                timestamp=pause.timestamp,
                delay=pause.delay,
                window=window,
                pid=window.pid,
                timeout=self._timeout(pause.seconds),
                note=_waited(pause, "with nothing observable changing"),
            )
        return pause

    def _announce(self, event: Event, windows: set[str]) -> list[Event]:
        """Emit a wait for a window this recording has not seen before.

        Unconditional, unlike the element rule: the generator has to look this
        window up to bind it to a variable whatever happens here, so all this
        decides is that the lookup lands where the window appeared rather than
        wherever it was first touched.
        """
        target = _target_of(event)
        window = target.window if target is not None else _activated(event)
        key = _window_key(window)
        if not self.options.windows or window is None or not key or key in windows:
            return []
        if isinstance(event, MouseMove):
            return []
        return [
            WaitForWindow(
                timestamp=event.timestamp,
                window=window,
                timeout=self.options.min_timeout,
            )
        ]

    def _wait_for_window(self, pause: Pause, window: WindowRef) -> Event:
        """Render rule 1: the pause was a window opening."""
        name = _window_label(window)
        return WaitForWindow(
            timestamp=pause.timestamp,
            delay=pause.delay,
            window=window,
            timeout=self._timeout(pause.seconds),
            note=_waited(pause, f"for {name!r} to open"),
        )

    def _timeout(self, seconds: float) -> float:
        """How long to allow, given how long it actually took."""
        wanted = max(self.options.min_timeout, seconds * self.options.factor)
        return round(min(wanted, self.options.max_timeout), 1)


# -- reading the event stream ------------------------------------------------

_POINTER_EVENTS = (Click, Drag, MouseMove, Scroll)

_AIMED_EVENTS = (*_POINTER_EVENTS, Assertion)
"""Events whose target the user chose, for "what was this pause waiting for".

A check is the one keyboard event that carries a deliberate target: the
pointer was put over the thing being verified before the key was pressed,
which is exactly the evidence `_next_target` wants. It is kept out of
`_POINTER_EVENTS` because it must not *raise* a window -- checking something
in a background window is a reasonable thing to record and does not mean the
recording moved into it.
"""


def _target_of(event: Event) -> Target | None:
    """The target an event acted on, whatever field the event keeps it in."""
    if isinstance(event, Drag):
        return event.start
    target = getattr(event, "target", None)
    return target if isinstance(target, Target) else None


def _activated(event: Event) -> WindowRef | None:
    """The window a `WindowActivate` names, so a raise counts as seeing it."""
    return event.window if isinstance(event, WindowActivate) else None


def _next_target(rest: list[Event]) -> Target | None:
    """The target of the next event that acted on something.

    Aimed events only. An ordinary keystroke's target is wherever the pointer
    happened to be resting, which says nothing about what the user was waiting
    for, and treating it as evidence is how an inferred wait ends up naming a
    window nobody was looking at. A check is the exception: its target was
    pointed at on purpose, and "waited, then verified" is the shape a pause
    before a check almost always has.
    """
    for event in rest:
        if isinstance(event, _AIMED_EVENTS):
            return _target_of(event)
        if isinstance(event, WindowActivate):
            return Target(x=0, y=0, window=event.window)
    return None


def _observe(
    event: Event, windows: set[str], elements: set[tuple[str, str, str]]
) -> None:
    """Record what this event proves was already on screen."""
    window = _activated(event)
    target = _target_of(event)
    if target is not None:
        window = target.window or window
    key = _window_key(window)
    if key:
        windows.add(key)
    if target is not None and target.element is not None:
        elements.add((key, target.element.role, target.element.name))


def _window_key(window: WindowRef | None) -> str:
    """A window's identity for "have we seen this before", or empty for none."""
    if window is None:
        return ""
    return window.app_id or window.title


def _window_label(window: WindowRef | None) -> str:
    """What to call a window in a comment a person will read.

    The title, where there is one. `_window_key` prefers the app id because
    that is what does not drift, but it makes a poor name in prose: once X11
    began reporting app ids, "the recording moved back to 'Recorder Check'"
    became "moved back to 'zenity'", which is true and unhelpful.
    """
    if window is None:
        return ""
    return window.title or window.app_id


def _waited(pause: Pause, what: str) -> str:
    """The comment explaining what the recording actually saw here."""
    return f"the recording waited {pause.seconds:g}s here {what}"


def strip_inferred(events: list[Event]) -> list[Event]:
    """Drop everything the analyzer added, leaving only what was observed.

    Inference is not persisted, so this exists for the case where a recording
    was saved after a render anyway -- an older file, or an editor that wrote
    its working state out.
    """
    inferred = (WaitForWindow, WaitForElement, WaitForIdle)
    return [
        event
        for event in events
        if not (isinstance(event, inferred) and event.origin is Origin.INFERRED)
    ]
