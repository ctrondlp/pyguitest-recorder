"""A live log of AT-SPI effects, for naming native-Wayland clicks.

The one thing a screen coordinate cannot do on native Wayland is name the
widget under it: a toolkit does not tell a client where it is on screen, so
`element_at` is refused there (its own capability guard says so). What the
toolkit *does* publish is the effect of an interaction -- the tab that became
selected, the list item that was chosen, the control that took focus -- as an
AT-SPI event whose source *is* that widget, with no coordinate involved.

This listens for those effects as they happen and keeps a short timestamped
log. The resolver, asked to name a click it could not place by coordinate,
reads the log for the effect that fired just after the press: its source is
the widget. A plain push button is the one common control this cannot name --
Qt publishes no observable event for its click -- which stays a coordinate.

It is a gi.Atspi client of its own, independent of the resolver's pyguitest
session, and runs its event loop on a private `GMainContext` in a daemon
thread so it neither needs nor disturbs the main thread's synchronous AT-SPI
queries. Started and stopped by the recorder around a recording; absent (and
harmless) everywhere else.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

__all__ = ["AtspiEffect", "AtspiEffectLog"]

# The effects whose source is the widget that reacted. `state-changed:selected`
# fires on the chosen item, `selection-changed`/`active-descendant` on the
# container, `focus:`/`state-changed:focused` on whatever took focus. Pressed
# is included for the toolkits that do emit it on a button; most do not.
_EVENTS = (
    "object:state-changed:selected",
    "object:selection-changed",
    "object:active-descendant-changed",
    "object:state-changed:focused",
    "focus:",
    "object:state-changed:pressed",
)
# Toplevel roles: where the ancestry walk stops, and the sources `nearest`
# refuses (a window that took focus names no widget). A page tab is NOT one --
# it is a perfectly good click target, so it must not be excluded here.
_WINDOW_ROLES = frozenset({"frame", "window", "dialog"})


@dataclass(frozen=True)
class AtspiEffect:
    """One observed effect: the widget that reacted, and when."""

    at: float
    """`time.monotonic()` when the effect fired -- the same clock every
    capture backend stamps its events with (the wayland backend rebases the
    kernel's CLOCK_REALTIME stamps onto it at capture), so a press timestamp
    and this are directly comparable."""

    name: str
    role: str
    pid: int | None
    toplevel: str
    """The name of the window-role ancestor, used to tie the effect to the
    window a click landed in -- the one match available when the window
    backend (kdotool, COSMIC) publishes no pid to match on."""


class AtspiEffectLog:
    """Collect AT-SPI effects on a private event loop; query by time + window."""

    def __init__(self, maxlen: int = 256) -> None:
        """Keep the last `maxlen` effects; nothing runs until `start`."""
        self._effects: deque[AtspiEffect] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: Any = None
        self._context: Any = None
        self._listeners: list[Any] = []

    def start(self) -> bool:
        """Begin listening. Returns False if AT-SPI cannot be started.

        False means "no effect naming", never an exception: a missing gi, or a
        session with no accessibility bus for `Atspi.init()` to reach, must
        leave clicks on coordinates rather than take down `Recorder` startup
        (which calls this with no try/except around it).

        Atspi is imported and initialised here, on the caller's (main) thread,
        and the private context is created and made this instance's before the
        worker thread touches anything: gi.Atspi binds its event dispatch to
        the context that is thread-default when it first initialises, so doing
        that setup in the worker thread -- after pyguitest's own pyatspi has
        initialised Atspi on the default context -- left the listener on a
        context nothing ran, and it saw no events (measured live).
        """
        try:
            import gi  # noqa: PLC0415

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi, GLib  # noqa: PLC0415
        except (ImportError, ValueError):
            return False
        self._context = GLib.MainContext.new()
        self._context.push_thread_default()
        try:
            Atspi.init()
            for event_type in _EVENTS:
                listener = Atspi.EventListener.new(self._on_event)
                listener.register(event_type)
                # Kept as (listener, event_type) pairs so `stop` can
                # deregister exactly what was registered.
                self._listeners.append((listener, event_type))
        except Exception:  # noqa: BLE001 - a dead a11y bus degrades, not raises
            self._listeners.clear()
            return False
        finally:
            self._context.pop_thread_default()
        # The loop is created HERE, before the worker starts, so `stop` always
        # has it: created inside the worker instead, a stop() racing a slow
        # thread start found `_loop` still None, quit nothing, timed out the
        # join and left the loop -- and the registered listeners -- running
        # for the life of the process. Reachable in practice: Recorder.start()
        # stops this immediately when the capture backend fails to start.
        self._loop = GLib.MainLoop.new(self._context, False)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop the event loop, release the listeners, stop appending."""
        loop, context = self._loop, self._context
        if loop is not None and context is not None:
            from gi.repository import GLib  # noqa: PLC0415

            # Quit via an idle source attached to the private context rather
            # than loop.quit() alone: a quit that lands before run() begins is
            # lost (run re-arms the loop), while an attached source fires as
            # soon as the loop dispatches -- however late the worker thread
            # gets there -- so both orderings end it.
            source = GLib.Idle()
            source.set_callback(lambda *_a: loop.quit() or GLib.SOURCE_REMOVE)
            source.attach(context)
        if self._thread is not None:
            self._thread.join(timeout=3)
        for listener, event_type in self._listeners:
            with contextlib.suppress(Exception):
                listener.deregister(event_type)
        self._listeners.clear()

    def nearest(self, after: float, within: float, toplevel: str) -> AtspiEffect | None:
        """The earliest effect in [after, after+within] from window `toplevel`.

        Inclusive at both ends: an effect stamped in the press's own instant
        is still its effect. Earliest rather than latest: the first thing to
        react to a press is what the press did; later effects are its
        consequences. `toplevel` ties it to the clicked window by the window's
        own title, since the window backend may publish no pid to match on.
        """
        with self._lock:
            candidates = [
                e
                for e in self._effects
                if after <= e.at <= after + within
                and e.name
                and e.toplevel == toplevel
                and e.role not in _WINDOW_ROLES
            ]
        return min(candidates, key=lambda e: e.at) if candidates else None

    # -- listener thread ----------------------------------------------------
    def _run(self) -> None:
        # The listeners were registered on `self._context`, and the loop was
        # created on it, in `start`; this thread only runs it so their events
        # are dispatched.
        self._context.push_thread_default()
        try:
            self._loop.run()
        finally:
            self._context.pop_thread_default()

    def _on_event(self, event: Any) -> None:
        widget = self._widget_of(event)
        if widget is None:
            return
        try:
            name = widget.get_name() or ""
            role = widget.get_role_name() or ""
            pid = widget.get_process_id()
            toplevel = self._toplevel_name(widget)
        except Exception:  # noqa: BLE001 - a reaped accessible is no effect
            return
        with self._lock:
            self._effects.append(
                AtspiEffect(
                    # time.monotonic, the pipeline's one clock -- press
                    # timestamps arrive rebased onto it, see AtspiEffect.at.
                    at=time.monotonic(),
                    name=name,
                    role=role,
                    pid=pid,
                    toplevel=toplevel,
                )
            )

    @staticmethod
    def _widget_of(event: Any) -> Any:
        """The widget an event is really about, which is not always its source.

        Measured on the Qt AT-SPI bridge (KDE Plasma 6): a container, not the
        chosen widget, is the source of the two selection events -- a page-tab
        list for `selection-changed`, the list/tree for `active-descendant`.
        The widget the user actually acted on is the selected child, and
        naming *that* is the whole point. For `state-changed:selected` the
        source already is the item (so only when it became selected), and for
        focus the source is what took focus. Everything is `getattr`/try
        guarded: an AT-SPI shape this does not expect yields nothing and the
        click stays a coordinate, never an error.
        """
        src = getattr(event, "source", None)
        if src is None:
            return None
        etype = getattr(event, "type", "")
        try:
            if "active-descendant" in etype:
                return getattr(event, "any_data", None)
            if "selection-changed" in etype:
                if src.get_n_selected_children() > 0:
                    return src.get_selected_child(0)
                return None
            if "state-changed:selected" in etype:
                return src if getattr(event, "detail1", 0) == 1 else None
        except Exception:  # noqa: BLE001 - a selection iface that will not answer
            return None
        return src  # focus:, state-changed:focused, state-changed:pressed

    @staticmethod
    def _toplevel_name(src: Any) -> str:
        """The name of the nearest window-role ancestor, or ''.

        Bounded: a cycle or a pathological tree must not hang the listener.
        """
        node = src
        for _ in range(24):
            try:
                role = node.get_role_name()
                if role in _WINDOW_ROLES:
                    return node.get_name() or ""
                parent = node.get_parent()
            except Exception:  # noqa: BLE001
                return ""
            if parent is None:
                return ""
            node = parent
        return ""
