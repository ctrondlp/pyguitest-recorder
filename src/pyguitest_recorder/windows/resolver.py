"""Resolve a screen coordinate into the window and element beneath it.

This is what makes a recording outlive the coordinates it was made at, and it
is the reason the recorder can generate `gui.button("Save").click()` instead
of `gui.move_mouse(180, 90); gui.click()`.

The element half talks to AT-SPI directly rather than through pyguitest.
pyguitest's `Element` deliberately exposes no extents and no hit-testing --
its whole argument is that elements replace coordinates, so asking "which
element is at this point" is a question its API does not have -- but that is
exactly the question a recorder must answer, because a coordinate is all the
input backend gives it. `Atspi.Component.get_accessible_at_point` answers it,
and behaves identically under X11 and Wayland, which is the one part of this
recorder that is not X11-bound.

Everything degrades. No AT-SPI means coordinates with window context; no
window backend means bare coordinates; and a recording made either way still
generates a working script, just a more fragile one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..model import ElementRef, Target, WindowRef

__all__ = ["ContextResolver", "NullResolver", "DesktopResolver"]

MAX_DEPTH = 24
"""Descent limit, so a malformed accessible tree cannot spin forever."""

EXTENTS_SLACK = 8
"""Pixels an element may lie outside its window before it is disbelieved.

Client-side decorations put a widget a few pixels past the frame the window
manager reports, which is ordinary. Being *larger than the whole window* is
not, and that is what this catches.
"""


@runtime_checkable
class ContextResolver(Protocol):
    """Answers what a screen coordinate points at."""

    def resolve(self, x: int, y: int, screen: int = 0) -> Target:
        """Return the target at this point, with whatever context is available."""

    def close(self) -> None:
        """Release anything held open."""


@dataclass
class NullResolver:
    """Resolves to bare coordinates. The floor every other resolver falls back to."""

    def resolve(self, x: int, y: int, screen: int = 0) -> Target:
        """Return the point with no context attached."""
        return Target(x=x, y=y, screen=screen)

    def close(self) -> None:
        """Nothing is held open."""


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

    _titles: dict[str, set[str]] = field(default_factory=dict, init=False)
    _atspi: Any = field(default=None, init=False)
    _warned: list[str] = field(default_factory=list, init=False)
    _scaled: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """Add this process to the ignore set and open AT-SPI if wanted."""
        self.ignore_pids.add(os.getpid())
        self._scaled = self._any_screen_scaled()
        if self.elements:
            self._atspi = _open_atspi(self._warned)

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
        element = self._element(x, y) if self._atspi is not None else None
        if element is not None and not self._covers(element, x, y):
            return Target(x=x, y=y, screen=screen, window=window)
        if element is not None and not self._belongs(element, window):
            return Target(x=x, y=y, screen=screen, window=window)
        return Target(x=x, y=y, screen=screen, window=window, element=element)

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
        """Find the toplevel under the point, falling back to the active one."""
        if self.session is None:
            return None
        window = self._window_at(x, y, screen) or self._active_window()
        if window is None:
            return None
        if window.pid in self.ignore_pids:
            return None
        return self._describe(window)

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
        """Snapshot a pyguitest Window, tracking whether its title is stable.

        A title seen to change while the same app id stays put marks that
        window unstable, and the generator demotes the title accordingly.
        Titles drift constantly -- an editor appends its document name, a
        browser follows the tab -- and matching on one is the single most
        common reason a generated script stops finding its window.
        """
        key = window.app_id or window.title
        seen = self._titles.setdefault(key, set())
        if window.title:
            seen.add(window.title)
        return WindowRef(
            title=window.title,
            app_id=window.app_id,
            pid=window.pid,
            geometry=self._geometry(window),
            title_stable=len(seen) <= 1,
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
        """Descend the accessible tree to the deepest element at this point."""
        atspi = self._atspi
        try:
            node = self._deepest(x, y)
        except Exception:  # noqa: BLE001 - AT-SPI raises freely on stale nodes
            return None
        if node is None:
            return None
        try:
            return ElementRef(
                role=node.get_role_name(),
                name=node.get_name() or "",
                description=node.get_description() or "",
                path=_ancestry(node),
                extents=_extents(atspi, node),
                pid=_process_id(node),
            )
        except Exception:  # noqa: BLE001
            return None

    def _deepest(self, x: int, y: int) -> Any:
        """Walk applications, then descend the first frame containing the point."""
        atspi = self._atspi
        desktop = atspi.get_desktop(0)
        for index in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(index)
            if app is None:
                continue
            node = self._descend_app(app, x, y)
            if node is not None:
                return node
        return None

    def _descend_app(self, app: Any, x: int, y: int) -> Any:
        """Descend one application's frames to the element at the point."""
        atspi = self._atspi
        for index in range(app.get_child_count()):
            frame = app.get_child_at_index(index)
            if frame is None:
                continue
            hit = atspi.Component.get_accessible_at_point(
                frame, x, y, atspi.CoordType.SCREEN
            )
            if hit is None:
                continue
            return _descend(atspi, hit, x, y)
        return None


def _descend(atspi: Any, node: Any, x: int, y: int) -> Any:
    """Follow get_accessible_at_point down to the deepest hit."""
    for _ in range(MAX_DEPTH):
        child = atspi.Component.get_accessible_at_point(
            node, x, y, atspi.CoordType.SCREEN
        )
        if child is None:
            return node
        node = child
    return node


def _process_id(node: Any) -> int | None:
    """The process an accessible belongs to, where the bridge publishes one."""
    try:
        pid = int(node.get_process_id())
    except Exception:  # noqa: BLE001 - not every bridge answers this
        return None
    return pid or None


def _extents(atspi: Any, node: Any) -> tuple[int, int, int, int] | None:
    """Read an element's screen rectangle, which Wayland may not answer."""
    try:
        rect = atspi.Component.get_extents(node, atspi.CoordType.SCREEN)
    except Exception:  # noqa: BLE001
        return None
    if rect is None or rect.width <= 0 or rect.height <= 0:
        return None
    return (rect.x, rect.y, rect.width, rect.height)


def _ancestry(node: Any) -> tuple[tuple[str, str], ...]:
    """The (role, name) chain from the application root down to this element.

    Kept so an ambiguous name can be disambiguated by where it sits rather
    than by index, which is the locator that breaks the moment a toolbar
    gains a button.
    """
    path: list[tuple[str, str]] = []
    current = node
    for _ in range(MAX_DEPTH):
        try:
            parent = current.get_parent()
        except Exception:  # noqa: BLE001
            break
        if parent is None:
            break
        try:
            path.append((parent.get_role_name(), parent.get_name() or ""))
        except Exception:  # noqa: BLE001
            break
        current = parent
    return tuple(reversed(path))


def _open_atspi(warnings: list[str]) -> Any:
    """Import AT-SPI, or record why element resolution is unavailable."""
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as exc:
        warnings.append(f"element resolution off: AT-SPI unavailable ({exc})")
        return None
    # `get_desktop` alone is not a probe: it hands back a desktop object
    # without contacting anything, so it succeeds against a bus whose registry
    # is dead and element resolution is then switched on for a tree that can
    # never answer. Counting the children is the first call that actually
    # talks to `org.a11y.atspi.Registry`. A count of zero is not a failure --
    # applications register when they start, which may be after this runs.
    try:
        Atspi.get_desktop(0).get_child_count()
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"element resolution off: no accessibility bus ({exc})")
        return None
    return Atspi
