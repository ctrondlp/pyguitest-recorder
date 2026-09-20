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
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from ..model import ElementRef, Target, WindowRef
from ..platforms import foreign_element_reason, foreign_focus_reason, is_windows

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

MENU_OWNER_ROLES = frozenset({"menu", "combo box"})
"""Roles that open a popup when clicked -- a menu-bar entry, a drop-down.

The popup is a window of its own, stacked over whatever is under it, and it is
not a child of the frame it opened from, so a hit test made from that frame
answers for the widget *underneath* the popup. See `DesktopResolver._popup_at`.
"""

POPUP_ITEM_ROLES = frozenset(
    {"menu item", "check menu item", "radio menu item", "menu"}
)
"""What a popup is made of. `menu` is here because an entry that opens a submenu
is one -- and it is the popup's own container for a combo box."""

POPUP_WAIT_SECONDS = 0.3
"""How long a click on a menu waits for its popup to appear before giving up on
seeing it. A popup is opened by the application, after the click, so a recorder
that consumes the click promptly can get here first."""

POPUP_POLL_SECONDS = 0.05
"""Between looks, while waiting for a popup to appear."""

POPUP_MEMORY_SECONDS = 6.0
"""How long a popup last seen open is still believed to be what a press landed
in, once it has closed. Not a guess about how long a menu stays open -- the
recorder consumes a click some time after it happened, and choosing an item
closes the popup, so by then it is usually gone. What this bounds is how long a
layout can outlive the popup it described: a click on whatever was beneath the
popup, later, must not be answered with a menu item."""

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

MOTION_TRUST_SECONDS = 0.25
"""How long a recorded move takes the last window lookup on trust, in seconds.

A pointer crossing one window asks the same question hundreds of times a second
and a lookup costs a round trip per window on the desktop, so a move reuses the
last answer -- but "the point is still inside the rectangle that answer covered"
is not evidence the window is still the one *under* it. A window stacked on top
of the answered one covers the same point, and the moves that entered it would be
attributed to the window beneath for as long as the pointer stayed inside the
first one's rectangle: the whole of a menu or dialog drawn over a bigger window,
which is where a recording spends its time. So the answer expires, and is asked
for afresh at most this often -- a few lookups a second where there were hundreds
-- which bounds how long a stacking change can go unnoticed and how much a
recording pays to notice it.
"""


def _scope_phrase(windows: bool | None = None) -> str:
    """How to describe the set of windows this recording covers.

    "the recorded display" is an X11 idea; on a native Windows recording there
    is no display to name and the windows are the desktop's own.

    `windows` is the recording's platform where the caller knows it from the
    capture backend rather than from the host -- see `DesktopResolver.windows`.
    None asks about this machine, which is the right answer for a caller with
    no backend to ask.
    """
    on_windows = is_windows() if windows is None else windows
    return "in this recording" if on_windows else "on the recorded display"


_FRAME_WINDOW_CLASS = "applicationframewindow"
"""The window class `ApplicationFrameHost.exe` hosts every UWP toplevel in.

Lower case because window classes are compared case-insensitively, and this is
the form `_in_a_frame_host` compares against.
"""


def _in_a_frame_host(window: WindowRef) -> bool:
    """Whether this window is one of Windows' UWP frame hosts.

    `Window.app_id` carries the window *class* name on Windows rather than the
    desktop-file id it is on X11 (pyguitest's own win32 backend documents the
    difference), and every Store app's toplevel -- Calculator, the Store build
    of Notepad, anything installed from it -- is an `ApplicationFrameWindow`
    owned by `ApplicationFrameHost.exe`. That split is the one pid mismatch on
    this platform with a documented cause, and the class name is what lets a
    recording tell it from a stranger's element.
    """
    return window.app_id.strip().casefold() == _FRAME_WINDOW_CLASS


def _contains(
    geometry: tuple[int, int, int, int], extents: tuple[int, int, int, int]
) -> bool:
    """Whether an element's rectangle sits inside the window's, within slack.

    An element inside a window cannot be bigger than the window. When it is --
    extents of 1920x1080 reported for a widget in a 310x263 window, seen while
    recording a private X server next to a real desktop -- the two answers are
    describing different screens, and only one of them is the one being
    recorded.

    Shared by `DesktopResolver._fits` and `_fits_answer`, which are the two
    things a caller can want from the same check: "keep it unless it is
    impossible" and "keep it only if this corroborates it".
    """
    wx, wy, width, height = geometry
    ex, ey, ewidth, eheight = extents
    return (
        ex >= wx - EXTENTS_SLACK
        and ey >= wy - EXTENTS_SLACK
        and ex + ewidth <= wx + width + EXTENTS_SLACK
        and ey + eheight <= wy + height + EXTENTS_SLACK
    )


def _has_point(rect: tuple[int, int, int, int], x: int, y: int) -> bool:
    """Whether a rectangle contains a point."""
    left, top, width, height = rect
    return left <= x < left + width and top <= y < top + height


_UNPLACED = -(2**31)
"""The x and y AT-SPI reports for a widget that is not on screen: `G_MININT`."""


def _placed(rect: tuple[int, int, int, int]) -> bool:
    """Whether a rectangle says where a widget is, rather than that it is nowhere.

    Position is the signal and size is not. A closed menu's items report
    `(-2147483648, -2147483648, 1, 1)` in mate-calc and
    `(-2147483648, -2147483648, 233, 25)` in pluma -- the same items, the same
    sentinel, but pluma keeps their size. A test on size alone called nine
    closed items showing, and the popup logic built its bounds from them.
    """
    return rect[0] > _UNPLACED and rect[1] > _UNPLACED and rect[2] > 1 and rect[3] > 1


class _NoPopup:
    """The type of `_NO_POPUP`, so that answer can be told from the others."""


_NO_POPUP = _NoPopup()
"""`DesktopResolver._popup_at`'s answer for "no popup has a claim on this point",
which is a different answer from None ("a popup does, and nothing in it is here")."""


@dataclass(frozen=True)
class _Lookup:
    """What one window lookup answered, and when, for a recorded move to reuse."""

    screen: int
    window: Any
    """The pyguitest window that was under the point, or None if nothing was.

    A miss is remembered as well as a hit, for the same interval. A pointer
    crossing bare desktop is the common case a hit-only cache did nothing for,
    and a miss is the expensive answer: the whole lookup, and then the same
    lookup again for the settle-and-retry a click gets.
    """

    at: float


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

    def resolve_window(self, x: int, y: int, screen: int = 0) -> Target:
        """Return the target at this point with no element looked up.

        For events whose element is never read: a pointer move is rendered as a
        coordinate and the origin of the window it was in, and nothing else, so
        the element half of `resolve` is a cost with no buyer -- and it is the
        expensive half, an accessibility hit-test rather than a window list
        lookup. `record_motion` would pay it once per motion event.
        """

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

    def resolve_window(self, x: int, y: int, screen: int = 0) -> Target:
        """Return the point with no context attached; this floor has no windows."""
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
    windows: bool | None = None
    """Whether this recording is of a native Windows desktop. None asks the host.

    The recording's platform and not the machine's: Windows can run an X server
    (Xming, VcXsrv, WSLg), where `backend = "xrecord"` records X clients whose
    windows, elements and pids are all X11's to interpret. `Recorder` passes
    the answer its capture backend gives -- see `recorder._windows_desktop` --
    and None keeps every other caller on the host answer this used to read.
    """

    _identity: dict[Any, _Identity] = field(default_factory=dict, init=False)
    _resolves_elements: bool = field(default=False, init=False)
    _warned: list[str] = field(default_factory=list, init=False)
    _scaled: bool = field(default=False, init=False)
    _motion_cache: _Lookup | None = field(default=None, init=False)
    """The last window lookup, for a recorded move to reuse while it is fresh --
    see `_recent_lookup`. Set by `_window`, so a click leaves the motion path a
    warm answer behind it."""
    _menu_owner: Any = field(default=None, init=False)
    """The live menu or drop-down the pointer was last resolved to, whose popup a
    later point may be inside. See `_popup_at`."""
    _popup_layout: list[tuple[Any, tuple[int, int, int, int], str]] | None = field(
        default=None, init=False
    )
    """The popup's items -- live element, rectangle, role -- as last seen open.
    Kept because by the time a press is consumed the popup it landed in has
    usually closed, and a closed item has no rectangle."""
    _popup_seen: float = field(default=0.0, init=False)
    """When `_popup_layout` was last seen open, on `_now`'s clock."""
    _spending: bool = field(default=True, init=False)
    """Whether the resolve in progress is a press -- see `resolve_hover`. Carried
    here rather than passed down because `_element` is a seam callers replace,
    and its two-argument shape is part of that."""

    def __post_init__(self) -> None:
        """Add this process and its terminal to the ignore set."""
        self.ignore_pids.add(os.getpid())
        self.ignore_pids.update(self._own_terminal_pids())
        self._scaled = self._any_screen_scaled()
        self._resolves_elements = self.elements and self._can_resolve_elements()

    def _on_windows(self) -> bool:
        """Whether this recording is of a native Windows desktop."""
        return is_windows() if self.windows is None else self.windows

    def _session_type(self) -> str:
        """This recording's platform in the vocabulary `platforms` reads.

        `platforms.is_windows(session_type)` takes the text
        `Environment.session_type` stores -- the `str()` of a pyguitest
        `SessionType` -- and looks for the member name inside it. Built from
        the backend here instead, because the recording whose header this
        wording ends up in has not been written yet at this point.
        """
        return "WIN32" if self._on_windows() else "X11"

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

        On Windows the console's own window is found directly too, by asking
        Windows which window hosts this process's console -- see
        `_console_owner_pid` -- and it counts only if that process owns a
        window this session lists, exactly as an ancestor must.
        """
        if self.session is None:
            return set()
        try:
            owners = {window.pid for window in self.session.windows() if window.pid}
        except Exception:  # noqa: BLE001 - no window list is no terminal to find
            return set()
        found: set[int] = set()
        for pid in _ancestor_pids():
            if pid in owners:
                found.add(pid)
                break
        # Asked of Windows as well, because the terminal is not always an
        # ancestor there: a console started from the shell (Win+R `cmd`, a
        # double-clicked script) is handed to Windows Terminal, which is
        # launched by the system rather than by the process that asked for it.
        # Its window then belongs to a process whose parent is `svchost`, so the
        # walk above never meets it -- and the recorder recorded its own console.
        console = _console_owner_pid()
        if console is not None and console in owners:
            found.add(console)
        return found

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
        return self._resolve(x, y, screen, spend=True)

    def resolve_hover(self, x: int, y: int, screen: int = 0) -> Target:
        """`resolve`, for a place the pointer rested rather than one it pressed.

        The same answer, except that it does not use up an open popup: choosing
        an item from a menu closes it, and a rest is consumed alongside the press
        that ended it, so a rest that spent the popup would leave that press with
        the widget underneath. See `_popup_at`. Optional for a resolver -- the
        normalizer falls back to `resolve` where there is none.
        """
        return self._resolve(x, y, screen, spend=False)

    def _resolve(self, x: int, y: int, screen: int, *, spend: bool) -> Target:
        """The window and element under a point; see `resolve`."""
        self._spending = spend
        window = self._window(x, y, screen)
        element = self._element(x, y) if self._resolves_elements else None
        if element is not None and not self._is_widget(element):
            return Target(x=x, y=y, screen=screen, window=window)
        if element is not None and not self._covers(element, x, y):
            return Target(x=x, y=y, screen=screen, window=window)
        if element is not None and not self._belongs(element, window):
            return Target(x=x, y=y, screen=screen, window=window)
        return Target(x=x, y=y, screen=screen, window=window, element=element)

    def resolve_window(self, x: int, y: int, screen: int = 0) -> Target:
        """Return the toplevel under this point, looking for no element in it.

        The window still has to be right: every coordinate rendered from this
        target is an offset into that window's origin, so skipping the window
        too would turn a window-relative move into a bare screen coordinate.
        What is skipped is the accessibility hit-test -- the half nothing reads
        for a move, and the half that costs a round trip to the application.

        What is skipped *after the first event* is the hit test as well: a
        pointer moving inside one window is the common case by far, so
        `_recent_window` answers from the last lookup while it is fresh
        (`MOTION_TRUST_SECONDS`) and this point is still inside the rectangle
        that lookup covered. The rectangle itself is read again every time, so a
        window that moved is noticed exactly as before; what goes away is a
        window list and a geometry read per window on the desktop, hundreds of
        times a second. Measured live before that: 572 motion events over a
        21-second session left the recorder 4.6s behind the hand making it, with
        the input piling up in the capture queue and the stop key answered only
        once the backlog had been worked through.

        Freshness is what keeps that answer honest. Containment alone cannot say
        whether a window stacked *above* the answered one now covers the point,
        and the moves that entered it would be attributed to the window beneath
        for as long as the pointer stayed inside the first one's rectangle.

        Nor does a move wait out a miss. A click that finds no window sleeps and
        asks again, because a window still animating in is worth waiting for --
        but a pointer move is one of hundreds and the next will ask anyway, so it
        takes the first answer, and a miss is remembered for as long as a hit is.
        Bare desktop is where a pointer spends much of its time, and a lookup
        that finds nothing plus two sleeps of 50ms is a tenth of a second per
        move: a recorder that cannot keep up with a hand on the one stretch of
        screen nothing is drawn.
        """
        if self._recently_missed(screen):
            return Target(x=x, y=y, screen=screen)
        window = self._recent_window(x, y, screen)
        if window is None:
            window = self._window(x, y, screen, patient=False)
        return Target(x=x, y=y, screen=screen, window=window)

    def _recent_lookup(self, screen: int) -> _Lookup | None:
        """The last window lookup, if it was on this screen and has not expired."""
        lookup = self._motion_cache
        if lookup is None or lookup.screen != screen:
            return None
        if _now() - lookup.at >= MOTION_TRUST_SECONDS:
            return None
        return lookup

    def _recently_missed(self, screen: int) -> bool:
        """Whether a lookup a moment ago found nothing at all on this screen."""
        lookup = self._recent_lookup(screen)
        return lookup is not None and lookup.window is None

    def _recent_window(self, x: int, y: int, screen: int) -> WindowRef | None:
        """The window the last lookup answered, if it is fresh and covers this point.

        A recorded move needs a window for one reason -- the origin its
        coordinate is rendered against -- and a pointer crossing one window asks
        the same question hundreds of times a second. The rectangle is read
        afresh on every call rather than cached with the identity, so a window
        that moved or resized is answered for where it is *now*, which is what
        the rest of this file depends on: the origin a coordinate is rendered
        against has to be the origin it had when the coordinate was captured.
        Nothing else is re-derived -- the identity was settled the first time
        this window was seen and deliberately does not move, see `_identify`.
        """
        lookup = self._recent_lookup(screen)
        if lookup is None or lookup.window is None:
            return None
        described = self._describe(lookup.window)
        geometry = described.geometry
        if geometry is None:
            return None
        wx, wy, width, height = geometry
        if wx <= x < wx + width and wy <= y < wy + height:
            return described
        return None

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
        target = self._resolve(x, y, screen, spend=False)
        if target.element is None:
            return Observation(target=target)
        return self._state(x, y, target)

    def _state(self, x: int, y: int, target: Target) -> Observation:
        """Read text and checked state off the live element under this point."""
        expected = target.element
        try:
            found = self._live_at(x, y, spend=False)
            element = found[0] if found is not None else None
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
            f"{_scope_phrase(self._on_windows())}; "
            f"{foreign_focus_reason(self._session_type())}"
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
                f"ignored accessible elements that no window "
                f"{_scope_phrase(self._on_windows())} accounts for; "
                f"{foreign_element_reason(self._session_type())}"
            )
            return False
        if window.pid is not None and element.pid is not None:
            return self._same_process(element, window)
        return self._fits(element, window)

    def _same_process(self, element: ElementRef, window: WindowRef) -> bool:
        """Whether the element's process is the window's.

        A real corroboration on Linux, where an element and the window under
        the same point genuinely share a process, so a mismatch means the
        accessibility bus has answered about another login session.

        **Not true on Windows, and not an edge case there.** Every Store app
        splits across two processes: the toplevel is an `ApplicationFrameWindow`
        owned by `ApplicationFrameHost.exe`, and the widgets inside it belong to
        the application. pyguitest documents exactly this on `WINDOW_PID` --
        `GetWindowThreadProcessId` reports the host, and UI Automation reports
        the real process -- so on that platform the two pids differing is the
        *expected* answer for a correctly resolved element.

        Measured on a real recording of Calculator: the window was pid 8824
        (the frame host) and its buttons pid 16672, so this rejected every
        widget in the application. The only element that survived was `Close
        Calculator` on the frame's own title bar, which the host does own --
        which is what made the cause unmistakable.

        So on Windows the pid is not evidence either way, and the question
        falls through to `_windows_mismatch`, which asks the two things that
        can still be answered there: whether this window is one of the frame
        host's -- its class name is `ApplicationFrameWindow`, and
        `Window.app_id` carries the window class on that platform -- and,
        where it is not, whether the element's own rectangle corroborates
        that it belongs.

        Verified rather than assumed is the whole point of that split. A
        mismatch with nothing behind it used to be kept as soon as the
        geometry stopped talking: `_fits` answers True whenever it cannot
        tell -- any scaled screen, or a rectangle the backend never reported
        -- and on a 150% display that is every element from every other
        process on the machine.
        """
        if element.pid == window.pid:
            return True
        if self._on_windows():
            return self._windows_mismatch(element, window)
        self._warn(
            f"ignored an accessible element from pid {element.pid} under a "
            f"window owned by pid {window.pid}; "
            f"{self._mismatch_reason(element)}"
        )
        return False

    def _mismatch_reason(self, element: ElementRef) -> str:
        """Why an element's process is not the one that owns the window under it.

        Two causes that look identical from the pids alone, and are not the same
        problem. The accessibility bus really is scoped to the login session
        rather than the display, so an element can be another session's. But it
        can as easily belong to a window *of this display* that is merely
        underneath the one clicked: the accessible tree carries no stacking
        order, so a hit-test answers for whichever overlapping window has the
        smaller widget there -- pyguitest's `element_at` says as much. Found
        live with two overlapping windows on one private X server, where the
        note blamed another session for an element from the window next door.

        The window list settles it: a process that owns a window here is not
        another session's.
        """
        stacked = self._other_window_of(element)
        if stacked is None:
            return foreign_element_reason(self._session_type())
        return (
            f"that process owns another window on this display "
            f"({stacked.title or stacked.app_id!r}), so this is the hit-test "
            "answering for a window stacked underneath the one clicked -- the "
            "accessible tree has no stacking order"
        )

    def _other_window_of(self, element: ElementRef) -> Any | None:
        """A window on the recorded display owned by the element's process."""
        if element.pid is None or element.pid in self.ignore_pids:
            return None
        try:
            windows = list(self.session.windows())
        except Exception:  # noqa: BLE001 - a diagnostic must not fail a recording
            return None
        return next((w for w in windows if w.pid == element.pid), None)

    def _windows_mismatch(self, element: ElementRef, window: WindowRef) -> bool:
        """Whether an unequal pid on Windows means "same application", or not.

        Two answers mean that. The window is one of the frame host's, which
        explains the mismatch outright -- a Store app's widgets are published
        by the application while its toplevel belongs to
        `ApplicationFrameHost.exe` -- so the element is kept on the same terms
        `_fits` has always applied, geometry included. Or the element's own
        rectangle corroborates that it sits inside the window, which is the
        evidence this already accepts wherever a pid is missing on either
        side, and is what rejects an element from another desktop.

        A mismatch with neither answer is refused, and the recording says why.
        No amount of "probably fine" survives contact with a desktop that is
        not the one being recorded: the accessibility bus is scoped to the
        login session, not to the display, so an element that is not inside
        this window can only have come from somewhere else.
        """
        if _in_a_frame_host(window):
            return self._fits(element, window)
        if self._fits_answer(element, window) is True:
            return True
        self._warn(
            f"ignored an accessible element from pid {element.pid} under a "
            f"window owned by pid {window.pid}: only an "
            "ApplicationFrameWindow explains a pid mismatch on this platform, "
            "and this element's own geometry does not corroborate it either; "
            f"{foreign_element_reason(self._session_type())}"
        )
        return False

    def _fits(self, element: ElementRef, window: WindowRef) -> bool:
        """Whether the element's rectangle could plausibly be in this window.

        The fallback for when a pid is missing on either side, which is common:
        a bare X server with no window manager publishes no `_NET_WM_PID`, and
        not every accessibility bridge answers `get_process_id`.

        Skipped entirely where the geometry cannot be compared, because AT-SPI
        extents and window geometry are then not reliably in the same units and
        a false rejection costs every named element on the desktop. That one
        answer is what `_fits_answer` and this method share and use the
        opposite way round: see it for what the other caller does with it.
        """
        geometry = window.geometry
        extents = element.extents
        if self._scaled or geometry is None or extents is None:
            return True
        if _contains(geometry, extents):
            return True
        _wx, _wy, width, height = geometry
        _ex, _ey, ewidth, eheight = extents
        self._warn(
            f"ignored accessible elements whose {ewidth}x{eheight} extents do "
            f"not fit the {width}x{height} window under the same point; they "
            "describe a different screen from the one being recorded"
        )
        return False

    def _fits_answer(self, element: ElementRef, window: WindowRef) -> bool | None:
        """Whether the element's rectangle is inside the window's, or None.

        None is "the geometry cannot say": any screen scaled, or a rectangle
        missing on either side. It is not collapsed into a bool here because
        the two callers want it differently -- `_fits` keeps the element, since
        refusing on "cannot tell" would cost every named element on a scaled
        desktop, while `_windows_mismatch` refuses it, because a *differing*
        pid is a claim that needs corroboration rather than the benefit of the
        doubt.
        """
        geometry = window.geometry
        extents = element.extents
        if self._scaled or geometry is None or extents is None:
            return None
        return _contains(geometry, extents)

    def _warn(self, message: str) -> None:
        """Record a degradation once, however many events hit it."""
        if message not in self._warned:
            self._warned.append(message)

    def close(self) -> None:
        """Nothing to release: the session belongs to the caller."""

    # -- windows -------------------------------------------------------------

    def _window(
        self, x: int, y: int, screen: int, *, patient: bool = True
    ) -> WindowRef | None:
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

        `patient` says whether that retry is worth its wait. It is for a click,
        which is one event a recording cannot do without. It is not for a
        pointer move, which is one of hundreds and is asked again by the next --
        see `resolve_window`.
        """
        if self.session is None:
            return None
        window = self._resolve_window(x, y, screen)
        if window is None and patient:
            for _ in range(2):
                time.sleep(0.05)
                window = self._resolve_window(x, y, screen)
                if window is not None:
                    break
        # Kept for the motion path, which asks the same question once per
        # pointer event -- see `resolve_window`. A click resolving a window
        # leaves exactly the answer the moves after it would have got, and a
        # miss leaves the answer a run of moves over bare desktop would have.
        if window is None or window.pid in self.ignore_pids:
            self._motion_cache = _Lookup(screen, None, _now())
            return None
        self._motion_cache = _Lookup(screen, window, _now())
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
        if awidth <= 1 or aheight <= 1:
            # A window a pixel wide has no chrome to have been clicked. GTK maps
            # a 1x1 leader window beside every application, and on some window
            # managers that -- untitled, no pid -- is what the active window
            # is. Preferred anyway, its slack put every click within
            # DECORATION_SLACK of the real window's corner onto it, and the
            # File menu of pluma under marco was recorded against a window with
            # neither a title nor a size: found live, and it turned the whole
            # script into absolute coordinates.
            return window
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
            found = self._live_at(x, y, spend=self._spending)
            if found is None:
                return None
            element, rect = found
            described = self._describe_element(element)
            # A popup's item read after the popup closed reports a rectangle at
            # the far corner of the screen, which no point is inside; the one it
            # had while it was open is what the press landed in.
            return replace(described, extents=rect) if rect is not None else described
        except Exception:  # noqa: BLE001 - a stale tree raises freely
            return None

    def _live_at(
        self, x: int, y: int, *, spend: bool
    ) -> tuple[Any, tuple[int, int, int, int] | None] | None:
        """The live accessible under this point, an open popup's items included.

        With the rectangle to describe it by where that is not the element's own:
        see `_element`. The single place an element is looked up, so a click and
        a check made on the same point cannot disagree about what is there.
        """
        popup = self._popup_at(x, y, spend=spend)
        if not isinstance(popup, _NoPopup):
            return popup
        element = self.session.element_at(x, y)
        if element is None:
            return None
        # Not on Windows, where UI Automation hit-tests popups itself and there is
        # nothing to remember: this would be up to `POPUP_WAIT_SECONDS` and a
        # subtree read over UI Automation on every press on a menu, for an answer
        # `_popup_at` never uses there.
        if element.role in MENU_OWNER_ROLES and not self._on_windows():
            if spend:
                # Looked at now, waiting a moment for it: a press is what opens a
                # popup, and this is the last time it can be seen open -- the
                # next press is consumed after the item it chooses has closed it.
                self._menu_owner = element
                self._popup_layout = self._await_popup(element)
                self._popup_seen = _now()
            else:
                # A rest on the entry the popup came from is consumed after the
                # press that opened it, and often after the popup has closed
                # again, so it must not replace what that press remembered with
                # nothing. It only takes over where a popup is really showing --
                # the pointer sliding from one menu-bar entry to the next.
                shown = self._visible_items(element)
                if shown:
                    self._menu_owner = element
                    self._popup_layout, self._popup_seen = shown, _now()
        return element, None

    def _popup_at(
        self, x: int, y: int, *, spend: bool
    ) -> tuple[Any, tuple[int, int, int, int]] | _NoPopup | None:
        """A popup's item here and its rectangle; None if on none; else `_NO_POPUP`.

        `element_at` cannot see into a popup. The popup is a window of its own,
        stacked over the application, and no descendant of the frame the hit
        test starts from, so the point is answered for the widget *underneath*.
        Found live on MATE with pluma's File menu open: `New` came back as the
        toolbar's `Open` button, the recording said `gui.button("Open").click()`,
        and the script that replays it opens a file dialog instead of making a
        document -- clean, plausible, and wrong, because that button is the same
        process as the window and its rectangle does contain the point, which
        is everything the resolver checks. `Save` came back as the tab beneath it.

        The popup's own items are in the tree, though, and are found by looking
        inside the menu that was opened, so the click is named for what it
        pressed. A menu is opened by a click on something whose role says so, so
        the last such element the pointer resolved to is remembered rather than
        every menu in the application being searched on each click.

        **The popup is usually gone by the time the press is consumed.** Choosing
        an item closes the menu, the recorder handles events after they happen,
        and a closed item has no rectangle -- so the first version of this,
        which looked at the popup as it stood, passed every test and changed
        nothing in a live recording: the press was consumed a moment after the
        popup had closed, and the hit test named the toolbar button again. So
        the popup is looked at while it is open (`_live_at`, right after the
        click that opened it) and its layout kept, and a press is answered from
        that layout when the popup itself has gone.

        A kept layout must not outlive its popup, or a later click on the widget
        that was underneath would be answered with a menu item. It is spent by
        the press that chooses an item -- `spend`, which a hover is not, since a
        rest is consumed alongside the press that ended it and must not use the
        popup up first -- and by any point outside the popup, and it expires
        after `POPUP_MEMORY_SECONDS`. Not spent by a press on an entry that opens
        a submenu, which leaves the popup open.

        Inside the popup but on no item -- a separator, a gap -- is answered
        None, which is a coordinate, and never the hit test: whatever it would
        name is under the popup and is not what was clicked.

        Skipped on Windows, where UI Automation hit-tests popups themselves.
        """
        owner = self._menu_owner
        if owner is None or self._on_windows():
            return _NO_POPUP
        shown = self._visible_items(owner)
        if shown:
            self._popup_layout, self._popup_seen = shown, _now()
        elif (
            self._popup_layout is not None
            and _now() - self._popup_seen <= POPUP_MEMORY_SECONDS
        ):
            shown = self._popup_layout
        if not shown:
            self._forget_popup()
            return _NO_POPUP
        left = min(rect[0] for _, rect, _ in shown)
        top = min(rect[1] for _, rect, _ in shown)
        right = max(rect[0] + rect[2] for _, rect, _ in shown)
        bottom = max(rect[1] + rect[3] for _, rect, _ in shown)
        if not _has_point((left, top, right - left, bottom - top), x, y):
            # Only a press outside dismisses a popup. A rest outside it -- on the
            # menu-bar entry it came from, typically -- does not, and is consumed
            # after the press that opened the popup and before the one that
            # chooses from it.
            if spend:
                self._forget_popup()
            return _NO_POPUP
        under = [entry for entry in shown if _has_point(entry[1], x, y)]
        if not under:
            return None
        item, rect, role = min(under, key=lambda entry: entry[1][2] * entry[1][3])
        if spend and role not in MENU_OWNER_ROLES:
            self._forget_popup()
        return item, rect

    def _visible_items(
        self, owner: Any
    ) -> list[tuple[Any, tuple[int, int, int, int], str]]:
        """The items of a menu's popup that have a rectangle, which means it is open.

        A closed item reports a position at the far corner of the screen (see
        `_placed`), so nothing has to be told when a menu opens or closes: an item
        with a real rectangle is showing.
        """
        try:
            items = self.session.elements(
                within=owner, predicate=lambda e: e.role in POPUP_ITEM_ROLES
            )
        except Exception:  # noqa: BLE001 - a menu that closed mid-read is closed
            return []
        shown = []
        for item in items:
            try:
                rect = self._extents(item)
                if rect is not None and _placed(rect):
                    shown.append((item, rect, item.role))
            except Exception:  # noqa: BLE001 - an item can go stale under the read
                continue
        return shown

    def _await_popup(
        self, owner: Any
    ) -> list[tuple[Any, tuple[int, int, int, int], str]] | None:
        """Look for a menu's popup, waiting a moment for the application to open it.

        Bounded by the clock and not by a count of looks, because a look is a
        round trip per item: a drop-down of hundreds of entries would otherwise
        cost that many times over before giving up on a click that never opened
        anything. It always looks once, and never again after the time is up.
        """
        deadline = time.monotonic() + POPUP_WAIT_SECONDS
        while True:
            shown = self._visible_items(owner)
            if shown:
                return shown
            if time.monotonic() >= deadline:
                return None
            time.sleep(POPUP_POLL_SECONDS)

    def _forget_popup(self) -> None:
        """Stop believing any popup is open."""
        self._menu_owner = None
        self._popup_layout = None

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


_GA_ROOTOWNER = 3
"""`GetAncestor`'s flag for the root of the owner chain -- the visible window a
pseudoconsole belongs to, where the console window itself is hidden."""


def _console_owner_pid() -> int | None:
    r"""The pid of the window that hosts this process's console, on Windows.

    Asked of Windows rather than inferred from the process tree, because the
    two disagree exactly where it matters. Under Windows Terminal -- the default
    on Windows 11 -- `GetConsoleWindow` answers a hidden `PseudoConsoleWindow`,
    and `GetAncestor(..., GA_ROOTOWNER)` from it is the visible
    `CASCADIA_HOSTING_WINDOW_CLASS` window, which is Microsoft's own documented
    route to "the terminal I am running in". Measured on Windows 11 with a
    console started through Explorer: the launching chain was `python` ->
    `cmd.exe` -> `explorer.exe` -> `svchost.exe`, and the window's owner was
    `WindowsTerminal.exe`, in none of it.

    Private DLL handles, so the prototypes set here are not written onto the
    shared `ctypes.windll` objects other code in the process reads. None off
    Windows, when there is no console (a GUI launcher, a service), or when any
    call fails: an unanswerable question is not a reason to stop recording.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    try:
        kernel32 = ctypes.WinDLL("kernel32")
        user32 = ctypes.WinDLL("user32")
        kernel32.GetConsoleWindow.argtypes = ()
        kernel32.GetConsoleWindow.restype = wintypes.HWND
        user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    except (OSError, AttributeError):
        return None
    console = kernel32.GetConsoleWindow()
    if not console:
        return None
    owner = user32.GetAncestor(console, _GA_ROOTOWNER) or console
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(owner, ctypes.byref(pid))
    return int(pid.value) or None


def _parent_pids_windows() -> dict[int, int]:
    r"""Pid -> parent pid, from Toolhelp, on Windows.

    `ps` does not exist there, so the Unix reader below returns nothing and
    the recorder stopped excluding its own terminal -- which is the whole
    point of the walk. Seen in a real Windows recording: the generated script
    waited for a window titled `C:\WINDOWS\system32\cmd.exe -
    pyguitest-recorder -o script3.py --save-session session3.json`, the very
    console the recorder was running in, under a title that only exists while
    a recording is being made and so can never match on replay.

    `CreateToolhelp32Snapshot` is the same route pyguitest's own
    `wait_for_process` takes, and `PROCESSENTRY32W` is declared here rather
    than imported from it because a private name in another package is not an
    interface. `dwSize` is *validated* by the API, so a wrong layout is a
    refusal rather than a wrong answer.
    """
    import ctypes

    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return {}

    class _Entry(ctypes.Structure):
        """`PROCESSENTRY32W`: one row of the snapshot, the exe name last."""

        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            # ULONG_PTR, so pointer-width: anything narrower puts every field
            # after it at the wrong offset on 64-bit.
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_int32),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_uint16 * 260),
        ]

    try:
        kernel32 = loader("kernel32", use_last_error=True)
    except OSError:
        return {}
    kernel32.CreateToolhelp32Snapshot.argtypes = (ctypes.c_ulong, ctypes.c_ulong)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel32, name)
        function.argtypes = (ctypes.c_void_p, ctypes.POINTER(_Entry))
        function.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    invalid = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == invalid:
        return {}
    parents: dict[int, int] = {}
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return parents


def _ancestor_pids(limit: int = MAX_ANCESTRY) -> list[int]:
    """This process's ancestors, nearest first, up to `limit` of them.

    Read from `ps` rather than `/proc`, in one call: FreeBSD without
    linprocfs has no `/proc` at all, and this runs once at startup where a
    subprocess costs nothing. An unreadable process table is not fatal --
    the caller still ignores this process itself, as it always has.

    Windows has no `ps`, so the call below raised `FileNotFoundError`, was
    swallowed, and left the ancestry empty -- silently turning off the
    terminal exclusion this exists for. `_parent_pids_windows` is that
    platform's answer.
    """
    if is_windows():
        return _walk_parents(_parent_pids_windows(), limit)
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
    return _walk_parents(parents, limit)


def _walk_parents(parents: dict[int, int], limit: int) -> list[int]:
    """This process's ancestors out of a pid -> parent map, nearest first.

    Bounded by `limit` and by the walk reaching a root, and by `seen`: a
    process table read while processes are exiting can hand back a cycle,
    and a pid whose parent has been reused can point back down its own
    chain. Neither is a reason to hang at startup.
    """
    chain: list[int] = []
    seen = {os.getpid()}
    pid = os.getpid()
    while len(chain) < limit:
        pid = parents.get(pid, 0)
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
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


def _now() -> float:
    """The clock a remembered window lookup ages by.

    Monotonic, so an adjusted system clock cannot make an old answer look new. A
    function rather than a call inline so a test can move time without touching
    the `time` module every other thing in the process reads.
    """
    return time.monotonic()
