"""The canonical event model.

Raw X11 events are never written to Python directly. They are normalized into
the types here first, and the generator only ever sees these. That indirection
is what lets the same recording target a changed pyguitest API, or a capture
backend that is not X11, without re-recording anything.

Two distinctions in this module carry real weight.

`Origin` separates what was *observed* from what the analyzer *inferred* and
what a user *edited in*. Deleting an observed click and deleting an inferred
wait mean different things to an editor, and a diagnostic bundle needs to say
which of the two a failing script came from.

`Target` is why a recording can outlive the coordinates it was made at. Every
pointer event resolves, at record time, to the window and -- where AT-SPI can
answer -- the accessible element under the pointer. The generator prefers the
element, falls back to a window-relative coordinate, and only then emits an
absolute one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from enum import Enum
from typing import Any, ClassVar

__all__ = [
    "Origin",
    "ElementRef",
    "WindowRef",
    "Target",
    "Event",
    "MouseMove",
    "Click",
    "Drag",
    "Scroll",
    "KeyStroke",
    "TextInput",
    "HotKey",
    "Assertion",
    "CHECKS",
    "WindowActivate",
    "WaitForWindow",
    "WaitForElement",
    "WaitForIdle",
    "Sync",
    "Pause",
    "Comment",
    "EVENT_TYPES",
    "event_from_dict",
]


class Origin(Enum):
    """Where an event came from, which decides what editing it means."""

    OBSERVED = "observed"
    """Captured from the input backend; deleting it changes what was recorded."""

    INFERRED = "inferred"
    """Added by the analyzer, such as a synchronization point."""

    MANUAL = "manual"
    """Added or edited by hand in the recorder UI."""


@dataclass(kw_only=True, frozen=True)
class ElementRef:
    """An accessible element identified at record time.

    `path` is the chain of (role, name) pairs from the application root, kept
    so an ambiguous name can be disambiguated by ancestry rather than by
    index. `extents` is the element rectangle when AT-SPI could be trusted for
    it, and is a fallback locator only -- see the module docstring.
    """

    role: str
    name: str = ""
    description: str = ""
    path: tuple[tuple[str, str], ...] = ()
    extents: tuple[int, int, int, int] | None = None
    pid: int | None = None
    """Which process published this element, for cross-checking it against the
    window under the same point. The accessibility bus is scoped to the login
    session rather than to one X display, so the two can disagree."""
    actions: tuple[str, ...] | None = None
    """The AT-SPI actions this element offered when recorded, e.g. ('click',).

    Some toolkits (KDE's QML-based Kickoff menu, at least) publish plain
    labels with no Action interface at all -- `Element.click()` has no
    coordinate path that works without them on a non-GNOME Wayland
    compositor, so a locator built from one would fail every time at replay.
    Checked before the element path is offered for a click; not used to
    gate other locators, which do not need the element to be actionable.

    `None` rather than `()` means "not captured" -- a session saved before
    this field existed -- and is treated as unknown, not as no actions, so
    `--regenerate` on an old `.json` does not downgrade elements that were
    working fine to coordinates just because this was never recorded."""

    @property
    def addressable(self) -> bool:
        """Whether this element can be located by name at replay time."""
        return bool(self.name)

    @property
    def clickable(self) -> bool:
        """Whether AT-SPI offered an action `Element.click()` can invoke.

        Mirrors pyguitest's own fallback match in `AtspiBackend`'s
        `Element.click()` (`a.lower() in ("click", "press")`) -- anything
        looser would claim clickability the replay side cannot make good on.
        `actions is None` (unknown -- see its docstring) counts as clickable,
        the same optimism this locator always had before actions existed.
        """
        if self.actions is None:
            return True
        return any(a.lower() in ("click", "press") for a in self.actions)


@dataclass(kw_only=True, frozen=True)
class WindowRef:
    """A toplevel window as it looked when an event was captured.

    Deliberately not a pyguitest `Window`: that object's identity is a live
    backend handle scoped to one session, which cannot survive being written
    to a file. What survives is the properties a replay can search by.

    `title` is recorded but is the *weakest* of these. Titles drift while the
    window stays put -- GNOME Text Editor renames itself the moment it has
    content -- so `title_stable` records whether this window's title was seen
    to change during the recording, and the generator demotes an unstable
    title in favour of `app_id`.
    """

    title: str = ""
    app_id: str = ""
    pid: int | None = None
    geometry: tuple[int, int, int, int] | None = None
    title_stable: bool = True
    app_id_ambiguous: bool = False
    """Whether another window open at the same moment shared this app_id.

    A single process can own several toplevels at once -- a desktop shell's
    own desktop, panels, and popups, seen live all sharing app_id
    "plasmashell" on KDE -- and `app_id` alone cannot then tell them apart.
    Set when this window was first seen, from a live window list, not
    inferred from anything recorded later.
    """

    @property
    def addressable(self) -> bool:
        """Whether this window can be found again by a stable property."""
        trustworthy_app_id = bool(self.app_id) and not self.app_id_ambiguous
        return trustworthy_app_id or bool(self.title)


@dataclass(kw_only=True, frozen=True)
class Target:
    """What an action was aimed at: a point, and what was under it.

    `x` and `y` are always screen-absolute, because that is what the capture
    backend reports and what a diagnostic needs. Everything else here exists
    so the generator does not have to emit them.
    """

    x: int
    y: int
    screen: int = 0
    window: WindowRef | None = None
    element: ElementRef | None = None

    @property
    def relative(self) -> tuple[int, int] | None:
        """The point relative to the target window's origin, if geometry is known."""
        if self.window is None or self.window.geometry is None:
            return None
        wx, wy, _, _ = self.window.geometry
        return self.x - wx, self.y - wy


@dataclass(kw_only=True)
class Event:
    """One canonical event. Subclasses add the fields their kind needs.

    `timestamp` is seconds since the recording started; `delay` is seconds
    since the previous event, kept separately because an editor that reorders
    or deletes events must be able to rewrite one without recomputing the
    other from wall-clock times that no longer apply.
    """

    kind: ClassVar[str] = "event"

    timestamp: float = 0.0
    delay: float = 0.0
    origin: Origin = Origin.OBSERVED
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict, tagged with the event kind."""
        data = asdict(self)
        data["origin"] = self.origin.value
        data["kind"] = self.kind
        return data


@dataclass(kw_only=True)
class MouseMove(Event):
    """Pointer motion that was judged meaningful on its own."""

    kind: ClassVar[str] = "mouse_move"

    target: Target


@dataclass(kw_only=True)
class Click(Event):
    """A press/release pair, possibly repeated into a double or triple click."""

    kind: ClassVar[str] = "click"

    target: Target
    button: int = 1
    count: int = 1


@dataclass(kw_only=True)
class Drag(Event):
    """A press, motion and release, recorded as one gesture."""

    kind: ClassVar[str] = "drag"

    start: Target
    end: Target
    button: int = 1


@dataclass(kw_only=True)
class Scroll(Event):
    """Wheel movement in detents, in pyguitest's sign convention.

    `dy` positive is up and `dx` positive is right, matching
    `Session.scroll`. Backends that report the opposite convert on capture,
    not here.
    """

    kind: ClassVar[str] = "scroll"

    target: Target
    dx: int = 0
    dy: int = 0


@dataclass(kw_only=True)
class KeyStroke(Event):
    """A single key tapped, named in pyguitest's key vocabulary."""

    kind: ClassVar[str] = "key_stroke"

    key: str
    target: Target | None = None


@dataclass(kw_only=True)
class TextInput(Event):
    """A run of printable keystrokes coalesced into one string."""

    kind: ClassVar[str] = "text_input"

    text: str
    target: Target | None = None
    sensitive: bool = False


@dataclass(kw_only=True)
class HotKey(Event):
    """A modifier combination, as an ordered list of key names."""

    kind: ClassVar[str] = "hotkey"

    keys: tuple[str, ...]
    target: Target | None = None


CHECKS = ("text", "checked", "showing", "window", "nothing")
"""What an `Assertion` can check, decided by what was under the pointer.

`text` and `checked` compare against a value read at record time; `showing`
only requires the element to be there, which is all that can be asked of a
button. `window` is the fallback when no element could be named but a window
could, and `nothing` records that a check was asked for at a point nothing
could be identified at -- kept rather than dropped, because a check the
recorder silently discarded is worse than one it admits it could not make.
"""


@dataclass(kw_only=True)
class Assertion(Event):
    """A check the person recording asked for, on what was under the pointer.

    This is the one event type the user creates deliberately rather than by
    interacting with the application, and it is what separates a replayable
    script from a test: a recording of actions alone passes as long as nothing
    raises, whatever the application actually did.

    `expected` is what the element read at the moment the check was recorded,
    which is the value the generated script will require. `sensitive` marks a
    password field, whose contents are redacted exactly as typed input is.
    """

    kind: ClassVar[str] = "assertion"

    check: str
    target: Target
    expected: str | bool | None = None
    sensitive: bool = False


@dataclass(kw_only=True)
class WindowActivate(Event):
    """Focus moved to another toplevel."""

    kind: ClassVar[str] = "window_activate"

    window: WindowRef


@dataclass(kw_only=True)
class WaitForWindow(Event):
    """Block until a window appears. Inferred from a window mapping after an action."""

    kind: ClassVar[str] = "wait_for_window"

    window: WindowRef
    timeout: float = 10.0
    origin: Origin = Origin.INFERRED


@dataclass(kw_only=True)
class WaitForElement(Event):
    """Block until an element appears. Inferred from a new element after an action."""

    kind: ClassVar[str] = "wait_for_element"

    element: ElementRef
    timeout: float = 10.0
    origin: Origin = Origin.INFERRED


@dataclass(kw_only=True)
class WaitForIdle(Event):
    """Block until a process stops burning CPU. Inferred from a busy gap.

    `window` is what the generator actually renders, because the pid observed
    while recording is meaningless at replay time -- the application will have
    been started again, with a different one. `Window.pid` gives the live
    equivalent, so the window is the durable half and `pid` is kept only as a
    record of what was seen.
    """

    kind: ClassVar[str] = "wait_for_idle"

    window: WindowRef | None = None
    pid: int | None = None
    timeout: float = 30.0
    origin: Origin = Origin.INFERRED


@dataclass(kw_only=True)
class Sync(Event):
    """Round-trip the input stream so injected events are known to have landed."""

    kind: ClassVar[str] = "sync"

    origin: Origin = Origin.INFERRED


@dataclass(kw_only=True)
class Pause(Event):
    """An explicit sleep. The fallback when nothing better can be inferred."""

    kind: ClassVar[str] = "pause"

    seconds: float = 0.0


@dataclass(kw_only=True)
class Comment(Event):
    """A comment line in the generated source. Carries no action."""

    kind: ClassVar[str] = "comment"

    text: str = ""
    origin: Origin = Origin.MANUAL


EVENT_TYPES: dict[str, type[Event]] = {
    cls.kind: cls
    for cls in (
        MouseMove,
        Click,
        Drag,
        Scroll,
        KeyStroke,
        TextInput,
        HotKey,
        Assertion,
        WindowActivate,
        WaitForWindow,
        WaitForElement,
        WaitForIdle,
        Sync,
        Pause,
        Comment,
    )
}


def _rebuild(cls: type, data: Any) -> Any:
    """Rebuild one nested context object from its dict form."""
    if data is None:
        return None
    if cls is Target:
        return Target(
            x=data["x"],
            y=data["y"],
            screen=data.get("screen", 0),
            window=_rebuild(WindowRef, data.get("window")),
            element=_rebuild(ElementRef, data.get("element")),
        )
    if cls is WindowRef:
        geometry = data.get("geometry")
        return WindowRef(
            title=data.get("title", ""),
            app_id=data.get("app_id", ""),
            pid=data.get("pid"),
            geometry=tuple(geometry) if geometry else None,
            title_stable=data.get("title_stable", True),
            app_id_ambiguous=data.get("app_id_ambiguous", False),
        )
    extents = data.get("extents")
    raw_actions = data.get("actions")
    return ElementRef(
        role=data["role"],
        name=data.get("name", ""),
        description=data.get("description", ""),
        path=tuple(tuple(step) for step in data.get("path", ())),
        extents=tuple(extents) if extents else None,
        pid=data.get("pid"),
        actions=tuple(raw_actions) if raw_actions is not None else None,
    )


_CONTEXT_FIELDS = {
    "target": Target,
    "start": Target,
    "end": Target,
    "window": WindowRef,
    "element": ElementRef,
}


def event_from_dict(data: dict[str, Any]) -> Event:
    """Rebuild an event from its serialized form, by its `kind` tag.

    A recording is user-editable JSON -- `--regenerate` exists precisely so
    one can be hand-trimmed and re-rendered -- so a malformed entry here must
    fail with a message that names what is wrong, not a bare `KeyError`/
    `TypeError` from three calls of indirection down in `_rebuild`.
    """
    if not isinstance(data, dict):
        raise ValueError(f"event entry is not an object: {data!r}")
    payload = dict(data)
    kind = payload.pop("kind", None)
    if kind is None:
        raise ValueError(f"event entry has no 'kind': {data!r}")
    cls = EVENT_TYPES.get(kind)
    if cls is None:
        raise ValueError(
            f"unknown event kind {kind!r}; this recorder understands "
            f"{sorted(EVENT_TYPES)}"
        )
    try:
        payload["origin"] = Origin(payload.get("origin", Origin.OBSERVED.value))
        for name, context in _CONTEXT_FIELDS.items():
            if name in payload:
                payload[name] = _rebuild(context, payload[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"malformed {kind!r} event ({exc})") from exc
    if "keys" in payload:
        payload["keys"] = tuple(payload["keys"])
    known = {f.name for f in fields(cls)}
    try:
        return cls(**{k: v for k, v in payload.items() if k in known})
    except TypeError as exc:
        raise ValueError(f"malformed {kind!r} event ({exc})") from exc
