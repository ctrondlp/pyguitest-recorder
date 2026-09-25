"""Render canonical events as pyguitest source.

Everything emitted here is checked against the installed pyguitest: the
generator asks `pyguitest.Session` whether a method exists before it will
emit a call to it, and `validate` compiles the finished file, checking `gui.*`
calls against `Session` and attribute reads on an element against `Element` --
the two surfaces a generated script calls through. A recorder whose output
does not import is worse than no recorder, and the failure mode is silent --
a plausible-looking script that names a function the library does not have.

Three emission rules carry the design.

*Elements lead, coordinates follow.* A click on a widget AT-SPI could name
becomes `gui.button("Save").click()`, not a coordinate -- and where the
element offers a selection rather than an activation, as a radio button, a
page tab, a list row and a tree item all do, it becomes
`gui.tree_item("Q1").select()` instead. Only an element that offered neither
falls to a coordinate: KDE's QML-based Kickoff menu publishes labels with no
click or press action, where `Element.click()` would fail at replay on every
compositor without GNOME's ponytail daemon. Coordinates are the last resort
otherwise, in the order window-relative then absolute, because a coordinate is
the one locator guaranteed to break when the window moves.

*Scripts declare what they need.* Every generated file opens with
`gui.require(...)` naming the capabilities it uses. A recording made on X11
and replayed on Wayland should fail on the first line with a typed exception,
not halfway through with a click that went nowhere.

*Waits are synchronization, not sleeps.* The analyzer infers wait events; this
module renders them. `gui.wait(...)` appears only where nothing better could
be inferred, and says so in a comment.

*Public API only, and nothing of its own.* Every call in a generated script is
a method or property pyguitest publishes, and the only thing the file defines
is `main`. A private name in the output is a dependency on an implementation
detail, and a helper function written into a script is extensibility in the
wrong repository: the library the script runs against is pyguitest, so that is
where a capability belongs. `tests/test_generator.py` asserts both, because
the generator once wrote private helpers into every script that needed one
(`_HELPER_SOURCE`, removed in 0.2.0).
"""

from __future__ import annotations

import ast
import builtins
import keyword
import math
import re
import shutil
import subprocess
import textwrap
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, fields
from typing import Literal

from ..model import (
    Assertion,
    Click,
    Comment,
    Drag,
    ElementRef,
    Event,
    HotKey,
    KeyStroke,
    MouseMove,
    Pause,
    Recording,
    Scroll,
    Sync,
    Target,
    TextInput,
    WaitForElement,
    WaitForIdle,
    WaitForWindow,
    WindowActivate,
    WindowRef,
    describe_assertion,
)
from ..platforms import element_api, plain_name

__all__ = [
    "GeneratorOptions",
    "PythonGenerator",
    "generate",
    "validate",
    "ValidationError",
]

PROFILE = "pyguitest-0.12"
"""The API profile this generator targets, recorded in the output header.

Bumped with the pyguitest whose surface the emitted calls were actually
checked against, not with this package's own version. It is what tells a
reader of a two-year-old generated script which API it was written for, and
what `--regenerate` re-renders against when that API has moved on.
"""

# AT-SPI roles pyguitest gives a dedicated accessor. Anything else is reached
# through the general `element(role=..., name=...)` form.
#
# "button" is the same role as "push button": at-spi2 renamed the enum member
# without changing its integer, so which string a recording carries depends on
# the version it was made against. at-spi2 2.61.1 emits only "button", so
# without this entry the sugar never fired on a current desktop and every
# recorded button came out as the longhand `element()` form.
_SUGAR = {
    "push button": "button",
    "button": "button",
    "check box": "checkbox",
    "combo box": "dropdown",
    "menu item": "menu_item",
    "link": "link",
}

# The `gui.*` calls that hand back an Element, so `validate` can check what is
# called on one against the installed `Element` -- see _unknown_api. The sugar
# names come from _SUGAR rather than being listed again: an accessor added there
# is covered the moment it can be emitted. The rest are the other ways a
# pyguitest script gets an Element, which a hand-edited generated file may use.
_ELEMENT_FACTORIES = frozenset(_SUGAR.values()) | {
    "element",
    "text_field",
    "window_element",
    "root_element",
    "element_at",
}

_TEXT_ROLES = frozenset({"entry", "text", "password text"})

# Roles whose click *is* a selection, so `select()` is tried ahead of
# `click()` where the element offers both. Windows publishes `do default
# action` on nearly everything, and a tree row's default action there is a
# double click -- it toggles the row rather than selecting it. Not every
# selectable role: an AT-SPI menu item carries SELECTABLE too, and selecting
# one only highlights it where the recording chose it.
_SELECT_ROLES = frozenset(
    {"radio button", "page tab", "tree item", "list item", "table row", "table cell"}
)

# Role value -> the Role enum member name, for readable output.
_ROLE_CONSTANTS = {
    "push button": "PUSH_BUTTON",
    "button": "PUSH_BUTTON",
    "toggle button": "TOGGLE_BUTTON",
    "check box": "CHECK_BOX",
    "radio button": "RADIO_BUTTON",
    "link": "LINK",
    "text": "TEXT",
    "entry": "ENTRY",
    "password text": "PASSWORD_TEXT",
    "spin button": "SPIN_BUTTON",
    "combo box": "COMBO_BOX",
    "list": "LIST",
    "list item": "LIST_ITEM",
    "menu": "MENU",
    "menu item": "MENU_ITEM",
    "check menu item": "CHECK_MENU_ITEM",
    "radio menu item": "RADIO_MENU_ITEM",
    "page tab": "PAGE_TAB",
    "table cell": "TABLE_CELL",
    "tree item": "TREE_ITEM",
    "slider": "SLIDER",
    "icon": "ICON",
    "label": "LABEL",
}


_RESERVED = frozenset({"gui", "pyguitest", "os", "re", "time", "Capability", "Role"})
"""Names the generated module binds itself, which a window must not take.

A window titled "gui" would otherwise produce `gui = gui.wait_for_window(...)`,
rebinding the session over itself. That compiles, passes validation, and then
fails several lines later with an unrelated AttributeError.
"""

_BUTTONS = {2: "middle", 3: "right"}
"""Button numbers named, for the comment explaining a coordinate fallback."""

_SENDKEYS_SPECIAL = frozenset("^%+~#&(){}")
"""Characters send_keys reads as syntax; braced when one is the key itself."""

_UNATTRIBUTED_SETTLE = 0.3
"""Seconds waited before a pointer action whose target resolved to no window
at all.

Reproduced live on KDE: dismissing GNOME Text Editor's own in-window
"Discard changes?" sheet with two clicks close together in time recorded
with zero window attribution both times -- the *recording's* resolver came
up with nothing, and a user's own recorded pause was too short to notice
(under `normalize.py`'s `pause_threshold`), so nothing told the generator
this transition needed a moment. `target.window is None` is the one case a
generated script has *nothing* grounding the point in -- not a window, not
an element -- so it is also the one case worth a small unconditional safety
margin rather than trusting the point is already valid the instant it is
used.
"""

_AWAITS_A_KEY_ACTION = (TextInput, KeyStroke, HotKey, Click, Drag, Scroll)
"""Events that deliver input, and so must not arrive before the window the
keystroke before them opened.

Deliberately not `MouseMove`: positioning the pointer is harmless against a
window that has not appeared, and the click that follows the move is in this
tuple and carries the wait instead. Deliberately not `Pause` or any of the
explicit waits, which are already the gap they stand for.
"""

_KEY_ACTION_CONSUMING_WAITS = (Pause, WaitForIdle)
"""Events that already sleep for the interval a keystroke left behind.

`Pause` *is* a recorded idle, and `WaitForIdle` waits on the application, so
either way the seconds are in the script and the settle below would be a
second helping of the same gap. Deliberately not the waits for a window or an
element: those return as soon as the thing is there, which for a window that
is already open is immediately, so they do not stand for the gap -- the same
reason `_SEPARATES_CLICKS` leaves them out.
"""

_SEPARATES_CLICKS = (
    Pause,
    WaitForIdle,
    Sync,
    TextInput,
    KeyStroke,
    HotKey,
    Drag,
    Scroll,
    WindowActivate,
)
"""Events that genuinely put time between two clicks, for the settle below.

Deliberately a list of what *does* separate rather than "anything that is
not a click". Binding a window does not: `gui.expect_window(...)` for a
window that is already open -- the desktop, a panel -- returns immediately,
and a `WaitForWindow` for one is rendered as that same instant lookup.
Treating it as separation is what left a MATE Applications menu with no
pause at all between the click that opens it and the click meant to pick an
item out of it: the second click landed before the menu had drawn, the menu
took the click as a dismiss, and the application never started. Found live.
A comment is not separation either, for the same reason -- it emits no code.
"""

_HOVER_WAIT_CAP = 2.0
"""Longest wait a recorded hover is replayed as, in seconds.

A hover only has to last until the interface responds to it -- a submenu
opens in a fraction of a second. Past that the pointer was resting because
the person was reading, or thinking, or answering the door, and replaying
that faithfully would only make the script slow.
"""

_COORDINATE_CLICK_SETTLE = 0.5
"""Seconds inserted between two coordinate clicks `normalize.py` recorded as
separate, back to back, with nothing rendered in between.

Reproduced live on GhostBSD/MATE: `double_click_interval` (0.4s) is how
close together two clicks have to be for `normalize.py` to merge them into
one double click, but only a gap of `pause_threshold` (1.0s) or more becomes
an explicit `Pause`/`gui.wait(...)`. A real gap in between -- long enough
that two clicks were genuinely separate, too short to be worth a comment --
falls through both and is silently dropped, so the generated script issues
them back to back. Replayed against a real recording of clicking a GNOME
Text Editor window's close button three times, that turned into a
double-click on the *app's own* header bar a fraction of a second later than
intended -- close was never given a chance to register before the pair
after it read as one gesture, and it toggled maximize instead. Fixed here,
not in `normalize.py`: the dropped gap is real but its exact duration is not
worth reconstructing, only that replay must not let two clicks the recording
already decided were separate collapse back into one on replay.
"""

_KEY_ACTION_SETTLE_FLOOR = 0.15
_KEY_ACTION_SETTLE_CAP = 1.0
"""The window a dropped gap after a keystroke is restored within.

`normalize.py` turns an idle of `pause_threshold` (1.0s) or more into an
explicit `Pause`. Anything shorter is dropped entirely -- and a keystroke is
exactly where that costs a replay, because a chord routinely *opens* something
the next line then types into. `Win+R`, `Ctrl+O`, `Alt+F4`, or an Enter that
submits a dialog all put a window on screen that was not there a moment ago.

Measured on a real Windows 11 recording of `Win+R`, `cmd`, Enter: the recorded
gaps were 0.59s, 0.53s, 0.69s and 0.49s -- every one of them real, every one
under the threshold, so all four were dropped and the script fired the chord,
the text and the Return back to back. Replayed, the Run dialog had not taken
focus before the text arrived, so it stayed open and no console appeared. The
reported symptom was "it acts like it didn't tap Return, or did it too fast",
which is precisely right.

The *recorded* gap is restored rather than a fixed constant, because the
recording already knows it -- `_COORDINATE_CLICK_SETTLE` had to invent 0.5s
only because the gap it stands for is genuinely unknowable by then. The floor
keeps a run of held-down or fast-repeated keys (arrow navigation, a typed
accelerator) from gaining a wait between every pair; the cap keeps a long
think before the next keystroke from becoming a long sleep, which is what
`pause_threshold` is for and what a `Pause` would already have said.
"""

_WAYPOINT_TOLERANCE = 8
"""Pixels a recorded path may sit off the line between its neighbours and still
be dropped as detail, when a route is being replayed as waypoints.

The same number as the analyzer's `motion_threshold`, deliberately: that is
already this codebase's line between "the pointer meant this" and "the pointer
was passing through", drawn at 8px to tell a click from a drag. A recorded path
is mostly samples of travel between two or three real turns, so thinning to it
is what turns hundreds of motion events into a handful of corners.
"""

_NATURAL_MOTION = "move_mouse_naturally"
"""The Session method a shaped move needs, asked of the installed pyguitest.

Not every pyguitest has it -- it is newer than the release this package's floor
names -- so a session without it renders the teleport it would have emitted
anyway, and says so in a warning, rather than emitting a call that imports
cleanly and fails at replay. Same degradation `ELEMENT_GEOMETRY` already gets.
"""

_MODULE_DUNDERS = frozenset({"__name__", "__file__", "__doc__", "__spec__"})
"""Names every module is given without assigning them, for the unbound check."""


class ValidationError(Exception):
    """Generated source that does not compile, or names a function pyguitest lacks."""


@dataclass
class GeneratorOptions:
    """How to render a recording. Mirrors the CLI and configuration file."""

    locators: Literal["element", "relative", "absolute"] = "element"
    """Preferred locator. Each falls back to the next on the way to absolute."""

    motion: Literal["teleport", "natural", "recorded", "verbatim"] = "teleport"
    """How to render a pointer move. Off is the old behaviour, deliberately.

    `teleport` is `gui.move_mouse()`, exactly as recorded: the pointer is never
    anywhere in between. Fine for a click, and wrong for anything that watches
    the pointer on its way -- a hover reveal, a hot corner, a menu that opens
    on the approach. This stays the default because `_move` also positions the
    pointer before a click or a scroll, where the path was incidental: shaping
    those would put a derived 0.25-1.5s in front of every click in the script,
    which is the slowdown `_HOVER_WAIT_CAP` exists to avoid.

    `natural` is `gui.move_mouse_naturally()`, one call carrying the
    recording's own endpoints and a path pyguitest shapes -- the same script
    length as a teleport, and a much better account of the travel. Turn it on
    for a recording whose hover and approach behaviour matters.

    `recorded` goes further and puts the route itself back, as `via=` waypoints
    thinned to the corners that mattered. It is one of the two that can make a
    script long, which is why it is asked for rather than assumed.

    `verbatim` is every position the recording has, each with the wait that
    preceded it -- the only value that replays the *timing* too, since every
    other one drops it and leaves the travel to a shaper or to no clock at all.
    That is what a gesture whose meaning is in when the pointer was where needs:
    a menu row that opens its submenu once the pointer has hovered it, and pops
    down when the pointer leaves for long enough, is decided by those intervals
    and not by the route alone. It is the longest by a wide margin -- a few
    hundred positions per few seconds of motion, so a thousand-line script for
    one menu trip -- and it wants `record_motion` for anything to replay.
    """

    max_waypoints: int = 32
    """Cap on the `via` points under `motion = "recorded"`; 0 or less is no cap.

    A generated script is meant to be one a person would have been willing to
    write, and a hundred-element `via` is not that however accurate it is.

    Set above what thinning actually produces, deliberately. Douglas-Peucker at
    8px turns the worst realistic route measured -- 500 samples of a 180px
    curve -- into 29 points on its own, so this is a backstop and not a budget
    the setting lives inside. It matters because the cap can only ever *lose*
    shape: a 12-point zigzag came out as a straight line at 8, since reaching
    that count means dropping the corners, and a zigzag has nothing else.
    """

    capability_preamble: bool = True
    comments: bool = True
    function_name: str = "main"
    default_timeout: float = 10.0
    redact_sensitive: bool = True
    include_header: bool = True

    header: str = ""
    """Text of the caller's own to open the file with, above the detail."""

    format_output: bool = True
    """Run `ruff format` over the result. Silently skipped when ruff is absent."""

    suppress_keymap_warning: bool = False
    """Silence pyguitest's own `KeymapWarning` at replay.

    uinput text injection depends on the replay machine's keyboard layout
    matching the one it was recorded on -- real signal worth seeing at
    least once, so this defaults off. On, the generated script filters it
    out via the standard `warnings` module.
    """

    suppress_atspi_chatter: bool = False
    """Silence GLib's "dbind" log domain -- AT-SPI's own registry chatter,
    not pyguitest's, e.g. a stray "GetItems ... Object does not exist" about
    an unrelated application's stale accessible object. Confirmed background
    noise on a desktop where the accessibility bus is not scoped to one
    display, not a signal of anything wrong with the replay -- but this
    installs a process-wide GLib log handler with no way to undo it, so it
    defaults off rather than silencing a domain a caller might want to see
    messages from for an unrelated reason.
    """


_ElementKey = tuple[str, str, tuple[tuple[str, str], ...]]
"""An element's (role, name, ancestry path) -- what tells two mentions of the
same widget apart from two different widgets that happen to share a role and
a name.

Known limitation: two genuinely distinct widgets that also share their full
ancestry path -- role and name identical at every level, such as repeated
rows in a list with no per-row identifying name -- are indistinguishable by
this key and collapse onto one `_ElementKey` in `_compute_element_scopes`,
so the second one silently loses its collision warning and `within=`
scoping. Nothing in `ElementRef` currently carries a signal that would tell
them apart without also risking false collisions for a genuinely repeated
reference to the same widget (e.g. `extents`, which changes if the window
moves between two clicks on it). Resolving this needs a stable per-node
identity from the accessibility layer, which pyguitest does not expose
today -- see the PR #3 review thread this was flagged in."""


@dataclass
class _State:
    """Mutable bookkeeping for one render pass."""

    lines: list[str] = field(default_factory=list)
    capabilities: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    windows: dict[tuple[str, str, int | None], str] = field(default_factory=dict)
    geometry_for: str | None = None
    geometry_origin: tuple[int, int] | None = None
    pointer: str | None = None
    natural_motion: bool = False
    """Whether the installed pyguitest has the shaped-move method.

    Computed once per render rather than asked per move: the answer belongs to
    the installed library, and asking imports pyguitest and walks its
    namespace -- which a recording with hundreds of motion events would
    otherwise do hundreds of times.
    """
    focused_window: str | None = None
    """The window variable the script has most recently focused, so keyboard
    input can confirm focus once per switch rather than before every key.
    See `_focus_before_typing`."""
    bare_click_pending: bool = False
    """Whether the last line emitted was a coordinate click with nothing --
    no wait, no other action -- after it. See _COORDINATE_CLICK_SETTLE."""
    session_type: str = ""
    """The recording's own session type, for naming what that platform has.

    The recording's rather than this machine's: regenerating a Windows
    recording on Linux is ordinary, and a comment in the output naming AT-SPI
    would be describing the wrong desktop. See `platforms.element_api`."""
    key_action_at: float | None = None
    """When the last keystroke or chord was rendered, or None if none was.

    A chord is the one action most likely to have put something new on screen
    that the next line then addresses, and the gap that let it appear is the
    one `normalize.py` drops when it falls under `pause_threshold`. See
    `_KEY_ACTION_SETTLE_FLOOR`.

    A timestamp rather than the flag this used to be, because the wait is owed
    to the *keystroke* while every event's `delay` is the interval to the one
    before it: a click 0.2s after a move that was itself 0.7s after the chord
    read as 0.2s, under the floor, on an interval the recording had spent 0.9s
    on. `_settle_after_key_action` subtracts the two timestamps instead.
    """
    replayed_until: float | None = None
    """The recording time the script has replayed up to, under `verbatim`.

    None until the first event is rendered -- the recording's own opening gap
    is not something a script should sleep through, and there is no earlier
    statement to measure it from. A hover advances this past its own wait, so
    the position that leaves the rest is not charged for the dwell twice. Only
    `_wait_for_gap` reads it.
    """
    secrets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    element_scopes: dict[_ElementKey, tuple[str, str]] = field(default_factory=dict)
    """Ambiguous element identity -> the ancestor that disambiguates it.

    Computed once, over the whole recording, before rendering starts -- see
    `_compute_element_scopes`. Empty for a recording with no colliding
    (role, name) pair, which is the common case and costs nothing extra.
    """

    scope_vars: dict[tuple[str, str], str] = field(default_factory=dict)
    """Ancestor (role, name) -> the variable it was bound to, so two elements
    needing the same scoping container share one lookup rather than
    searching for it twice."""


@dataclass(frozen=True)
class _MotionRun:
    """Consecutive pointer positions that were really one movement.

    Only ever built from motion recorded in its own right (`record_motion`),
    where a MouseMove with no dwell is a position and nothing else. A MouseMove
    that *is* a hover carries a dwell and is never folded into one of these:
    the rest is the whole point of it, and collapsing a run across it would
    drop the submenu the recording depended on.
    """

    moves: tuple[MouseMove, ...]

    touches_a_rest: bool = False
    """Whether the pointer had settled at either end of this movement.

    The one thing a recording can say about whether a movement was *steering*
    or merely travelling: a run that begins or ends where the pointer came to
    rest was made by someone positioning it, so where it went on the way is
    part of what the recording did. `_emit_motion_run` keeps the route for one
    of these whichever shaping was asked for.
    """

    @property
    def last(self) -> MouseMove:
        """Where the movement ended -- the only position it has to reach."""
        return self.moves[-1]


def _flush_run(
    run: Sequence[MouseMove], *, after_a_rest: bool, before_a_rest: bool
) -> list[Event | _MotionRun]:
    """One movement for a run of positions, or the positions if there is one.

    A single position is not a run: rendering it through the same path as a
    real one would add a call that means nothing.

    `after_a_rest` and `before_a_rest` say whether the event on either side of
    the run was a hover, which is what `_MotionRun.touches_a_rest` is built
    from -- the run is being described by its neighbours, so it has to be told
    about them here rather than left to work them out later.
    """
    if len(run) < 2:
        return list(run)
    return [_MotionRun(tuple(run), touches_a_rest=after_a_rest or before_a_rest)]


def _same_frame(first: Target, second: Target) -> bool:
    """Whether two points share a screen, a window, and a window origin.

    The origin is the one that is not obvious. A relative coordinate is an
    offset into where its window was *at the moment it was captured*, and one
    `via=[...]` renders every point of it against a single `window_x`/`window_y`
    read -- so a window that moved partway through the points would have its
    earlier ones measured from an origin the script no longer has.
    `_ensure_geometry` exists for that same reason within a single event; this
    is the version of it that spans several.
    """
    return (
        first.screen == second.screen
        and first.window == second.window
        and getattr(first.window, "geometry", None)
        == getattr(second.window, "geometry", None)
    )


_VERBATIM_WAIT_FLOOR = 0.001
"""Shortest recorded gap under `motion = "verbatim"` still worth a statement.

A pointer's own sample spacing lands around 6-8ms, so anything at or above a
millisecond is real travel time and is replayed. Below it there is nothing to
replay: `time.sleep` does not deliver sub-millisecond precision, and a
`gui.wait(0.000)` between two moves would be a line of pure noise. Zero would
still be the recorded order of the events, which is what the rest of the
generator already gives without the wait.
"""

_VERBATIM_HOVER_FLOOR = 0.005
"""Least of a hover's wait still worth a statement under `motion = "verbatim"`.

A hover's wait is written to two decimals, so anything under half a hundredth
would read `gui.wait(0.00)`. What is left of a rest that small is what the
script has already replayed through the rest's own positions -- see
`PythonGenerator._unreplayed_dwell` -- and a line that sleeps for nothing is
noise.
"""


def _shaped(motion: str) -> bool:
    """Whether a `motion` value asks for a shaped move at all.

    Only the two shaped values do. `verbatim` deliberately does not: it is the
    recorded positions and nothing else, so there is no path for pyguitest to
    invent between them. An unrecognised value falls back to the recorded
    behaviour rather than silently selecting one of the shaped ones --
    `load_settings` rejects an unknown *key*, but nothing validates a *value*,
    so `motion = "naturl"` reaches here intact.
    """
    return motion in ("natural", "recorded")


def _is_rest(event: Event) -> bool:
    """Whether this event is the pointer coming to rest -- what a dwell says."""
    return isinstance(event, MouseMove) and event.dwell > 0


def _group_motion(
    events: Sequence[Event], options: GeneratorOptions
) -> list[Event | _MotionRun]:
    """Fold runs of dwell-less MouseMoves into one movement each.

    Returns the events untouched unless the setting asks for a shaped move,
    which renders every position it was handed, one `gui.move_mouse` per line
    -- the behaviour every recording got before this option existed. `verbatim`
    takes that path too, and keeps the recorded spacing between those lines;
    see `_wait_for_gap`.

    Otherwise a run of positions becomes a single movement: `natural` keeps
    only where it ended, and `recorded` keeps the route between. Both are
    strictly shorter than the wall of teleports, which is the point -- that
    wall is what `record_motion` costs today.

    Each movement is told whether the pointer had settled on either side of it
    (`_MotionRun.touches_a_rest`). Only a dwell says that: a rest is where the
    pointer stopped, so a run next to one is a move made from or to a place
    the pointer was being *aimed* at. Anything else leaves the flag as it was,
    because only a position moves the pointer -- and the asymmetry is
    deliberate, since keeping a route is faithful and dropping one is a guess.
    """
    if not _shaped(options.motion):
        return list(events)
    grouped: list[Event | _MotionRun] = []
    run: list[MouseMove] = []
    settled = False
    for event in events:
        if _is_rest(event) or not isinstance(event, MouseMove):
            grouped.extend(
                _flush_run(run, after_a_rest=settled, before_a_rest=_is_rest(event))
            )
            run = []
            grouped.append(event)
            settled = _is_rest(event)
            continue
        if run and not _same_frame(run[-1].target, event.target):
            # A window moved under the run, so this position belongs to the
            # next one. What precedes that one is a position, not whatever
            # settled before this one did.
            grouped.extend(_flush_run(run, after_a_rest=settled, before_a_rest=False))
            run = []
            settled = False
        run.append(event)
    grouped.extend(_flush_run(run, after_a_rest=settled, before_a_rest=False))
    return grouped


def _off_line(point: Target, start: Target, end: Target) -> float:
    """How far `point` sits off the straight line from `start` to `end`."""
    ax, ay, bx, by, px, py = start.x, start.y, end.x, end.y, point.x, point.y
    span = math.hypot(bx - ax, by - ay)
    if not span:
        return math.dist((px, py), (ax, ay))
    return abs((px - ax) * (by - ay) - (py - ay) * (bx - ax)) / span


def _straight(targets: Sequence[Target]) -> bool:
    """Whether a route never turned, within the thinning tolerance.

    A `via` for a route that was a straight line would be claiming a route
    that was not there -- the same mistake, in the other direction, as
    emitting one for a route nobody recorded. Thinning cannot answer this on
    its own: it drops points that sit near the line between their *neighbours*,
    which leaves the two ends of a straight run standing however straight it
    was. The question here is whether the whole route sits near the line
    between its own first and last positions.
    """
    if len(targets) < 3:
        return True
    return all(
        _off_line(target, targets[0], targets[-1]) <= _WAYPOINT_TOLERANCE
        for target in targets[1:-1]
    )


def _simplify(
    points: Sequence[Target], tolerance: float = _WAYPOINT_TOLERANCE
) -> list[Target]:
    """Drop the points a route can do without, keeping the ones it turns on.

    Douglas-Peucker, which is defined by the guarantee this needs: **every
    dropped point is within `tolerance` of the line the kept points draw.** It
    gets that by taking the point of *greatest* deviation from the chord first,
    keeping it if it exceeds the tolerance, and recursing either side of it; a
    span whose worst point is within tolerance is dropped whole.

    The iterative "drop anything within tolerance of its neighbours" loop this
    replaces did not have that guarantee, and looked like it did. It judged
    each point against two neighbours, either of which might already have been
    dropped on an earlier pass, so the chord grew without bound: 500 samples of
    a 180px curve came out as six chords, up to 286px from the path they were
    supposed to be reproducing. There is a test for the property now, which is
    what would have caught it.

    An explicit stack rather than recursion, because a long recording nests
    deeply enough for the recursion limit to be a real ceiling.
    """
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        worst, worst_at = 0.0, first
        for index in range(first + 1, last):
            off = _off_line(points[index], points[first], points[last])
            if off > worst:
                worst, worst_at = off, index
        if worst <= tolerance:
            continue
        keep[worst_at] = True
        stack.append((first, worst_at))
        stack.append((worst_at, last))
    return [point for point, wanted in zip(points, keep, strict=True) if wanted]


def _thin(targets: Sequence[Target], cap: int) -> list[Target]:
    """The waypoints a route needs: its corners, within the tolerance.

    Positions are compared in absolute screen coordinates, which is sound
    because `_group_motion` only ever folds moves sharing one window origin --
    so every point here differs from its relative offset by the same constant
    and the distances are identical either way.

    `cap` (0 or less for no cap) is the backstop for a route that genuinely
    wanders, and it is met by **loosening the tolerance rather than by
    subsampling**. That distinction is the whole of this function's second
    half. Dropping every N-th point of an already-thinned route throws away
    exactly the points `_simplify` just decided were corners: measured on a
    24-sample zigzag, where every point is a corner, a cap of 8 took the
    deviation from 0px to **113px**. Loosening instead keeps the corners and
    smooths the detail between them, so the script gets shorter and the
    deviation grows in the same, predictable direction.
    """
    kept = _simplify(targets)
    if cap <= 0 or len(kept) <= cap:
        return kept
    tolerance = _WAYPOINT_TOLERANCE
    for _ in range(16):  # 8px doubling to ~500k px; a cap of 2 is reached early
        tolerance *= 2
        kept = _simplify(targets, tolerance)
        if len(kept) <= cap:
            return kept
    # Only reachable for a cap smaller than the two endpoints, which
    # `_simplify` always keeps -- so this cannot in practice drop one.
    return kept[:cap]


def _iter_element_refs(recording: Recording) -> Iterator[ElementRef]:
    """Every `ElementRef` an event carries, wherever it is nested.

    Walked generically over each event's own dataclass fields rather than
    naming `target`/`start`/`end`/`element` -- `Drag` alone has two `Target`s
    under different names, and a field-name list here would need updating
    every time an event type gained one, silently under-counting until it
    was.
    """
    for event in recording.events:
        for f in fields(event):
            value = getattr(event, f.name)
            if isinstance(value, Target):
                if value.element is not None:
                    yield value.element
            elif isinstance(value, ElementRef):
                yield value


def _compute_element_scopes(
    recording: Recording,
) -> tuple[dict[_ElementKey, tuple[str, str]], list[str]]:
    """Which addressable elements need `within=` to replay correctly.

    Two elements collide when they share a (role, name) pyguitest would
    search for, but not a full identity (role, name, ancestry path) -- the
    same widget mentioned twice, the ordinary case, keys identically and
    never reaches `groups` with more than one member. A real collision (two
    distinct elements, same role and name, different ancestry) does, and
    each such element gets whichever ancestor in its own path is not shared
    by any other member of the group -- see `_disambiguating_ancestor`.

    A collision `_disambiguating_ancestor` cannot resolve (nothing in the
    path is both named and unshared) is reported as a warning rather than
    left silent: the generated script would otherwise emit the same
    unscoped `gui.element(role=..., name=...)` for two different widgets,
    which matches whichever one pyguitest's search finds first -- exactly
    the "confidently wrong" failure this project exists to avoid.
    """
    groups: dict[tuple[str, str], dict[_ElementKey, ElementRef]] = {}
    for ref in _iter_element_refs(recording):
        if not ref.addressable:
            continue
        key: _ElementKey = (ref.role, ref.name, ref.path)
        groups.setdefault((ref.role, ref.name), {})[key] = ref
    scopes: dict[_ElementKey, tuple[str, str]] = {}
    warnings: list[str] = []
    for (role, name), members in groups.items():
        if len(members) < 2:
            continue
        unresolved = False
        for key, ref in members.items():
            others = [other for other_key, other in members.items() if other_key != key]
            ancestor = _disambiguating_ancestor(ref, others)
            if ancestor is not None:
                scopes[key] = ancestor
            else:
                unresolved = True
        if unresolved:
            warnings.append(
                f"{len(members)} elements named {name!r} with role {role!r} could "
                "not be told apart by ancestry; the generated script may click "
                "the wrong one"
            )
    return scopes, warnings


def _disambiguating_ancestor(
    ref: ElementRef, others: list[ElementRef]
) -> tuple[str, str] | None:
    """The nearest named ancestor of `ref` that none of `others` also has.

    Walked from the immediate parent outward (nearest ancestors disambiguate
    with the least indirection) rather than from the root, and skips any
    ancestor with no name -- a nameless container cannot be found again by
    `gui.element(name=...)` either, so binding one would just move the
    ambiguity rather than resolve it. A candidate is rejected if it appears
    *anywhere* in another element's path, not just at the matching depth --
    `_ancestor_var` binds it with an unscoped `gui.element(role=, name=)`,
    which has no notion of depth, so an ancestor shared at a different depth
    would still resolve ambiguously at replay time. Returns None when
    nothing in the whole path is both named and unshared, which does happen
    (two identically structured, identically named panes -- see
    docs/troubleshooting.md) and is a real limit of ancestry-based
    disambiguation, not a bug in finding it.
    """
    path = ref.path
    for depth in range(1, len(path) + 1):
        candidate = path[-depth]
        if not candidate[1]:
            continue
        if all(candidate not in other.path for other in others):
            return candidate
    return None


def _literal(value: str | int | float | bool | None) -> str:
    """Render a Python literal.

    Delegated to `ast.unparse` rather than hand-rolled: escaping, quoting and
    the awkward cases (embedded quotes, newlines, non-ASCII) are the standard
    library's problem, and getting them subtly wrong is how a generator emits
    a file that does not parse. `_format` normalizes the quote style
    afterwards where ruff is available.
    """
    return ast.unparse(ast.Constant(value))


def _identifier(base: str, taken: set[str]) -> str:
    """Make a readable, unique Python identifier from a window's identity.

    Reverse-DNS app ids are common and their leading segments carry no
    information, so `org.gnome.TextEditor` binds to `texteditor` rather than
    `org_gnome_texteditor`, which would dominate every line it appears in.

    Two dots are required before that applies, not one. WM_CLASS is often just
    a program name, and a one-dot rule read `check_app.py` as reverse-DNS and
    bound the window to `py` -- seen in a live run the moment X11 started
    reporting an app id at all.
    """
    if base.count(".") >= 2 and " " not in base:
        base = base.rsplit(".", 1)[-1]
    cleaned = "".join(c if c.isalnum() else "_" for c in base.lower()).strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"win_{cleaned}" if cleaned else "win"
    if keyword.iskeyword(cleaned) or keyword.issoftkeyword(cleaned):
        cleaned = f"{cleaned}_window"
    # Cut at a word boundary rather than mid-word: a long title truncated to
    # the character produced `hello_there_draft_text_edito`, which reads as a
    # typo in every line it appears in.
    candidate = cleaned[:28]
    if len(cleaned) > 28 and "_" in candidate[1:]:
        candidate = candidate.rsplit("_", 1)[0]
    suffix = 2
    while candidate in taken:
        candidate = f"{cleaned[:26]}_{suffix}"
        suffix += 1
    return candidate


class PythonGenerator:
    """Turn a `Recording` into pyguitest source."""

    def __init__(self, options: GeneratorOptions | None = None) -> None:
        """Build a generator with the given options, or the defaults."""
        self.options = options or GeneratorOptions()
        self._api = _session_methods()

    def render(self, recording: Recording) -> str:
        """Return the complete generated module for `recording`."""
        state = _State()
        state.natural_motion = _NATURAL_MOTION in _session_methods()
        state.session_type = recording.environment.session_type
        if self.options.locators == "element":
            state.element_scopes, scope_warnings = _compute_element_scopes(recording)
            state.warnings.extend(scope_warnings)
        if _shaped(self.options.motion) and not state.natural_motion:
            state.warnings.append(
                f"the installed pyguitest has no {_NATURAL_MOTION}(), so every "
                f"move under motion = {self.options.motion!r} was rendered as a "
                "teleport"
            )
        for item in _group_motion(recording.events, self.options):
            if isinstance(item, _MotionRun):
                self._emit_motion_run(item, state)
            else:
                self._emit(item, state)
        source = self._assemble(recording, state)
        return _format(source) if self.options.format_output else source

    def _emit_motion_run(self, run: _MotionRun, state: _State) -> None:
        """Render one movement: where it ended, and its route where it needs one.

        Under `natural` a movement is normally the endpoints alone -- pyguitest
        shapes what happens between them, which is a better account of travel
        than a straight line and the same call length as a teleport.

        Not for a movement that began or ended where the pointer had settled,
        though. The route is then part of what the recording *did* rather than
        travel between two places, and a shaped one is a different path. Seen
        live, on MATE: the pointer rested on the Applications menu's first row
        (which is what opens that row's submenu), then travelled right and *down
        the submenu column* to the item it clicked. Collapsed into one leg, the
        shaped path bows across the menu's other rows -- `arc` is a fraction of
        the leg, pyguitest's own default -- each of which opens its own submenu
        on the way past, so the click landed on an item the recording never
        chose and the application the demo was opening never opened. Keeping the
        route costs a `via=[...]` and is the only thing that replays that
        interaction; `recorded` keeps every route that is not straight at all.

        Under `recorded` the route comes back as waypoints, thinned first (see
        `_thin`), so a few hundred motion events become the handful of points
        the movement actually turned on.
        """
        state.capabilities.add("POINTER_MOVE")
        via: list[Target] = []
        if self.options.motion == "recorded" or run.touches_a_rest:
            route = [move.target for move in run.moves[:-1]]
            # Straightness is asked of the whole route, destination included:
            # the recorded samples alone can all be collinear while the move
            # still ended somewhere off that line.
            whole = [*route, run.last.target]
            if not _straight(whole):
                via = _thin(route, self.options.max_waypoints)
        self._move(run.last.target, state, via=via)

    # -- event dispatch ------------------------------------------------------

    def _emit(self, event: Event, state: _State) -> None:
        """Append the lines for one event."""
        handler = getattr(self, f"_emit_{type(event).__name__.lower()}", None)
        if handler is None:
            state.warnings.append(f"no renderer for {type(event).__name__}")
            return
        if self.options.motion == "verbatim":
            # Before the note, not after it: the wait is what the recording did
            # before this event, and the note is about the event itself.
            self._wait_for_gap(event, state)
        else:
            # `verbatim` already replays every gap, so this would double them.
            self._settle_after_key_action(event, state)
        if self.options.comments and event.note:
            state.lines.append(f"# {event.note}")
        if isinstance(event, _SEPARATES_CLICKS):
            state.bare_click_pending = False
        handler(event, state)
        if isinstance(event, (KeyStroke, HotKey)):
            # When, rather than whether: the wait one of these is owed is
            # measured from it, and everything in between carries delays
            # measured from each other. See `_State.key_action_at`.
            state.key_action_at = event.timestamp

    def _settle_after_key_action(self, event: Event, state: _State) -> None:
        """Give back the gap a keystroke was followed by, where one was dropped.

        `normalize.py` only records an idle of `pause_threshold` or more as a
        `Pause`; a shorter one leaves nothing behind but the timestamps, and
        the script then issues both back to back. After a chord
        that opened a window, that is the difference between a replay that
        works and one that types into nothing -- see
        `_KEY_ACTION_SETTLE_FLOOR` for the recording this was measured on.

        Only after a keystroke, and only for a gap the recording actually
        contains. A `Pause` of its own means the gap was already long enough
        to be rendered, so adding to it would sleep twice.

        Only *before an event that delivers input*, too. A `MouseMove` is a
        positioning step and already carries its own settle where nothing
        resolved the window under it, so allowing this one as well produced
        two waits on consecutive lines -- the pause belongs in front of
        whatever actually addresses the new window, which is the click or the
        keystroke after the move, not the move itself.

        The interval is that event's timestamp minus the keystroke's, not the
        `delay` the event carries. A move in between neither takes the wait nor
        cancels it -- it is a step towards the line that does -- and the delay
        it carries is the interval to *it*, which is how a chord 0.9s before a
        click, with a move in between, came out as the 0.2s after the move and
        fell under the floor. An input event ends the question whether or not a
        wait was emitted for it: that click is the line that addressed the new
        window, and the one after it does not need the same pause again.

        A recorded `Pause` or `WaitForIdle` in between settles it the other
        way: the seconds are already in the script, so emitting this one as
        well would sleep through the same interval twice.
        """
        at = state.key_action_at
        if at is None or isinstance(event, MouseMove):
            return
        if isinstance(event, _KEY_ACTION_CONSUMING_WAITS):
            state.key_action_at = None
            return
        if not isinstance(event, _AWAITS_A_KEY_ACTION):
            return
        state.key_action_at = None
        gap = event.timestamp - at
        if gap < _KEY_ACTION_SETTLE_FLOOR:
            return
        state.capabilities.add("TIMING")
        self._comment(
            "the keystroke before this had time to take effect -- long enough"
            " to matter, too short for the recorder to have kept it as a pause",
            state,
        )
        state.lines.append(f"gui.wait({min(gap, _KEY_ACTION_SETTLE_CAP):.2f})")

    def _wait_for_gap(self, event: Event, state: _State) -> None:
        """Sleep for the time the recording spent before this event.

        The half of a recording that `natural` and `recorded` both drop: each
        keeps a path and leaves the clock behind, so the pointer reaches the
        right place at the wrong speed. What an interface saw between two
        positions *is* the interval between them, and for a menu row held open
        by a hover -- or popped down by leaving one for long enough -- that
        interval is the input, not the route.

        Gaps are measured between the events this script replays rather than
        between the recording's events as written, so a hover's own wait covers
        the dwell and the position that leaves the rest is not asked for it
        again. Three decimals rather than the two every other wait here takes:
        these are the millisecond intervals themselves, and 6ms rounded to
        0.01 would be a 50% error on every one of them.
        """
        if state.replayed_until is None:
            state.replayed_until = event.timestamp
            return
        gap = event.timestamp - state.replayed_until
        state.replayed_until = max(state.replayed_until, event.timestamp)
        if gap < _VERBATIM_WAIT_FLOOR:
            return
        state.capabilities.add("TIMING")
        state.lines.append(f"gui.wait({gap:.3f})")

    def _emit_comment(self, event: Comment, state: _State) -> None:
        """Render a standalone comment."""
        if self.options.comments:
            state.lines.append(f"# {event.text}")

    def _emit_pause(self, event: Pause, state: _State) -> None:
        """Render an explicit sleep, which is always a last resort."""
        state.capabilities.add("TIMING")
        if self.options.comments:
            state.lines.append("# no state change to synchronize on here")
        state.lines.append(f"gui.wait({event.seconds:.2f})")
        if self.options.motion == "verbatim":
            # A Pause is timestamped where the idle it stands for *began* (see
            # `_gap`), so `_wait_for_gap` has only carried the clock as far as
            # the idle's start -- while the event that ended that idle sits at
            # the other end of the same seconds. Advancing the clock by the
            # sleep just emitted is what stops the interruption being replayed
            # twice, once as a Pause and again as the gap after it.
            replayed = event.timestamp + event.seconds
            state.replayed_until = max(state.replayed_until or 0.0, replayed)

    def _emit_sync(self, event: Sync, state: _State) -> None:
        """Round-trip the input stream."""
        state.capabilities.add("INPUT_SYNC")
        state.lines.append("gui.sync()")

    def _emit_mousemove(self, event: MouseMove, state: _State) -> None:
        """Render meaningful pointer motion, and the hover if it was one."""
        state.capabilities.add("POINTER_MOVE")
        self._move(event.target, state)
        if not event.dwell:
            return
        # The wait is the point of a hover, not decoration: the interface
        # reacts to the pointer having *stayed*, and a replay that arrives
        # and leaves in the same instant gets no submenu, no tooltip, and
        # then clicks a coordinate that only exists because of them.
        state.capabilities.add("TIMING")
        self._comment(
            "the pointer rested here long enough for the interface to react"
            " -- a hover, not a move on the way somewhere",
            state,
        )
        verbatim = self.options.motion == "verbatim"
        waited = min(event.dwell, _HOVER_WAIT_CAP)
        if verbatim:
            waited = self._unreplayed_dwell(event, waited, state)
        if waited >= _VERBATIM_HOVER_FLOOR or not verbatim:
            state.lines.append(f"gui.wait({waited:.2f})")
        state.bare_click_pending = False

    def _unreplayed_dwell(
        self, event: MouseMove, waited: float, state: _State
    ) -> float:
        """How much of a hover's wait the `verbatim` clock has yet to spend.

        A rest is stamped where it *began* and emitted where it *ended* -- it is
        only known to have been one once the pointer leaves -- so by the time it
        is rendered the script may already have replayed part of it: the
        positions the pointer took inside it, each with the wait that preceded
        it (a hand never rests perfectly still, and `record_motion` records every
        tremor), or a Pause standing for the same idle. Waiting the whole dwell
        on top of that replays a rest at up to twice its length, on the one value
        whose point is replaying the recorded clock. So the wait is whatever is
        left of it, and the clock is brought up to the end of the rest and no
        further: the position that leaves it is then measured from there, and is
        not asked for the same interval again.

        `waited` is already capped (see `_HOVER_WAIT_CAP`); a rest the script has
        replayed past that -- through its own positions, or a Pause, neither of
        which is capped -- is left as it was replayed.
        """
        ends = event.timestamp + waited
        replayed = (
            event.timestamp if state.replayed_until is None else state.replayed_until
        )
        state.replayed_until = max(replayed, ends)
        return ends - replayed

    def _emit_click(self, event: Click, state: _State) -> None:
        """Render a click, preferring the element under the pointer.

        Only a left click can take the element path. `Element.click()` accepts
        no button argument, so rendering a recorded right click that way would
        turn a context menu into an ordinary activation -- a script that runs
        cleanly and does the wrong thing, which is the worst outcome here.
        """
        if event.button == 1 and event.count == 2 and self._expand_click(event, state):
            state.bare_click_pending = False
            return
        if event.button == 1 and event.count == 2 and self._double_click(event, state):
            state.bare_click_pending = False
            return
        if event.button == 1:
            element = event.target.element
            if (
                element is not None
                and element.role in _SELECT_ROLES
                and self._select_click(event, state)
            ):
                return
            call = self._element_call(element, state, "click()")
            if call is not None:
                state.lines.extend([call] * event.count)
                state.pointer = None
                state.bare_click_pending = False
                self._note_element_repeat(event, state)
                return
            if self._select_click(event, state):
                return
        if state.bare_click_pending:
            state.capabilities.add("TIMING")
            self._comment(
                "spaced out from the click before it: whatever that one"
                " opened may not have drawn yet, and two clicks with no gap"
                " can also read as one double-click to the application",
                state,
            )
            state.lines.append(f"gui.wait({_COORDINATE_CLICK_SETTLE:g})")
        state.capabilities.update({"POINTER_MOVE", "POINTER_BUTTON"})
        self._move(event.target, state)
        self._note_button_fallback(event, state)
        self._note_unclickable_fallback(event, state)
        button = "" if event.button == 1 else str(event.button)
        if event.count == 2:
            state.lines.append(f"gui.double_click({button})")
        else:
            state.lines.extend([f"gui.click({button})"] * event.count)
            self._note_repeat(event, state)
        state.bare_click_pending = True

    def _select_click(self, event: Click, state: _State) -> bool:
        """Render a single click as `Element.select()`, where that is the act.

        The second named path, for the controls that answer a click with a
        *selection* rather than an activation -- a radio button, a page tab, a
        list row, a tree item. Each offers `select` and not `click`, so
        without this a click on one fell all the way to a coordinate, which is
        the one locator guaranteed to break when the window moves.

        A single click only: `select()` is idempotent, so a repeated click has
        no rendering as one and keeps its coordinates.
        """
        element = event.target.element
        if event.count != 1 or element is None or not element.selectable:
            return False
        call = self._element_locator_call(element, state, "select()")
        if call is None:
            return False
        state.lines.append(call)
        state.pointer = None
        state.bare_click_pending = False
        return True

    def _expand_click(self, event: Click, state: _State) -> bool:
        """Render a double click on a disclosure control as expand()/collapse().

        The third named path, ahead of `_double_click`: a tree row, a
        notebook, anything AT-SPI or UIA marks EXPANDABLE. Measured live on
        a GTK3 GtkTreeView, `.double_click()` -- the fallback this pre-empts
        -- *activates* a row rather than opening it; double-clicking a
        Windows tree row happens to expand it, which is exactly the kind of
        agreement-by-accident that stops holding the moment a script written
        against one platform runs on the other. `.expand()`/`.collapse()`
        names the actual intent and means the same thing everywhere pyguitest
        implements it -- both platforms, as of the floor this package names.

        Rendered as the toggle a double click is, read at replay -- collapse
        if open, expand if not -- rather than as a direction fixed from the
        `expanded` the recording captured. That read is not reliably from
        before the click: an element is described when its press is
        consumed, and on Windows a native tree view has toggled the row by
        then whenever the consumer ran behind the second press -- so the
        same double click on a collapsed row recorded `expanded` True and
        rendered `collapse()`, a no-op on replay that left the next,
        nested row missing. Whether the read lands before or after depends
        only on timing, so no reading of it is safe; replaying the toggle
        itself reproduces the recording from the state it started in.

        Two clicks, one toggle, the same way `_double_click` collapses its
        pair -- toggling twice would put the row back.
        """
        element = event.target.element
        if element is None or not element.expandable:
            return False
        locator = self._element_expr(element, state)
        if locator is None:
            return False
        state.capabilities.add("ELEMENT_ACTION")
        state.lines.extend(
            [
                f"if {locator}.expanded:",
                f"    {locator}.collapse()",
                "else:",
                f"    {locator}.expand()",
            ]
        )
        state.pointer = None
        return True

    def _double_click(self, event: Click, state: _State) -> bool:
        """Render a double click on a named element that stays a double click.

        Two `Element.click()` calls are not a double click: each is a separate
        round trip through the accessibility bus, which is slower than any
        double-click interval -- and the failure is silent: double-clicking a
        folder icon simply does not open the folder. Seen in a real recording
        of a file manager.

        Asked of the element itself -- `Element.double_click`, which the floor
        this package names guarantees, so there is no session spelling left to
        fall back to. The element is located first and its *live* extents drive
        the move, so the locator stays an element -- the point is read at
        replay, not baked in -- while the gesture stays one gesture.
        """
        element = event.target.element
        locator = self._element_expr(element, state)
        if locator is None or element is None:
            return False
        if element.extents is None:
            # Nothing said this element has a position worth trusting, and a
            # move to a rectangle that does not exist is worse than two clicks.
            return False
        state.capabilities.update(
            {"ELEMENT_TREE", "ELEMENT_GEOMETRY", "POINTER_MOVE", "POINTER_BUTTON"}
        )
        state.lines.append(f"{locator}.double_click()")
        state.pointer = None
        return True

    def _note_button_fallback(self, event: Click, state: _State) -> None:
        """Explain a coordinate click on an element that could have been named."""
        element = event.target.element
        if (
            not self.options.comments
            or event.button == 1
            or element is None
            or not element.addressable
            or self.options.locators != "element"
        ):
            return
        button = _BUTTONS.get(event.button, f"button {event.button}")
        state.lines.append(
            f"# {element.name!r} was named, but Element.click() takes no button,"
        )
        state.lines.append(f"# so this {button} click has to stay a coordinate")

    def _note_unclickable_fallback(self, event: Click, state: _State) -> None:
        """Explain a coordinate click on an element AT-SPI would not let click."""
        element = event.target.element
        if (
            not self.options.comments
            or event.button != 1
            or element is None
            or not element.addressable
            or element.clickable
            or self.options.locators != "element"
        ):
            return
        api = element_api(state.session_type)
        state.lines.append(
            f"# {element.name!r} was named, but offered {api} no click or"
        )
        state.lines.append("# press action, so this has to stay a coordinate")

    def _note_repeat(self, event: Click, state: _State) -> None:
        """Explain a repeated click beyond a double.

        pyguitest has no primitive past double_click.
        """
        if event.count > 1 and self.options.comments:
            state.lines.insert(
                len(state.lines) - event.count,
                f"# recorded as a {event.count}x click; pyguitest has no"
                " primitive past double_click,",
            )
            state.lines.insert(
                len(state.lines) - event.count,
                "# so this is consecutive clicks and depends on the"
                " toolkit's click interval",
            )

    def _note_element_repeat(self, event: Click, state: _State) -> None:
        """Explain a repeated click on a named element.

        A double click on a named element is normally one gesture (see
        `_double_click`); this is the path left when that declined, which is
        the element carrying no rectangle to move to. A repeat past two has no
        primitive on either path and lands here too.
        """
        if event.count > 1 and self.options.comments:
            if event.count == 2:
                reason = "the element has no rectangle to move to, so"
            else:
                reason = "pyguitest has no primitive past double_click, so"
            word = "double" if event.count == 2 else f"{event.count}x"
            state.lines.insert(
                len(state.lines) - event.count,
                f"# recorded as a {word} click; {reason} this is",
            )
            state.lines.insert(
                len(state.lines) - event.count,
                "# consecutive clicks and depends on the toolkit's"
                " double-click interval",
            )

    # -- checks --------------------------------------------------------------

    def _emit_assertion(self, event: Assertion, state: _State) -> None:
        """Render a check the person recording asked for.

        Dispatched by name the same way events are, so an unknown check from a
        recording made by a later version degrades to a comment rather than
        being dropped without trace.
        """
        renderer = getattr(self, f"_check_{event.check}", None)
        if renderer is None:
            state.warnings.append(f"unknown check {event.check!r} was not generated")
            return
        renderer(event, state)

    def _check_text(self, event: Assertion, state: _State) -> None:
        """Render a check that an element still reads what it read when recorded."""
        element = event.target.element
        if element is None:
            state.warnings.append(describe_assertion(event))
            return
        if event.sensitive and self.options.redact_sensitive:
            expected = self._secret_name(state)
        else:
            expected = _literal(str(event.expected))
        self._check_comment(
            describe_assertion(event, redact_sensitive=self.options.redact_sensitive),
            state,
        )
        self._expect("expect_text", element, state, f"equals={expected}")

    def _check_checked(self, event: Assertion, state: _State) -> None:
        """Render a check on a checkbox, radio button or toggle."""
        element = event.target.element
        if element is None:
            state.warnings.append(describe_assertion(event))
            return
        self._check_comment(describe_assertion(event), state)
        self._expect(
            "expect_checked", element, state, f"checked={bool(event.expected)}"
        )

    def _check_showing(self, event: Assertion, state: _State) -> None:
        """Render a check that an element is on screen.

        The floor: all that can be asked of a button, whose text is its own
        name and whose state says nothing about whether the application did
        what it was told.
        """
        element = event.target.element
        if element is None:
            state.warnings.append(describe_assertion(event))
            return
        self._check_comment(describe_assertion(event), state)
        self._expect("expect_showing", element, state)

    def _check_window(self, event: Assertion, state: _State) -> None:
        """Render a check that a window is open, where no element could be named."""
        window = event.target.window
        if window is None or not window.title:
            state.warnings.append(describe_assertion(event))
            return
        self._check_comment(describe_assertion(event), state)
        state.capabilities.add("WINDOW_LIST")
        state.lines.append(f"gui.expect_window({_title_pattern(window.title)})")

    def _check_nothing(self, event: Assertion, state: _State) -> None:
        """Record that a check was asked for where nothing could be identified.

        A warning rather than a comment, so it reaches the header even with
        comments switched off. A check the recorder quietly dropped is worse
        than one it admits it could not make: the script would otherwise look
        like it verifies something it never does.
        """
        state.warnings.append(describe_assertion(event))

    def _expect(
        self, helper: str, element: ElementRef, state: _State, extra: str = ""
    ) -> None:
        """Emit one `expect_` call against a named element.

        Scoped the same way a clicked element is (see `_element_expr`): a
        check on one of two same-named elements needs `within=` exactly as
        much as a click on it would, and is subject to the same collision
        warning when ancestry cannot tell them apart.
        """
        state.capabilities.add("ELEMENT_TREE")
        args = self._role_arg(element.role, state)
        key: _ElementKey = (element.role, element.name, element.path)
        scope = state.element_scopes.get(key)
        tail = f", {extra}" if extra else ""
        if scope is not None:
            within = self._ancestor_var(scope, state)
            tail += f", within={within}"
        state.lines.append(f"gui.{helper}({args}, name={_literal(element.name)}{tail})")

    def _check_comment(self, what: str, state: _State) -> None:
        """Label a check in the words of the person who will read it."""
        self._comment(f"Check: {what}", state)

    def _emit_drag(self, event: Drag, state: _State) -> None:
        """Render a drag as pyguitest's own drag primitive.

        A drag that *moves a window* has to be written in screen coordinates.
        Every other point in a generated script is relative to its window,
        because that is what survives the window being somewhere else -- but
        the window here is the thing being dragged, so it travels with the
        pointer and the offset within it barely changes. Both endpoints then
        render against different origins and collapse: a real recording of
        someone dragging a calculator around produced
        `gui.drag((x + 485, y + 49), (x + 485, y + 49))`, which moves nothing
        while looking like it does.
        """
        state.capabilities.update({"POINTER_MOVE", "POINTER_BUTTON", "TIMING"})
        button = "" if event.button == 1 else f", button={event.button}"
        if _dragged_its_own_window(event):
            self._comment(
                "this drag moved the window it began in, so its endpoints are "
                "screen coordinates rather than offsets into a window that "
                "was moving at the time",
                state,
            )
            start = f"{event.start.x}, {event.start.y}"
            end = f"{event.end.x}, {event.end.y}"
            state.pointer = None
            route = self._drag_route(event, state, screen_coordinates=True)
            state.lines.append(f"gui.drag(({start}), ({end}){button}{route})")
            return
        start = self._point(event.start, state)
        end = self._point(event.end, state)
        route = self._drag_route(event, state, screen_coordinates=False)
        state.lines.append(f"gui.drag(({start}), ({end}){button}{route})")
        state.pointer = end

    def _drag_route(
        self, event: Drag, state: _State, *, screen_coordinates: bool
    ) -> str:
        """The `via=` argument for a drag's recorded route, or nothing.

        The route *is* the gesture, and pyguitest's `drag` glides between one
        end and the other: without this a recorded curve replays as a straight
        line, which is a different thing to have dragged. Points come thinned
        to the corners the hand turned on, as a movement's do.

        There are two ways to write one, and which is available decides itself.
        When every point of the route shares a frame with the drag's end, the
        whole list is measured against that window's origin -- one
        `window_x`/`window_y` read serves it -- and reads like the rest of the
        script. When it does not, because the drag crossed into another window
        or a window moved underneath it, there is no single origin to measure
        the list against, so every point is written as a screen coordinate
        instead, exactly as the drag's own two ends are once its window has
        moved. Screen coordinates are less portable and always valid; a route
        without them is a gesture that did not happen.
        """
        one_frame = all(_same_frame(point, event.end) for point in event.route)
        points = list(event.route)
        if not points or _straight([*points, event.end]):
            return ""
        kept = _thin(points, self.options.max_waypoints)
        rendered = []
        for point in kept:
            if screen_coordinates or not one_frame:
                rendered.append(f"({point.x}, {point.y})")
            else:
                rendered.append(f"({self._point(point, state)})")
        return f", via=[{', '.join(rendered)}]"

    def _emit_scroll(self, event: Scroll, state: _State) -> None:
        """Render wheel movement in detents, at the point it was recorded at.

        The wheel acts on whatever is under the pointer, so a scroll emitted
        without first putting the pointer back scrolls whichever widget the
        previous action left it over.
        """
        if not event.dx and not event.dy:
            return
        state.capabilities.update({"POINTER_MOVE", "POINTER_SCROLL"})
        self._move(event.target, state)
        parts = []
        if event.dx:
            parts.append(f"dx={event.dx}")
        if event.dy:
            parts.append(f"dy={event.dy}")
        state.lines.append(f"gui.scroll({', '.join(parts)})")

    def _emit_textinput(self, event: TextInput, state: _State) -> None:
        """Render typed text, into a named field where one was identified."""
        if event.sensitive:
            text = self._secret(event, state)
            # Said at the point of use, not only in the binding block at the
            # top: a reader scanning the body should not have to work out why
            # one `type_text` takes a name where every other takes a string.
            self._comment(
                "this went into a password field, so the text itself was "
                "never written here",
                state,
            )
        else:
            text = _literal(event.text)
        element = event.target.element if event.target else None
        if (
            self.options.locators == "element"
            and element is not None
            and element.role in _TEXT_ROLES
            and element.name
        ):
            state.capabilities.update({"ELEMENT_TREE", "ELEMENT_ACTION"})
            state.lines.append(
                f"gui.text_field({_literal(element.name)}).set_text({text})"
            )
            return
        self._focus_before_typing(event.target, state)
        state.capabilities.add("TEXT_ENTRY")
        self._note_unidentified_text(event, state)
        state.lines.append(f"gui.type_text({text})")

    def _note_unidentified_text(self, event: TextInput, state: _State) -> None:
        """Say when typed text went somewhere the recording could not name.

        Password redaction works by *recognising* the field: text is withheld
        only where the element under it published the `password text` role. So
        a password typed into a field the recording never identified is
        written into the script in clear, and nothing about the script says
        so. That happened for real, into a network-share authentication
        dialog reached by Tab from the username field -- the recorder still
        believed it was in the username field, which is not a password field,
        so the password went in verbatim.

        This cannot be fixed by guessing: withholding every unidentified run
        would redact most typing on a toolkit whose hit-testing does not work,
        and guessing from the text itself is worse. What it can do is stop
        being silent, so that `--sensitive` gets used where it matters.
        """
        if event.sensitive or not self.options.redact_sensitive:
            return
        warning = (
            "typed text went to a field this recording could not identify, so "
            "it is in this script verbatim. Password fields are only withheld "
            "when they can be recognized -- if any of this was a secret, "
            "re-record with --sensitive and treat this file as credential-"
            "bearing until you have checked it"
        )
        if warning not in state.warnings:
            state.warnings.append(warning)

    def _secret(self, event: TextInput, state: _State) -> str:
        """Render sensitive input as an environment lookup, never as a literal."""
        if not self.options.redact_sensitive:
            return _literal(event.text)
        return self._secret_name(state)

    def _secret_name(self, state: _State) -> str:
        """Bind the next environment variable standing in for a redacted value."""
        name = f"SECRET_{len(state.secrets) + 1}"
        state.secrets.append(name)
        return name

    def _focus_before_typing(self, target: Target | None, state: _State) -> None:
        """Confirm the window about to be typed into actually holds focus.

        Keyboard input is the one thing that goes somewhere invisible when it
        goes wrong: a click lands at a coordinate whether or not the window
        was ready, but a keystroke goes to whatever *does* hold focus, and a
        freshly-opened window can exist -- `expect_window` has already
        returned it -- a moment before the window manager gives it focus.
        Reproduced live twice: typed text landing in the terminal the replay
        script was itself running in.

        So focus is confirmed here, immediately before typing, and nowhere
        else. It deliberately does not happen when a window is merely bound:
        raising a window as a side effect of looking it up is what dismissed
        an open menu by lifting the desktop out from under it, also live.
        Emitted once per switch -- typing two runs into one window without
        leaving it confirms focus once.
        """
        window = target.window if target else None
        if window is None or not window.addressable:
            return
        name = self._window_var(window, state)
        if name == state.focused_window:
            return
        state.capabilities.add("WINDOW_ACTIVATE")
        state.lines.append(f"gui.focus_window({name})")
        state.focused_window = name

    def _emit_keystroke(self, event: KeyStroke, state: _State) -> None:
        """Render a single key tap."""
        self._focus_before_typing(event.target, state)
        state.capabilities.add("KEY_EVENT")
        state.lines.append(f"gui.tap_key({_literal(event.key)})")

    def _emit_hotkey(self, event: HotKey, state: _State) -> None:
        """Render a modifier combination in send_keys' grammar.

        `KEY_EVENT`, not `TEXT_ENTRY`: send_keys declares no capability of its
        own and reaches the keyboard through `press_key`, so a script that
        required TEXT_ENTRY could pass `require()` on a backend that cannot
        press a modifier at all.
        """
        self._focus_before_typing(event.target, state)
        state.capabilities.add("KEY_EVENT")
        state.lines.append(f"gui.send_keys({_literal(_hotkey_string(event.keys))})")

    def _emit_windowactivate(self, event: WindowActivate, state: _State) -> None:
        """Render raising a window, binding it to a variable first."""
        if not event.window.addressable:
            self._comment(
                f"a window with {_unaddressable_reason(event.window)} was activated",
                state,
            )
            return
        name = self._window_var(event.window, state)
        state.capabilities.add("WINDOW_ACTIVATE")
        # focus_window, not activate_window: it waits for the window to stop
        # moving, asks more than once, and confirms the request was honored.
        # This is the one place a generated script should do that -- where
        # the recording says the person deliberately switched windows --
        # rather than on every window it happens to bind, which is what
        # dismissed an open menu by raising the desktop underneath it.
        state.lines.append(f"gui.focus_window({name})")
        state.focused_window = name
        # Raising a window can move it, so both the cached origin and the
        # pointer position stop being trustworthy here.
        state.geometry_for = None
        state.geometry_origin = None
        state.pointer = None

    def _emit_waitforwindow(self, event: WaitForWindow, state: _State) -> None:
        """Render waiting for a window to appear."""
        if not event.window.addressable:
            self._comment(
                f"waited for a window with {_unaddressable_reason(event.window)}",
                state,
            )
            return
        self._window_var(event.window, state, timeout=event.timeout)

    def _emit_waitforelement(self, event: WaitForElement, state: _State) -> None:
        """Render waiting for an element to appear.

        Scoped like a click on the same element would be (see
        `_element_expr`): waiting for one of two same-named elements needs
        `within=` exactly as much as clicking it does, once it appears.
        """
        state.capabilities.add("ELEMENT_TREE")
        args = self._role_arg(event.element.role, state)
        if event.element.name:
            args += f", name={_literal(event.element.name)}"
            key: _ElementKey = (
                event.element.role,
                event.element.name,
                event.element.path,
            )
            scope = state.element_scopes.get(key)
            if scope is not None:
                within = self._ancestor_var(scope, state)
                args += f", within={within}"
        state.lines.append(f"gui.wait_for_element({args}, timeout={event.timeout:g})")
        # Waiting for an element means the window under the pointer just
        # changed, so where the pointer was says nothing about where it is.
        state.pointer = None

    def _emit_waitforidle(self, event: WaitForIdle, state: _State) -> None:
        """Render waiting for the target process to stop burning CPU.

        The pid observed while recording means nothing at replay time, so the
        one this emits comes from the window: `Window.pid`, populated when the
        backend offers `WINDOW_PID`, which the preamble then requires. Without
        a window to hang it on there is no pid to wait on and this degrades to
        a sleep.
        """
        if event.window is None or not event.window.addressable:
            state.capabilities.add("TIMING")
            self._comment("no window to take a pid from, so this is a sleep", state)
            state.lines.append(f"gui.wait({self.options.default_timeout:g})")
            return
        name = self._window_var(event.window, state)
        state.capabilities.add("WINDOW_PID")
        state.lines.append(f"gui.wait_for_idle({name}.pid, timeout={event.timeout:g})")

    # -- locators ------------------------------------------------------------

    def _comment(self, text: str, state: _State) -> None:
        """Append an explanatory comment, unless comments are switched off.

        Wrapped, because `ruff format` will not do it: a comment is opaque to
        a formatter, so a long one is the only thing in a generated file that
        can run past the line limit -- and the explanatory ones here, which
        quote window titles, are exactly the long ones.
        """
        if not self.options.comments:
            return
        # 8 for the body indent, 2 for "# ", against the project's own limit.
        state.lines.extend(f"# {line}" for line in textwrap.wrap(text, width=78))

    def _move(self, target: Target, state: _State, via: Sequence[Target] = ()) -> None:
        """Put the pointer at `target`, unless it is already known to be there.

        Tracking the last emitted position is what keeps a click and the
        scroll that follows it at the same place from emitting the same move
        twice, and is why a scroll can afford to always ask for one.

        `via` is the route a movement took, already thinned. It is rendered as
        literal tuples rather than built in a loop because a generated script
        is meant to be one a person would have been willing to write; see
        `motion` on GeneratorOptions.
        """
        point = self._point(target, state)
        if point == state.pointer:
            return
        if target.window is None:
            self._comment(
                "nothing resolved a window for this point when it was "
                "recorded, so whatever is about to receive it gets a moment "
                "to finish appearing first",
                state,
            )
            state.capabilities.add("TIMING")
            state.lines.append(f"gui.wait({_UNATTRIBUTED_SETTLE:g})")
        screen = f", screen={target.screen}" if target.screen else ""
        if not _shaped(self.options.motion) or not state.natural_motion:
            # Either not asked for, or a pyguitest without the shaped call --
            # render() has already warned about the second.
            state.lines.append(f"gui.move_mouse({point}{screen})")
        else:
            rendered = [f"({self._point(waypoint, state)})" for waypoint in via]
            via_arg = f", via=[{', '.join(rendered)}]" if rendered else ""
            state.lines.append(f"gui.move_mouse_naturally({point}{screen}{via_arg})")
        state.pointer = point

    def _element_call(
        self, element: ElementRef | None, state: _State, action: str
    ) -> str | None:
        """Return an element-based call, or None if no element can be named.

        Checked before `_element_expr` is asked for a locator, not after: that
        call can already emit a disambiguating `within=` line as a side
        effect, and an element this is about to refuse for lacking a click
        action should not leave that line behind for a call it never renders.
        """
        if element is not None and not element.clickable:
            return None
        return self._element_locator_call(element, state, action)

    def _element_locator_call(
        self, element: ElementRef | None, state: _State, action: str
    ) -> str | None:
        """A locator plus an action on it, with no actionability gate.

        Separate from `_element_call` because `select()` is gated on
        `selectable` rather than `clickable` -- a tree item offers the first
        and not the second -- and both have to reach the locator without a
        refused call leaving a `within=` line behind.
        """
        locator = self._element_expr(element, state)
        if locator is None:
            return None
        state.capabilities.add("ELEMENT_ACTION")
        return f"{locator}.{action}"

    def _element_expr(self, element: ElementRef | None, state: _State) -> str | None:
        """Return the expression that finds this element, without an action.

        Split out from `_element_call` because a double click needs the
        element itself rather than a method on it -- see `_double_click`.
        """
        if self.options.locators != "element":
            return None
        if element is None or not element.addressable:
            return None
        state.capabilities.add("ELEMENT_TREE")
        key: _ElementKey = (element.role, element.name, element.path)
        scope = state.element_scopes.get(key)
        if scope is None:
            sugar = _SUGAR.get(element.role)
            if sugar is not None:
                return f"gui.{sugar}({_literal(element.name)})"
            args = self._role_arg(element.role, state)
            return f"gui.element({args}, name={_literal(element.name)})"
        # No _SUGAR here even where one exists for this role: the sugar
        # methods take no `within=`, exactly the scoping this element needs
        # to avoid matching its same-named sibling elsewhere in the tree.
        within = self._ancestor_var(scope, state)
        args = self._role_arg(element.role, state)
        return f"gui.element({args}, name={_literal(element.name)}, within={within})"

    def _ancestor_var(self, ancestor: tuple[str, str], state: _State) -> str:
        """Bind a disambiguating ancestor to a variable, finding it once.

        Cached by (role, name) in `state.scope_vars` so two elements that
        need the same scoping container -- two fields in one dialog, say --
        share a single lookup rather than searching for their shared parent
        twice.
        """
        if ancestor in state.scope_vars:
            return state.scope_vars[ancestor]
        role, name = ancestor
        state.capabilities.add("ELEMENT_TREE")
        args = self._role_arg(role, state)
        taken = set(state.scope_vars.values()) | set(state.windows.values()) | _RESERVED
        var_name = _identifier(name or role, taken)
        state.lines.append(f"{var_name} = gui.element({args}, name={_literal(name)})")
        state.scope_vars[ancestor] = var_name
        return var_name

    def _role_arg(self, role: str, state: _State) -> str:
        """Render the `role=` argument, as a Role constant where one exists."""
        constant = _ROLE_CONSTANTS.get(role)
        if constant is None:
            return f"role={_literal(role)}"
        state.roles.add(constant)
        return f"role=Role.{constant}"

    def _point(self, target: Target, state: _State) -> str:
        """Render a coordinate pair, window-relative where that is possible."""
        if self.options.locators == "absolute":
            return f"{target.x}, {target.y}"
        relative = target.relative
        if relative is None or target.window is None or not target.window.addressable:
            return f"{target.x}, {target.y}"
        name = self._window_var(target.window, state)
        geometry = target.window.geometry
        origin = (geometry[0], geometry[1]) if geometry else None
        self._ensure_geometry(name, state, origin)
        dx, dy = relative
        return f"{name}_x + {dx}, {name}_y + {dy}"

    def _ensure_geometry(
        self, name: str, state: _State, origin: tuple[int, int] | None
    ) -> None:
        """Read a window's origin, again whenever it could have gone stale.

        Re-read when the window *moved during the recording*, not only when a
        different window is addressed. Every offset below is relative to where
        the window was at the moment that event was captured, so one geometry
        read shared across a move puts every later coordinate out by however
        far it travelled. Seen live: a drag inside a window dragged the window,
        and its origin went from (0, 0) to (-160, 0) halfway through.
        """
        if state.geometry_for == name and state.geometry_origin == origin:
            return
        state.capabilities.add("WINDOW_GEOMETRY")
        if self.options.comments and state.geometry_for is None:
            state.lines.append(
                "# coordinates below are relative to this window's origin"
            )
        elif self.options.comments and state.geometry_for == name:
            state.lines.append("# the window moved, so its origin is read again")
        state.lines.append(f"{name}_x, {name}_y, _, _ = gui.geometry({name})")
        state.geometry_for = name
        state.geometry_origin = origin
        # The pointer expression is written in terms of that origin, so it
        # means something different now.
        state.pointer = None

    def _window_var(
        self, window: WindowRef, state: _State, timeout: float | None = None
    ) -> str:
        """Bind a window to a variable, waiting for it the first time it is used.

        Identity and readability want different fields, so they get different
        ones. The *key* is `(app_id, title, pid)`, not `(app_id, title)`
        alone: two terminal windows of one app share an app_id but are
        different windows, and app_id alone collapsed the second one's
        clicks onto the first one's binding. Title is safe to add to the key
        precisely because the resolver already pins it to what the window
        was first seen as and reuses that for every later mention (see
        WindowRef's docstring) -- but title alone is not enough either: two
        windows launched independently (two "Open File" dialogs from
        different processes, say) can be first seen with the identical
        title, and `(app_id, title)` collapsed the second one's clicks onto
        the first one's binding exactly the way app_id alone once did for
        the terminal case above. `pid` is a third field that already
        survives serialization on `WindowRef` (unlike a live handle, which
        deliberately does not, see its docstring) and is stable for a
        window's whole life, so adding it catches every case where the
        collision is between different processes. It does not catch two
        windows of the *same* process sharing both an app_id and a
        first-seen title (two "Open File" dialogs from one running
        instance) -- `WindowRef` has nothing left that distinguishes those,
        by design, and they still collapse to one binding. The *name*
        prefers the title on its own, because it is what a reader
        recognizes: once X11 began reporting app ids, a window called
        "Recorder Check" was binding to `zenity`, which is true and
        unhelpful.
        """
        key = (window.app_id, window.title, window.pid)
        if key in state.windows:
            return state.windows[key]
        state.capabilities.add("WINDOW_LIST")
        readable = window.title or window.app_id
        name = _identifier(
            readable or "window", set(state.windows.values()) | _RESERVED
        )
        state.windows[key] = name
        wait = timeout if timeout is not None else self.options.default_timeout
        state.lines.append(f"{name} = {self._window_lookup(window, wait, state)}")
        return name

    def _window_lookup(self, window: WindowRef, wait: float, state: _State) -> str:
        """Render the call that finds `window` again at replay time.

        `wait_for_window` matches a plain-string title literally, as a
        substring -- see `_title_pattern`. `expect_window` raises
        `WindowNotFound` in place of `wait_for_window`'s `None`, and does
        nothing else: it is "a lookup and nothing else" in its own words, and
        deliberately does not raise, focus or settle the window it hands back.
        (This docstring used to claim it settled focus and geometry; it does
        not, and never has -- `focus_window` is the call that waits for a
        window's geometry to stop moving, which is why the emitted script
        pairs the two.)

        A title seen to drift during the recording is not used at all. Titles
        drift constantly, an editor appending its document name being the
        documented case, and matching on one is the most common reason a
        generated script stops finding its window.
        """
        if window.title and (window.title_stable or not window.app_id):
            if not window.title_stable:
                self._comment(
                    "this window's title changed during recording and it has no"
                    " app id, so this match is fragile",
                    state,
                )
            return (
                f"gui.expect_window({_title_pattern(window.title)}, timeout={wait:g})"
            )
        if not window.app_id:
            # Neither identity: callers guard on `addressable`, so this is
            # only reachable through a hand-edited recording.
            self._comment("this window had no title and no app id", state)
            return "gui.active_window()"
        if window.title:
            self._comment(
                f"title drifted while recording (last seen {window.title!r});"
                " matching on the app id instead",
                state,
            )
        app_id = _literal(window.app_id)
        # An app id is protocol-specific, and a recording made through XWayland
        # has only ever seen the X11 half of it. pyguitest matches an app id
        # exactly and takes several for this reason -- "neither is derivable
        # from the other", as `_app_id_match` puts it -- but a recording can
        # only ever know the one it saw, so the other has to be named by hand.
        # Worth a comment rather than nothing: replayed against the same
        # application running natively this raises WindowNotFound on its first
        # line, which is loud but says nothing about why. Confirmed live on
        # GNOME Shell 51.rc, recording gedit through XWayland (`Gedit`) and
        # replaying against the native Wayland copy (`gedit`).
        if "xwayland" in (state.session_type or "").lower():
            self._comment(
                f"recorded through XWayland, so {app_id} is the X11 WM_CLASS. The "
                "same application run as a native Wayland client reports a "
                "different app id, and this line will not find it. To replay on "
                f'both, name both: app_id=({app_id}, "<the Wayland one>")',
                state,
            )
        return f"gui.expect_window(app_id={app_id}, timeout={wait:g})"

    # -- assembly ------------------------------------------------------------

    def _assemble(self, recording: Recording, state: _State) -> str:
        """Wrap the emitted body in imports, a preamble and an entry point."""
        out: list[str] = []
        if self.options.include_header:
            out.extend(_header(recording, state, self.options.header))
        out.extend(_imports(state, self.options))
        out.append("")
        suppression = _warning_suppression(self.options)
        if suppression:
            out.extend(suppression)
            out.append("")
        out.extend(_secret_bindings(state))
        out.append("")
        out.append(f"def {self.options.function_name}() -> None:")
        out.append('    """Replay the recorded interaction."""')
        out.append("    with pyguitest.connect() as gui:")
        body = _collapse_taps(_drop_unused_bindings(state.lines))
        if self.options.capability_preamble and state.capabilities:
            body = _require_lines(state.capabilities) + [""] + body
        out.extend(f"        {line}" if line else "" for line in body)
        if not any(line and not line.startswith("#") for line in body):
            # A body of comments alone (every event turned out unaddressable)
            # is not a statement -- Python needs one after `with`, or this
            # does not even parse.
            out.append("        pass")
        out.append("")
        out.append("")
        out.append('if __name__ == "__main__":')
        out.append(f"    {self.options.function_name}()")
        if self.options.include_header:
            out.extend(_footer(recording, state))
        return "\n".join(out) + "\n"


_TAP = re.compile(r'^gui\.tap_key\(("[^"]*"|\'[^\']*\')\)$')
"""A generated line that taps one named key, for the repeat pass."""

_TAP_RUN = 4
"""Taps of one key below which the literal repetition still reads better.

A double or triple tap is clearer written out; twenty is a wall. The
threshold is deliberately above the double-tap most keys get.
"""


def _collapse_taps(body: list[str]) -> list[str]:
    """Fold a long run of one key's taps into the loop it obviously is.

    Clearing a field by holding Backspace is one thing the person did, and a
    real recording rendered it as twenty consecutive identical lines. The
    recording keeps every tap -- this is a rendering decision, so a recording
    made before this existed picks it up on `--regenerate`.
    """
    out: list[str] = []
    index = 0
    while index < len(body):
        match = _TAP.match(body[index])
        if match is None:
            out.append(body[index])
            index += 1
            continue
        run = 1
        while index + run < len(body) and body[index + run] == body[index]:
            run += 1
        if run < _TAP_RUN:
            out.extend(body[index : index + run])
        else:
            out.append(f"for _ in range({run}):")
            out.append(f"    {body[index]}")
        index += run
    return out


def _dragged_its_own_window(event: Drag) -> bool:
    """Whether this drag moved the very window its coordinates are relative to.

    Recognised by the window being the same one at both ends while its origin
    is not: only the window moving under a held button does that.
    """
    start, end = event.start.window, event.end.window
    if start is None or end is None:
        return False
    if start.geometry is None or end.geometry is None:
        return False
    # Both fields together, as in _window_var: app_id alone is shared by
    # every window of one app, so it cannot rule out a drag that starts in
    # one window of an app and ends in a different window of that same app.
    if (start.app_id, start.title) != (end.app_id, end.title):
        return False
    return start.geometry[:2] != end.geometry[:2]


_BINDING = re.compile(
    r"^(\w+) = (?:gui\.wait_for_window|gui\.active_window|gui\.expect_window)\("
)
"""A generated line that binds a window handle, for the unused-name pass."""


def _drop_unused_bindings(body: list[str]) -> list[str]:
    """Strip the variable off a window lookup nothing goes on to use.

    A window is bound the first time it is mentioned, before the generator can
    know whether anything will want the handle -- and a recording whose clicks
    all resolved to named elements never does. The call still has to happen,
    because it is the synchronization; only the name is surplus. Dropping it
    keeps generated files clean under the reader's own linter.
    """
    out = list(body)
    bindings = {
        match.group(1): index
        for index, line in enumerate(body)
        if (match := _BINDING.match(line))
    }
    for name, index in bindings.items():
        used = re.compile(rf"\b{re.escape(name)}\b")
        if not any(used.search(line) for i, line in enumerate(body) if i != index):
            out[index] = body[index].split(" = ", 1)[1]
    return out


def _require_lines(capabilities: set[str]) -> list[str]:
    """Render the capability preamble, wrapped to the line limit."""
    names = [f"Capability.{c}" for c in sorted(capabilities)]
    single = f"gui.require({', '.join(names)})"
    if len(single) + 8 <= 88:
        return [single]
    lines = ["gui.require("]
    lines.extend(f"    {name}," for name in names)
    lines.append(")")
    return lines


def _imports(state: _State, options: GeneratorOptions) -> list[str]:
    """Render the import block the generated body actually needs."""
    lines: list[str] = []
    stdlib = (["os"] if state.secrets else []) + (
        ["warnings"] if options.suppress_keymap_warning else []
    )
    if stdlib:
        lines.extend(f"import {name}" for name in stdlib)
        lines.append("")
    lines.append("import pyguitest")
    names = []
    if state.capabilities:
        names.append("Capability")
    if state.roles:
        names.append("Role")
    if names:
        lines.append(f"from pyguitest import {', '.join(names)}")
    return lines


def _warning_suppression(options: GeneratorOptions) -> list[str]:
    """Render the module-level statements silencing configured warning noise.

    Two independent, independently-off-by-default switches: `KeymapWarning`
    is pyguitest's own, filtered the ordinary way through the `warnings`
    module; the AT-SPI "dbind" chatter is native GLib log output that never
    goes through Python's warnings machinery at all, so silencing it needs
    GLib's own log handler instead -- best-effort, since not every replay
    target has PyGObject installed.
    """
    lines: list[str] = []
    if options.suppress_keymap_warning:
        lines.extend(
            [
                "# --suppress-keymap-warning: uinput's keyboard-layout caveat is",
                "# silenced below; typed text still depends on the layout matching.",
                "from pyguitest.backends.input import KeymapWarning",
                "",
                'warnings.filterwarnings("ignore", category=KeymapWarning)',
            ]
        )
    if options.suppress_atspi_chatter:
        lines.extend(
            [
                "# --suppress-atspi-chatter: GLib's own AT-SPI/dbind log noise is",
                "# silenced below; unrelated to this script's own correctness.",
                "try:",
                "    from gi.repository import GLib",
                "",
                "    GLib.log_set_handler(",
                '        "dbind",',
                "        GLib.LogLevelFlags.LEVEL_WARNING,",
                "        lambda *a, **k: None,",
                "    )",
                "except Exception:",
                "    pass",
            ]
        )
    return lines


def _secret_bindings(state: _State) -> list[str]:
    """Render environment lookups standing in for redacted input."""
    if not state.secrets:
        return []
    lines = [
        "# Redacted: these values were not written into this script. Supply",
        "# them through the environment before replaying.",
    ]
    lines.extend(f'{name} = os.environ["{name}"]' for name in state.secrets)
    return lines


def _header(recording: Recording, state: _State, custom: str = "") -> list[str]:
    """Render the module docstring describing where the recording came from.

    A caller's own text leads where there is any -- it is what they wanted the
    file to open with -- but the "Generated by" line and the version block
    still follow it. Provenance is not something setting a header should be
    able to drop by accident; `include_header = false` is how you drop it on
    purpose.
    """
    env = recording.environment
    # Escaped, not rejected: this text is spliced straight into the module's
    # own triple-quoted docstring, and an embedded `"""` (a licence block, a
    # ticket reference, anything pasted from elsewhere) would otherwise close
    # it early -- turning the rest of the intended header into bare
    # top-level string statements and leaving a later closing `"""` to
    # reopen an unterminated string that swallows the remainder of the
    # module. `\"""` inside a triple-quoted string is valid Python and reads
    # as a literal `"""` in the rendered docstring.
    supplied = custom.strip().replace('"""', '\\"""').splitlines()
    if supplied:
        lines = [f'"""{supplied[0]}', *supplied[1:], ""]
        lines.append("Generated by pyguitest-recorder. Edit freely.")
    else:
        lines = ['"""Generated by pyguitest-recorder. Edit freely.']
    lines.append("")
    # Both versions, not just pyguitest's: the profile says which API the
    # calls were checked against, and this says which recorder wrote them --
    # which is the question asked first when a generated script turns out to
    # have a bug in its own shape rather than in the application.
    parenthetical = ", ".join(p for p in (plain_name(env.compositor), env.desktop) if p)
    detail = [
        f"Recorder:    pyguitest-recorder {env.recorder_version or 'unknown'}",
        f"Recorded at: {env.recorded_at or 'unknown'}",
        f"Profile:     {PROFILE}",
        f"Recorded on: {plain_name(env.session_type) or 'unknown'} "
        f"{('(' + parenthetical + ')') if parenthetical else ''}".rstrip(),
        f"Capture:     {env.capture_backend or 'unknown'}",
        f"pyguitest:   {env.pyguitest_version or 'unknown'}",
    ]
    lines.extend(d for d in detail if d)
    if env.xwayland:
        lines.append("")
        lines.append("Recorded through XWayland: native Wayland clients were invisible")
        lines.append("to the capture backend and are absent from this script.")
    # Only a pointer to the notes, not the notes themselves. They answer the
    # question a reader asks *second* -- why a script is all coordinates when
    # the whole point of the tool is that it should not be -- and one recording
    # of a text editor produced enough of them to bury the code under forty
    # lines of preamble before the first import.
    count = len(_notes(recording, state))
    if count:
        lines.append("")
        lines.append(
            f"{count} note{'s' if count > 1 else ''} about how this was recorded "
            f"{'are' if count > 1 else 'is'} at the end of this file."
        )
    lines.append('"""')
    return lines


def _notes(recording: Recording, state: _State) -> list[str]:
    """Everything the recording has to say about how it went."""
    return [*recording.environment.notes, *(f"WARNING: {w}" for w in state.warnings)]


def _footer(recording: Recording, state: _State) -> list[str]:
    """Render the notes as a trailing comment block, or nothing."""
    notes = _notes(recording, state)
    if not notes:
        return []
    lines = ["", "", "# Notes from the recording, in the order they arose:"]
    for note in notes:
        wrapped = textwrap.wrap(note, width=72)
        lines.append(f"#   - {wrapped[0]}")
        lines.extend(f"#     {rest}" for rest in wrapped[1:])
    return lines


def _title_pattern(title: str) -> str:
    r"""Render a window title as the literal `wait_for_window` wants.

    pyguitest's own `wait_for_window`/`find_window`/`window_element` match a
    plain string literally (as a substring), escaping it internally -- only
    a compiled `re.Pattern` is treated as regex. Before that fix, a plain
    string was always compiled as regex, so this function used to escape the
    title itself: "Document (1)" was otherwise a pattern matching "Document
    1", and a title with an unbalanced bracket did not compile at all.
    Escaping here too, now, would double-escape -- `re.escape` on a string
    that already contains literal backslashes from a first escaping pass
    turns `\\(` into `\\\\(`, which then matches nothing real. So this emits
    the raw title unchanged and lets pyguitest do the one escape.
    """
    return _literal(title)


def _unaddressable_reason(window: WindowRef) -> str:
    """Explain, for a generated comment, why `window.addressable` is False.

    Two different situations look identical from `addressable` alone: a
    window with neither field at all, and one whose only field, app_id, is
    shared with another window open at the same time (seen live on KDE: a
    desktop shell's own desktop, panels, and popups can all report the same
    app_id) and so cannot be trusted to mean this one specifically.
    """
    if window.app_id:
        return (
            f"no title, and its app id {window.app_id!r} was shared by another "
            "open window at the time"
        )
    return "no title and no app id"


def _hotkey_string(keys: tuple[str, ...]) -> str:
    """Render a modifier combination in send_keys' `^%+#` grammar."""
    prefixes = {
        "ctrl": "^",
        "control": "^",
        "alt": "%",
        "shift": "+",
        "meta": "#",
        "super": "#",
        "altgr": "&",
    }
    mods = [prefixes[k.lower()] for k in keys[:-1] if k.lower() in prefixes]
    body = _sendkeys_atom(keys[-1])
    for mod in reversed(mods):
        body = f"{mod}({body})"
    return body


def _sendkeys_atom(key: str) -> str:
    """Render one key as send_keys reads it.

    The full keysym name goes inside the braces, not the three-letter
    abbreviation: send_keys looks a braced name up in the backend's aliases
    and *otherwise passes it to press_key as written*, so `{Return}` resolves
    while `{RET}` -- which is not the alias, `ENT` is -- resolves as neither.
    Truncating was wrong for Return, Down, Next and Prior alike.

    A one-character key that happens to be grammar (`+`, `(`, `^`) is braced
    for the same reason `quote_for_type` braces it.
    """
    if len(key) == 1 and key not in _SENDKEYS_SPECIAL:
        return key
    return "{" + key + "}"


def _format(source: str) -> str:
    """Normalize layout with `ruff format`, or return the input unchanged.

    Layout is not this module's job. Emitting roughly-right lines and handing
    them to a real formatter is both shorter and more reliable than getting
    line wrapping right by hand -- and it is the same formatter the project
    lints with, so generated files match hand-written ones.
    """
    ruff = shutil.which("ruff")
    if ruff is None:
        return source
    try:
        done = subprocess.run(
            [ruff, "format", "--stdin-filename", "generated.py", "-"],
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return source
    return done.stdout if done.returncode == 0 and done.stdout else source


def _session_methods() -> frozenset[str]:
    """The method names the installed pyguitest Session actually offers."""
    return _public_names("Session")


def _public_names(attribute: str) -> frozenset[str]:
    """The public attribute names of one pyguitest export, or nothing."""
    try:
        import pyguitest
    except ImportError:  # pragma: no cover - pyguitest is a hard dependency
        return frozenset()
    holder = getattr(pyguitest, attribute, None)
    if holder is None:  # pragma: no cover - every name here is exported
        return frozenset()
    return frozenset(n for n in dir(holder) if not n.startswith("_"))


def generate(recording: Recording, options: GeneratorOptions | None = None) -> str:
    """Render `recording` as pyguitest source."""
    return PythonGenerator(options).render(recording)


def validate(source: str) -> list[str]:
    """Check generated source compiles and only calls methods pyguitest has.

    Returns the list of problems; empty means the file is safe to offer for
    export. This is the step that stops the recorder shipping a script naming
    a function that does not exist -- the failure the design document's own
    example made.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error at line {exc.lineno}: {exc.msg}"]
    return _unknown_api(tree) + _unbound_names(tree)


def _unknown_api(tree: ast.AST) -> list[str]:
    """Report calls and constants the installed pyguitest does not have."""
    problems: list[str] = []
    holders = {
        "gui": ("pyguitest.Session has no method", _session_methods()),
        "Capability": ("pyguitest has no Capability", _public_names("Capability")),
        "Role": ("pyguitest has no Role", _public_names("Role")),
    }
    element_names = _public_names("Element")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        value = node.value
        if isinstance(value, ast.Name) and value.id in holders:
            message, known = holders[value.id]
            if known and node.attr not in known:
                problems.append(f"{message} {node.attr!r}")
        elif element_names and _is_element_call(value):
            # `gui.element(...).double_click()` is an attribute read on a
            # *result*, not on a `gui` name, so the check above cannot see it --
            # and it is the one API surface a generated script calls that way.
            # Unchecked, a method the installed pyguitest does not have reached
            # the file and failed at replay with an AttributeError naming it.
            if node.attr not in element_names:
                problems.append(f"pyguitest.Element has no method {node.attr!r}")
    return problems


def _is_element_call(node: ast.AST) -> bool:
    """Whether `node` is a `gui.<factory>(...)` call that hands back an Element."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _ELEMENT_FACTORIES
        and isinstance(func.value, ast.Name)
        and func.value.id == "gui"
    )


def _unbound_names(tree: ast.AST) -> list[str]:
    """Report names the generated module reads without ever binding.

    Deliberately scope-blind: every binding anywhere in the file counts
    everywhere. That can miss a name bound only in some other function, and
    never invents a problem that is not one -- the right trade for a check
    whose whole job is to stop a generated script failing at run time.

    This is the check that would have caught the pid `wait_for_idle` used to
    read off an `app` variable nothing defined. Compiling the source does not
    catch it; only running the script did.
    """
    bound = {n for n in dir(builtins) if not n.startswith("_")} | _MODULE_DUNDERS
    used: dict[str, int] = {}
    for node in ast.walk(tree):
        bound.update(_bindings(node))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.setdefault(node.id, node.lineno)
    return [
        f"line {line}: {name!r} is used but never assigned"
        for name, line in sorted(used.items(), key=lambda item: item[1])
        if name not in bound
    ]


def _bindings(node: ast.AST) -> set[str]:
    """Every name `node` binds on its own account."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        return {node.id}
    if isinstance(node, ast.arg):
        return {node.arg}
    if isinstance(node, ast.alias):
        # `import a.b.c` binds only `a` in the enclosing scope (`a.b.c` is
        # reached through it, never as its own name) -- split(".")[0] is
        # that rule, not an arbitrary truncation. An `as` alias has no dots
        # to strip, so it passes through unchanged either way.
        return {(node.asname or node.name).split(".")[0]}
    if isinstance(node, ast.ExceptHandler) and node.name:
        return {node.name}
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return {node.name}
    if isinstance(node, ast.Global | ast.Nonlocal):
        return set(node.names)
    return set()
