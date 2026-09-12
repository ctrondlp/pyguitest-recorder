"""Resolve a screen coordinate into the window and element beneath it.

This is what makes a recording outlive the coordinates it was made at, and it
is the reason the recorder can generate `gui.button("Save").click()` instead
of `gui.move_mouse(180, 90); gui.click()`.

Both halves go through one pyguitest session: `window_at` for the toplevel,
`element_at` for the accessible under the same point. The element side used
to talk to `Atspi.Component` through `gi` directly, because pyguitest had no
way to ask what was at a point -- its whole argument is that elements replace
coordinates. `Capability.ELEMENT_GEOMETRY` closed that gap, and this asks the
question through the public API now, which is also how the recorder inherits
pyguitest's own honesty about when screen coordinates mean anything.

Everything degrades. No AT-SPI means coordinates with window context; no
window backend means bare coordinates; and a recording made either way still
generates a working script, just a more fragile one.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..model import ElementRef, Target, WindowRef

__all__ = ["ContextResolver", "NullResolver", "DesktopResolver", "Observation"]

MAX_ANCESTRY = 8
"""How far up the process tree to look for the terminal the recorder runs in.

Far enough for python -> shell -> terminal with room to spare, short enough
that a deep chain cannot wander into the session's own shell."""

MAX_DEPTH = 24
"""Descent limit, so a malformed accessible tree cannot spin forever."""

WINDOW_ROLES = frozenset({"frame", "window", "dialog"})
"""Accessible roles that are a toplevel rather than something to click.

The same three pyguitest's `Role.WINDOW_ROLES` counts as windows, spelled
out here because they arrive as plain role strings from a recording that
may have been made on another machine.
"""

EXTENTS_SLACK = 8
"""Pixels an element may lie outside its window before it is disbelieved.

Client-side decorations put a widget a few pixels past the frame the window
manager reports, which is ordinary. Being *larger than the whole window* is
not, and that is what this catches.
"""

DECORATION_SLACK = 40
"""Pixels outside a window's client rect still treated as that window's own.

pyguitest reports only the client rectangle (X11::GUITest's own contract,
kept deliberately thin since not every backend can even see decorations),
but a reparenting window manager draws the titlebar and borders -- where the
close/minimize/shade buttons live -- outside it. A click there falls
through plain bounding-box containment to whatever *other* window's rect
happens to occupy that screen pixel, which is nearly always something: a
decoration sits right at a window's edge. Measured live on xfwm4:
`_NET_FRAME_EXTENTS` of 29px (titlebar) and 5px (borders) for an ordinary
dialog; 40 gives headroom for taller themes without reaching far enough to
swallow a genuinely different window sitting flush against this one.
"""


@dataclass(frozen=True)
class Observation:
    """What was under a point, and what it read at that moment.

    Separate from `Target` because the state here is only ever wanted when a
    check is being recorded. Reading an element's text costs a round trip to
    the application over the accessibility bus, and doing that on every click
    -- for a value nothing would use -- would put that cost in the path of
    every event the recorder sees.
    """

    target: Target
    text: str | None = None
    checked: bool | None = None
    checkable: bool = False


@runtime_checkable
class ContextResolver(Protocol):
    """Answers what a screen coordinate points at."""

    def resolve(self, x: int, y: int, screen: int = 0) -> Target:
        """Return the target at this point, with whatever context is available."""

    def inspect(self, x: int, y: int, screen: int = 0) -> Observation:
        """Return the target at this point along with what it currently reads."""

    def focused(self) -> Target | None:
        """Return the element holding keyboard focus, where that is knowable."""

    def close(self) -> None:
        """Release anything held open."""


@dataclass
class NullResolver:
    """Resolves to bare coordinates. The floor every other resolver falls back to."""

    def resolve(self, x: int, y: int, screen: int = 0) -> Target:
        """Return the point with no context attached."""
        return Target(x=x, y=y, screen=screen)

    def inspect(self, x: int, y: int, screen: int = 0) -> Observation:
        """Return the bare point; there is nothing here to read a state off."""
        return Observation(target=self.resolve(x, y, screen))

    def focused(self) -> Target | None:
        """Nothing here knows what has focus."""
        return None

    def close(self) -> None:
        """Nothing is held open."""


@dataclass
class _Identity:
    """One live window's identity, held steady while its title moves."""

    app_id: str
    title: str
    """The *first* title this window was seen with -- see `_identify`."""

    stable: bool = True
    app_id_ambiguous: bool = False
    """Whether another window open at the moment this was identified shared
    this app_id -- see `_app_id_ambiguous`."""


@dataclass
class DesktopResolver:
    """Resolves against the live desktop, through pyguitest and AT-SPI.

    `ignore_pids` keeps the recorder out of its own recording: the recorder's
    window is on the same desktop, appears in the same window list, and would
    otherwise be resolved as the target of every click aimed at its own
    controls.
    """

    session: Any = None
    elements: bool = True
    ignore_pids: set[int] = field(default_factory=set)

    _identity: dict[Any, _Identity] = field(default_factory=dict, init=False)
    _resolves_elements: bool = field(default=False, init=False)
    _warned: list[str] = field(default_factory=list, init=False)
    _scaled: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """Add this process and its terminal to the ignore set."""
        self.ignore_pids.add(os.getpid())
        self.ignore_pids.update(self._own_terminal_pids())
        self._scaled = self._any_screen_scaled()
        self._resolves_elements = self.elements and self._can_resolve_elements()

    def _own_terminal_pids(self) -> set[int]:
        """The window-owning process the recorder is being driven from, if any.

        `os.getpid()` alone is not the recorder's footprint on the desktop.
        The recorder has no window of its own -- it runs in a terminal, and
        it is that *terminal's* pid the window carries -- so ignoring only
        this process leaves the window it is being driven from looking like
        an ordinary application to record.

        Seen live on KDE: typing into GTK4's Text Editor was attributed to
        the Konsole the recorder was running in, because AT-SPI named a
        focused text field owned by that terminal and `_window_owning` was
        happy to match it. The recording then waited for a window titled
        after that terminal's foreground process -- `pyguitest-recorder`
        while recording, `python3` while replaying -- so it matched nothing
        and the replay aborted on a window the typing never needed.

        Stops at the *first* ancestor that owns a window, which is the
        terminal, rather than ignoring the whole ancestry: walk far enough up
        and a desktop-launched chain reaches the session's own shell, and
        ignoring `plasmashell`/`gnome-shell` would blind the recorder to the
        panels and menus it most needs to see. An ancestry with no
        window-owning process in it -- the recorder driven over SSH, say --
        contributes nothing.
        """
        if self.session is None:
            return set()
        try:
            owners = {window.pid for window in self.session.windows() if window.pid}
        except Exception:  # noqa: BLE001 - no window list is no terminal to find
            return set()
        for pid in _ancestor_pids():
            if pid in owners:
                return {pid}
        return set()

    def _can_resolve_elements(self) -> bool:
        """Whether this session can name what is under a point, and say so.

        Two questions, both of which have been answered wrongly here before.
        The capability is the version check as well as the feature check: a
        pyguitest without ELEMENT_GEOMETRY does not declare it, so an older
        one degrades to coordinates rather than raising AttributeError at
        the first click.

        The second is liveness. An accessibility bus can be reachable while
        its registry is dead, and every call then answers emptily rather
        than failing -- so this reads the tree once before believing it. A
        childless desktop is *not* a failure: applications register when
        they start, which may well be after a recording begins.
        """
        if self.session is None:
            self._warn(
                "element resolution off: no pyguitest session to ask; clicks "
                "carry coordinates and no element"
            )
            return False
        if "ELEMENT_GEOMETRY" not in {c.name for c in self.session.capabilities}:
            self._warn(
                "element resolution off: this session does not provide "
                "ELEMENT_GEOMETRY, so nothing can say what is under a point"
            )
            return False
        try:
            len(self.session.root_element().children)
        except Exception as exc:  # noqa: BLE001 - any failure is the same answer
            self._warn(f"element resolution off: the accessible tree ({exc})")
            return False
        self._warn_if_chromium_invisible()
        return True

    def _warn_if_chromium_invisible(self) -> None:
        """Note the one case element resolution stays partly blind.

        Chromium -- and so Electron, VS Code, Slack and the rest -- builds
        no accessible tree at all until something announces that an
        assistive technology is running, which on Linux means
        `org.a11y.Status.IsEnabled`. Element resolution is otherwise
        working here (`_can_resolve_elements` already returned true by the
        time this runs), so a click on a GTK or Qt window resolves fine
        while one on a Chromium window silently finds no element -- which
        without this note reads as a resolver bug rather than the known,
        diagnosable gap it is. See pyguitest's own
        `assistive_technology_enabled` for the measurement this reports.
        """
        try:
            from pyguitest.session import assistive_technology_enabled
        except ImportError:
            return
        if assistive_technology_enabled() is False:
            self._warn(
                "Chromium and Electron windows (VS Code, Slack, and the "
                "rest) will resolve to no element: org.a11y.Status.IsEnabled "
                "is false, so they never register with the accessible tree "
                "at all, even though other windows resolve normally"
            )

    def _any_screen_scaled(self) -> bool:
        """Whether any screen is scaled, which makes extents incomparable."""
        if self.session is None:
            return False
        try:
            return any(float(screen.scale) != 1.0 for screen in self.session.screens())
        except Exception:  # noqa: BLE001 - no screen info is not a scaled screen
            return False

    @property
    def warnings(self) -> list[str]:
        """Anything that degraded, for the recording's environment block."""
        return list(self._warned)

    @property
    def resolves_elements(self) -> bool:
        """Whether clicks will carry a named element rather than a coordinate."""
        return self._resolves_elements

    def resolve(self, x: int, y: int, screen: int = 0) -> Target:
        """Return the window and element under this point.

        The element is required to belong to the same process as the window,
        which is not a formality. The accessibility bus is scoped to the login
        session, not to an X display, so a recorder capturing one display gets
        answers about applications on every other one -- and since both are
        asked about the same numeric coordinate, the wrong answer looks exactly
        like the right one. Observed: a recording made on a private Xvfb
        resolving its clicks onto the editor the recorder was written in.
        """
        window = self._window(x, y, screen)
        element = self._element(x, y) if self._resolves_elements else None
        if element is not None and not self._is_widget(element):
            return Target(x=x, y=y, screen=screen, window=window)
        if element is not None and not self._covers(element, x, y):
            return Target(x=x, y=y, screen=screen, window=window)
        if element is not None and not self._belongs(element, window):
            return Target(x=x, y=y, screen=screen, window=window)
        return Target(x=x, y=y, screen=screen, window=window, element=element)

    def inspect(self, x: int, y: int, screen: int = 0) -> Observation:
        """Resolve this point and read the state of whatever is under it.

        The element is looked up a second time rather than kept live from
        `resolve`: what that returns is a snapshot with no handle behind it,
        deliberately, so a recording can be written to a file. The second
        lookup is only reached when a check is being recorded, and it is
        checked against the first -- a tree that changed between the two
        answers is a tree whose reading cannot be attributed to the element
        the check was aimed at, so the state is dropped and the check
        degrades to "this is showing".
        """
        target = self.resolve(x, y, screen)
        if target.element is None:
            return Observation(target=target)
        return self._state(x, y, target)

    def _state(self, x: int, y: int, target: Target) -> Observation:
        """Read text and checked state off the live element under this point."""
        expected = target.element
        try:
            element = self.session.element_at(x, y)
            if element is None or expected is None:
                return Observation(target=target)
            if element.role != expected.role or (element.name or "") != expected.name:
                self._warn(
                    "recorded a check against an element that changed between "
                    "being named and being read; the check only requires it to "
                    "be showing"
                )
                return Observation(target=target)
            return Observation(
                target=target,
                text=element.text,
                checked=element.checked,
                checkable=bool(element.checkable),
            )
        except Exception:  # noqa: BLE001 - an unreadable state is no state
            return Observation(target=target)

    def focused(self) -> Target | None:
        """The element holding keyboard focus, or None if it cannot be trusted.

        This is the one question that names the widget typing is going *into*,
        rather than the one the pointer happens to be resting over, and on a
        toolkit whose hit-testing cannot place a widget it is the only question
        that works at all. Measured live: GTK4 publishes per-widget focus
        correctly on a bare X server, while `element_at` there returns the
        frame for every point.

        Two things are checked before the answer is believed.

        A desktop that does not publish per-widget focus answers with a
        *toplevel* instead -- GNOME Shell holds FOCUSED for the whole desktop
        on its own window -- so a window role is treated as "no answer", the
        same test `focus_tracking_works` makes. It is asked per run rather
        than once at startup because it is a fact about this moment: nothing
        has focus yet when a recording begins.

        Then the process. The accessibility bus is scoped to the login session
        and not to one X display, so `focused()` searches the whole desktop
        and will cheerfully name a widget in another session's application --
        and unlike a click, focus carries no coordinate to corroborate it
        against. The owning window is the only evidence there is, so an
        element no window on the recorded display accounts for is refused
        outright rather than guessed at.
        """
        if not self._resolves_elements:
            return None
        try:
            element = self.session.focused()
            is_window = element is not None and element.role in WINDOW_ROLES
        except Exception:  # noqa: BLE001 - an unreadable tree is no answer
            # `.role` reads the live bus same as `.focused()` did, and can go
            # stale between the two calls -- reliably so right after a key
            # like Escape, which is as likely to be closing the very menu
            # that just had focus as it is to be recorded text. A GLib
            # GError over "no such object path" here is dogtail asking the
            # bus about an accessible that has already been reaped, not a
            # bug worth stopping a recording for.
            return None
        if element is None or is_window:
            return None
        try:
            ref = self._describe_element(element)
        except Exception:  # noqa: BLE001
            return None
        window = self._window_owning(ref)
        if window is None:
            return None
        # Focus has no coordinates of its own. The window's own origin is used
        # rather than the pointer's position, so that if anything ever does
        # read a coordinate off this target it gets one inside the right
        # window instead of one pointing into a different one.
        geometry = window.geometry
        x, y = (geometry[0], geometry[1]) if geometry else (0, 0)
        return Target(x=x, y=y, window=window, element=ref)

    def _window_owning(self, element: ElementRef) -> WindowRef | None:
        """The window on the recorded display whose process published `element`."""
        if element.pid is None:
            self._warn(
                "ignored keyboard focus for elements that publish no process "
                "id; nothing else can tie them to the display being recorded, "
                "so typed text falls back to the last field that was clicked"
            )
            return None
        try:
            windows = list(self.session.windows())
        except Exception:  # noqa: BLE001
            return None
        owned = [
            window
            for window in windows
            if window.pid == element.pid and window.pid not in self.ignore_pids
        ]
        if owned:
            return self._describe(self._best_owner(owned, element))
        self._warn(
            f"ignored keyboard focus in pid {element.pid}, which owns no window "
            "on the recorded display; the accessibility bus is not scoped to "
            "one X display, so it came from another session"
        )
        return None

    def _best_owner(self, owned: list[Any], element: ElementRef) -> Any:
        """Which of one process's windows the focused element actually sits in.

        A process commonly owns more than one, and taking the first is how a
        recording ends up announcing a window nothing was ever done in: zenity
        owns both its dialog and a window called "zenity", and the run that
        found this generated a `wait_for_window("zenity")` for typing that went
        into the dialog.

        The element's own ancestry settles it -- the accessible tree names the
        toplevel it descends from, which is exactly the question being asked.
        Focus falls back to the active window, since whatever holds the
        keyboard is by definition in front, and to the first match only when
        neither answers.
        """
        toplevel = next(
            (name for role, name in reversed(element.path) if role in WINDOW_ROLES),
            "",
        )
        if toplevel:
            match = next((w for w in owned if w.title == toplevel), None)
            if match is not None:
                return match
        active = self._active_window()
        if active is not None:
            handle = getattr(active, "handle", None)
            for window in owned:
                if handle is not None and getattr(window, "handle", None) == handle:
                    return window
        return owned[0]

    def _is_widget(self, element: ElementRef) -> bool:
        """Whether the answer is something a script could sensibly click.

        A hit test that bottoms out at the *toplevel* has not found a widget
        -- it has found the only thing in that application whose rectangle
        could be checked. That is what a toolkit reporting its widgets in
        window coordinates looks like from outside: the frame is placed, so
        it passes, and every widget inside it claims points elsewhere on the
        screen, so none of them do. `gui.element(role=Role.FRAME, ...)` is
        never the click that was recorded, and the window is already carried
        separately, so this falls back to a coordinate instead.
        """
        if element.role not in WINDOW_ROLES:
            return True
        self._warn(
            f"ignored accessible answers that bottomed out at the window "
            f"itself ({element.role!r} {element.name!r}); this toolkit does "
            "not place its widgets in screen coordinates"
        )
        return False

    def _covers(self, element: ElementRef, x: int, y: int) -> bool:
        """Whether the element AT-SPI named actually contains the point asked about.

        `get_accessible_at_point` is only as good as the extents behind it, and
        a toolkit that reports every widget at the origin makes every widget
        contain every point. Seen live: a GTK4 dialog returning the same
        `label` for all of (10, 10) through (310, 300), extents 252x25. The
        recorder cannot tell that apart from a real answer, and believing it
        generates `gui.element(role=Role.LABEL, name=...).click()` for a click
        that was nowhere near the label -- a script that runs cleanly and does
        the wrong thing.

        Falling back to a coordinate here is a real loss, so the check is only
        applied where it can be trusted: extents must be known, and no screen
        may be scaled, since AT-SPI and the capture backend are then not
        necessarily reporting the same units.
        """
        if self._scaled or element.extents is None:
            return True
        ex, ey, width, height = element.extents
        if ex - EXTENTS_SLACK <= x <= ex + width + EXTENTS_SLACK and (
            ey - EXTENTS_SLACK <= y <= ey + height + EXTENTS_SLACK
        ):
            return True
        self._warn(
            f"ignored accessible elements whose own extents do not contain the "
            f"point they were looked up at ({element.role!r} at {element.extents} "
            f"for ({x}, {y})); this toolkit's hit-testing cannot be trusted"
        )
        return False

    def _belongs(self, element: ElementRef, window: WindowRef | None) -> bool:
        """Whether an element and the window under the same point agree.

        A pid missing on either side means the question cannot be asked -- no
        `WINDOW_PID`, or a bridge that publishes none -- and refusing every
        element on those desktops would cost far more than the mismatch it
        prevents. So only a real disagreement rejects.

        The exception is a window lookup that ran and found *nothing*. On a
        display with no window under the pointer, an accessible under that same
        point can only have come from somewhere else, and the recorder has just
        been told so. Keeping it is how a click on an empty private display
        ends up naming a widget in the developer's editor.
        """
        if window is None:
            if self.session is None:
                return True
            self._warn(
                "ignored accessible elements that no window on the recorded "
                "display accounts for; the accessibility bus is not scoped to "
                "one X display, so they came from another session"
            )
            return False
        if window.pid is not None and element.pid is not None:
            return self._same_process(element, window)
        return self._fits(element, window)

    def _same_process(self, element: ElementRef, window: WindowRef) -> bool:
        """Whether the element's process is the window's."""
        if element.pid == window.pid:
            return True
        self._warn(
            f"ignored an accessible element from pid {element.pid} under a "
            f"window owned by pid {window.pid}; the accessibility bus is not "
            "scoped to one X display"
        )
        return False

    def _fits(self, element: ElementRef, window: WindowRef) -> bool:
        """Whether the element's rectangle could plausibly be in this window.

        The fallback for when a pid is missing on either side, which is common:
        a bare X server with no window manager publishes no `_NET_WM_PID`, and
        not every accessibility bridge answers `get_process_id`.

        An element inside a window cannot be bigger than the window. When it is
        -- extents of 1920x1080 reported for a widget in a 310x263 window, seen
        while recording a private X server next to a real desktop -- the two
        answers are describing different screens, and only one of them is the
        one being recorded.

        Skipped entirely where any screen is scaled, because AT-SPI extents and
        window geometry are then not reliably in the same units and a false
        rejection costs every named element on the desktop.
        """
        if self._scaled or window.geometry is None or element.extents is None:
            return True
        wx, wy, width, height = window.geometry
        ex, ey, ewidth, eheight = element.extents
        if (
            ex >= wx - EXTENTS_SLACK
            and ey >= wy - EXTENTS_SLACK
            and ex + ewidth <= wx + width + EXTENTS_SLACK
            and ey + eheight <= wy + height + EXTENTS_SLACK
        ):
            return True
        self._warn(
            f"ignored accessible elements whose {ewidth}x{eheight} extents do "
            f"not fit the {width}x{height} window under the same point; they "
            "describe a different screen from the one being recorded"
        )
        return False

    def _warn(self, message: str) -> None:
        """Record a degradation once, however many events hit it."""
        if message not in self._warned:
            self._warned.append(message)

    def close(self) -> None:
        """Nothing to release: the session belongs to the caller."""

    # -- windows -------------------------------------------------------------

    def _window(self, x: int, y: int, screen: int) -> WindowRef | None:
        """Find the toplevel under the point, falling back to the active one.

        The fallback is a guess and is checked before it is believed. When the
        hit test comes back empty the click landed on the root, on a window the
        backend cannot see, or on one that has just moved -- and the *focused*
        window is only sometimes the right answer to that. Seen live: a click
        at (760, 500) in a second window resolved, through this fallback, to a
        window occupying (-160, 0, 310, 263), which does not contain the point
        by 610 pixels. Every coordinate under it then came out relative to the
        wrong origin, in a script that validated clean.

        A short, bounded retry on a total miss (neither the hit test nor the
        active-window fallback found anything at all) is new: seen live and
        reproduced repeatedly on KDE, dismissing GNOME Text Editor's own
        in-window "Discard changes?" sheet -- two clicks landing barely a
        moment apart -- came back with no window attribution every single
        time, on a click that a live re-check moments later resolves
        correctly. The window is real and already the active one; only the
        live subprocess round trip queried for it (kdotool, via KWin's
        scripting interface) is what is occasionally too slow to answer in
        time for a fast second click, not the window itself being unfindable.
        """
        if self.session is None:
            return None
        window = self._resolve_window(x, y, screen)
        if window is None:
            for _ in range(2):
                time.sleep(0.05)
                window = self._resolve_window(x, y, screen)
                if window is not None:
                    break
        if window is None:
            return None
        if window.pid in self.ignore_pids:
            return None
        return self._describe(window)

    def _resolve_window(self, x: int, y: int, screen: int) -> Any:
        """One attempt at the hit-test / decoration / active-window chain."""
        window = self._window_at(x, y, screen)
        window = self._prefer_decoration_owner(window, x, y)
        if window is None:
            window = self._plausible_active(x, y)
        return window

    def _prefer_decoration_owner(self, window: Any, x: int, y: int) -> Any:
        """Swap a plain hit-test match for the active window's own decoration.

        Seen live: closing "Application Finder" by its titlebar X recorded a
        click inside a terminal window sitting behind it, because the X sits
        outside the client rect pyguitest reports and the terminal's own rect
        happened to cover that pixel (see DECORATION_SLACK). The active
        window is almost always the one whose chrome was just clicked -- you
        do not usually reach past the focused window to close some other one
        -- so it is preferred whenever the point falls just outside its rect
        but the plain hit test landed on something else.
        """
        active = self._active_window()
        if active is None or active == window:
            return window
        geometry = self._geometry(active)
        if geometry is None:
            return window
        ax, ay, awidth, aheight = geometry
        if ax <= x < ax + awidth and ay <= y < ay + aheight:
            # Inside the active window's own client rect: the plain hit test
            # already agrees, or disagrees for some other reason this slack
            # is not about.
            return window
        near = (
            ax - DECORATION_SLACK <= x < ax + awidth + DECORATION_SLACK
            and ay - DECORATION_SLACK <= y < ay + aheight + DECORATION_SLACK
        )
        return active if near else window

    def _plausible_active(self, x: int, y: int) -> Any:
        """The focused window, but only if it could be the one under the point."""
        window = self._active_window()
        if window is None:
            return None
        geometry = self._geometry(window)
        if geometry is None:
            # Nothing to check it against; the guess is all there is.
            return window
        wx, wy, width, height = geometry
        if wx <= x < wx + width and wy <= y < wy + height:
            return window
        self._warn(
            "ignored the focused window as a stand-in for points it does not "
            "cover; those clicks carry absolute coordinates instead"
        )
        return None

    def _window_at(self, x: int, y: int, screen: int) -> Any:
        """Hit-test the point, where the backend supports it."""
        try:
            return self.session.window_at(x, y, screen)
        except Exception:  # noqa: BLE001 - any backend failure degrades the same
            return None

    def _active_window(self) -> Any:
        """The focused toplevel, as the fallback for a failed hit-test."""
        try:
            return self.session.active_window()
        except Exception:  # noqa: BLE001
            return None

    def _describe(self, window: Any) -> WindowRef:
        """Snapshot a pyguitest Window under a *stable* identity."""
        identity = self._identify(window)
        return WindowRef(
            title=identity.title,
            app_id=identity.app_id,
            pid=window.pid,
            geometry=self._geometry(window),
            title_stable=identity.stable,
            app_id_ambiguous=identity.app_id_ambiguous,
        )

    def _identify(self, window: Any) -> _Identity:
        """Follow one live window even as its title changes underneath it.

        Keyed on the window itself, which pyguitest hashes and compares by
        backend handle rather than by title -- exactly because a title can
        change while the window stays put.

        The title recorded is the **first** one seen, and every later mention
        of that window reuses it. That is what stops a drifting title from
        looking like a series of different windows: recording a text editor
        while typing "Hello" produced four `wait_for_window` calls for one
        window, three of which match nothing at replay, and since
        `wait_for_window` answers None rather than raising, the script then
        failed on `None.pid` several lines further down. Seen in the first
        recording anyone made of a real application.

        The first title is also the right one to match on, not merely the
        cheapest: replay follows the same sequence from the same starting
        state, so the title the window had when the recording first touched
        it is the title it will have when the script first looks for it.
        """
        key = self._identity_key(window)
        known = self._identity.get(key)
        # Stripped, not just read raw: the same window's title has come back
        # with and without a trailing space from different backends on this
        # very desktop (KWin's own live caption, kdotool, and whatever X11
        # property recording reads all agreeing on the visible text and
        # disagreeing on trailing whitespace) -- confirmed live on KDE, where
        # a recorded "Desktop @ QRect(0,0 1920x1080) " never matched the
        # identical-looking window `wait_for_window` found at replay. A
        # meaningless whitespace difference should not decide whether a
        # lookup finds its window, or whether this counts as the title
        # having drifted.
        title = (window.title or "").strip()
        if known is None:
            known = _Identity(
                app_id=window.app_id or "",
                title=title,
                app_id_ambiguous=self._app_id_ambiguous(window),
            )
            self._identity[key] = known
            return known
        if not known.app_id and window.app_id:
            known.app_id = window.app_id
            known.app_id_ambiguous = self._app_id_ambiguous(window)
        if title and title != known.title:
            # Warned once per window, not once per title. An editor retitles
            # itself on every keystroke, so naming the new title here put
            # eight near-identical notes in the header of one recording --
            # each unique, so `_warn`'s deduplication could not collapse them.
            if known.stable:
                self._warn(
                    f"the window first seen as {known.title!r} renamed itself "
                    "while it was being recorded; that first title is what the "
                    "script matches on, and an app id would be steadier"
                )
            known.stable = False
        return known

    def _identity_key(self, window: Any) -> Any:
        """Something that names this window for as long as the session lasts.

        The window itself where it can be hashed, since that follows the
        backend handle. A backend whose handle cannot be hashed falls back to
        the old behaviour of keying on what the window says it is, which
        cannot survive a drifting title but is no worse than before.
        """
        try:
            hash(window)
        except TypeError:
            return f"{window.app_id}\x00{window.title}"
        return window

    def _app_id_ambiguous(self, window: Any) -> bool:
        """Whether another currently open window shares this app_id.

        A single process can own several toplevels sharing one app_id -- a
        desktop shell's own desktop, panels, and popups, seen live all
        reporting "plasmashell" on KDE. When that is true, app_id alone
        cannot tell this window apart from the others, and a script matching
        on it would find whichever one happens to be listed first, silently,
        rather than the one actually meant. A live list is used rather than
        anything cached, because the answer is about what else is open right
        now, not what this window itself reports.
        """
        if not window.app_id or self.session is None:
            return False
        try:
            live = self.session.windows()
        except Exception:  # noqa: BLE001 - fails open, to app_id trusted as before
            return False
        return any(
            other != window
            and other.app_id == window.app_id
            and other.pid not in self.ignore_pids
            for other in live
        )

    def _geometry(self, window: Any) -> tuple[int, int, int, int] | None:
        """Read the window rectangle, where the backend has one."""
        try:
            rect = self.session.geometry(window)
        except Exception:  # noqa: BLE001
            return None
        if not rect:
            return None
        x, y, width, height = (int(v) for v in rect)
        return (x, y, width, height)

    # -- elements ------------------------------------------------------------

    def _element(self, x: int, y: int) -> ElementRef | None:
        """Snapshot the accessible element under this point, if there is one.

        Every read is inside the guard, not just the lookup: the tree can go
        stale between naming an element and describing it -- a menu closing
        under the pointer is enough -- and a half-read element is worth less
        than the coordinate it would replace.
        """
        try:
            element = self.session.element_at(x, y)
            if element is None:
                return None
            return self._describe_element(element)
        except Exception:  # noqa: BLE001 - a stale tree raises freely
            return None

    def _describe_element(self, element: Any) -> ElementRef:
        """Snapshot a live accessible as the durable reference a recording keeps."""
        return ElementRef(
            role=element.role,
            name=element.name or "",
            description=element.description or "",
            path=_ancestry(element),
            extents=self._extents(element),
            pid=element.pid,
            actions=tuple(element.actions or ()),
        )

    def _extents(self, element: Any) -> tuple[int, int, int, int] | None:
        """The element's screen rectangle, where the session serves one."""
        try:
            rect = self.session.extents(element)
        except Exception:  # noqa: BLE001
            return None
        if not rect:
            return None
        x, y, width, height = (int(v) for v in rect)
        return (x, y, width, height)


def _ancestor_pids(limit: int = MAX_ANCESTRY) -> list[int]:
    """This process's ancestors, nearest first, up to `limit` of them.

    Read from `ps` rather than `/proc`, in one call: FreeBSD without
    linprocfs has no `/proc` at all, and this runs once at startup where a
    subprocess costs nothing. An unreadable process table is not fatal --
    the caller still ignores this process itself, as it always has.
    """
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    parents: dict[int, int] = {}
    for line in (result.stdout or "").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            parents[int(fields[0])] = int(fields[1])
    chain: list[int] = []
    pid = os.getpid()
    while len(chain) < limit:
        pid = parents.get(pid, 0)
        if pid <= 1:
            break
        chain.append(pid)
    return chain


def _ancestry(element: Any) -> tuple[tuple[str, str], ...]:
    """The (role, name) chain from the application root down to this element.

    Kept so an ambiguous name can be disambiguated by where it sits rather
    than by index, which is the locator that breaks the moment a toolbar
    gains a button.
    """
    path: list[tuple[str, str]] = []
    current = element
    for _ in range(MAX_DEPTH):
        try:
            parent = current.parent
            if parent is None:
                break
            path.append((parent.role, parent.name or ""))
        except Exception:  # noqa: BLE001
            break
        current = parent
    return tuple(reversed(path))
