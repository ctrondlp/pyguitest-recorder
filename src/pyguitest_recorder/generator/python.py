"""Render canonical events as pyguitest source.

Everything emitted here is checked against the installed pyguitest: the
generator asks `pyguitest.Session` whether a method exists before it will emit
a call to it, and `validate` compiles the finished file. A recorder whose
output does not import is worse than no recorder, and the failure mode is
silent -- a plausible-looking script that names a function the library does
not have.

Three emission rules carry the design.

*Elements lead, coordinates follow.* A click on a widget AT-SPI could name
becomes `gui.button("Save").click()`, not a coordinate. Coordinates are the
last resort, in the order window-relative then absolute, because a coordinate
is the one locator guaranteed to break when the window moves.

*Scripts declare what they need.* Every generated file opens with
`gui.require(...)` naming the capabilities it uses. A recording made on X11
and replayed on Wayland should fail on the first line with a typed exception,
not halfway through with a click that went nowhere.

*Waits are synchronization, not sleeps.* The analyzer infers wait events; this
module renders them. `gui.wait(...)` appears only where nothing better could
be inferred, and says so in a comment.
"""

from __future__ import annotations

import ast
import builtins
import keyword
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass, field
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
)

__all__ = [
    "GeneratorOptions",
    "PythonGenerator",
    "generate",
    "validate",
    "ValidationError",
]

PROFILE = "pyguitest-0.5"
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

_TEXT_ROLES = frozenset({"entry", "text", "password text"})

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

_APP_ID_HELPER = "_window_by_app_id"
"""Name of the emitted lookup that finds a window by application id."""

_ELEMENT_HELPER = "_expect_element"
"""Name of the lookup the `expect_` helpers share."""

_DOUBLE_CLICK_HELPER = "double_click_element"
"""Name of the emitted double click that `Element` itself cannot do."""

_HELPER_NEEDS = {
    "expect_text": {_ELEMENT_HELPER},
    "expect_checked": {_ELEMENT_HELPER},
    "expect_showing": {_ELEMENT_HELPER},
}
"""Helpers that call other helpers, so requesting one emits both.

One level deep, and deliberately not a general dependency graph: the moment
a generated file needs one of those, the thing to do is stop generating
these and put them in a library the script imports.
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


@dataclass
class _State:
    """Mutable bookkeeping for one render pass."""

    lines: list[str] = field(default_factory=list)
    capabilities: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    windows: dict[str, str] = field(default_factory=dict)
    geometry_for: str | None = None
    geometry_origin: tuple[int, int] | None = None
    pointer: str | None = None
    helpers: set[str] = field(default_factory=set)
    secrets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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
        for event in recording.events:
            self._emit(event, state)
        source = self._assemble(recording, state)
        return _format(source) if self.options.format_output else source

    # -- event dispatch ------------------------------------------------------

    def _emit(self, event: Event, state: _State) -> None:
        """Append the lines for one event."""
        handler = getattr(self, f"_emit_{type(event).__name__.lower()}", None)
        if handler is None:
            state.warnings.append(f"no renderer for {type(event).__name__}")
            return
        if self.options.comments and event.note:
            state.lines.append(f"# {event.note}")
        handler(event, state)

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

    def _emit_sync(self, event: Sync, state: _State) -> None:
        """Round-trip the input stream."""
        state.capabilities.add("INPUT_SYNC")
        state.lines.append("gui.sync()")

    def _emit_mousemove(self, event: MouseMove, state: _State) -> None:
        """Render meaningful pointer motion."""
        state.capabilities.add("POINTER_MOVE")
        self._move(event.target, state)

    def _emit_click(self, event: Click, state: _State) -> None:
        """Render a click, preferring the element under the pointer.

        Only a left click can take the element path. `Element.click()` accepts
        no button argument, so rendering a recorded right click that way would
        turn a context menu into an ordinary activation -- a script that runs
        cleanly and does the wrong thing, which is the worst outcome here.
        """
        if event.button == 1 and event.count == 2 and self._double_click(event, state):
            return
        if event.button == 1:
            call = self._element_call(event.target.element, state, "click()")
            if call is not None:
                state.lines.extend([call] * event.count)
                state.pointer = None
                self._note_element_repeat(event, state)
                return
        state.capabilities.update({"POINTER_MOVE", "POINTER_BUTTON"})
        self._move(event.target, state)
        self._note_button_fallback(event, state)
        button = "" if event.button == 1 else str(event.button)
        if event.count == 2:
            state.lines.append(f"gui.double_click({button})")
        else:
            state.lines.extend([f"gui.click({button})"] * event.count)
            self._note_repeat(event, state)

    def _double_click(self, event: Click, state: _State) -> bool:
        """Render a double click on a named element that stays a double click.

        `Element` has no `double_click`, so this used to degrade to two
        `Element.click()` calls and hope the toolkit read them as one gesture.
        It frequently will not -- each click is a separate round trip through
        the accessibility bus, which is slower than any double-click interval
        -- and the failure is silent: double-clicking a folder icon simply
        does not open the folder. Seen in a real recording of a file manager.

        `Session.double_click` is a real double click but goes wherever the
        pointer is, so the element is located first and its *live* extents
        drive the move. That keeps the locator an element -- the point is read
        at replay, not baked in -- while the gesture stays one gesture.
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
        state.helpers.add(_DOUBLE_CLICK_HELPER)
        state.lines.append(f"{_DOUBLE_CLICK_HELPER}(gui, {locator})")
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

        `Session.double_click` exists, but it clicks wherever the pointer
        is, not a named element -- `Element` has no double_click of its own.
        So a double click on a named element still degrades to two
        `Element.click()` calls, unlike the coordinate path.
        """
        if event.count > 1 and self.options.comments:
            word = "double" if event.count == 2 else f"{event.count}x"
            state.lines.insert(
                len(state.lines) - event.count,
                f"# recorded as a {word} click; Element has no double_click,"
                " so this is",
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
            state.warnings.append("a text check had no element to make it against")
            return
        if event.sensitive and self.options.redact_sensitive:
            expected = self._secret_name(state)
            self._check_comment(f"{element.name!r} matches the recorded value", state)
        else:
            expected = _literal(str(event.expected))
            self._check_comment(f"{element.name!r} reads {event.expected!r}", state)
        self._expect("expect_text", element, state, f"equals={expected}")

    def _check_checked(self, event: Assertion, state: _State) -> None:
        """Render a check on a checkbox, radio button or toggle."""
        element = event.target.element
        if element is None:
            state.warnings.append("a checked check had no element to make it against")
            return
        word = "is checked" if event.expected else "is not checked"
        self._check_comment(f"{element.name!r} {word}", state)
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
            state.warnings.append("a showing check had no element to make it against")
            return
        self._check_comment(f"{element.name!r} is showing", state)
        self._expect("expect_showing", element, state)

    def _check_window(self, event: Assertion, state: _State) -> None:
        """Render a check that a window is open, where no element could be named."""
        window = event.target.window
        if window is None or not window.title:
            state.warnings.append("a window check had no window to make it against")
            return
        self._check_comment(f"the {window.title!r} window is open", state)
        state.capabilities.add("WINDOW_LIST")
        state.helpers.add("expect_window")
        state.lines.append(f"expect_window(gui, {_title_pattern(window.title)})")

    def _check_nothing(self, event: Assertion, state: _State) -> None:
        """Record that a check was asked for where nothing could be identified.

        A warning rather than a comment, so it reaches the header even with
        comments switched off. A check the recorder quietly dropped is worse
        than one it admits it could not make: the script would otherwise look
        like it verifies something it never does.
        """
        state.warnings.append(
            f"a check was recorded at ({event.target.x}, {event.target.y}), where "
            "neither an element nor a window could be identified; nothing was "
            "generated for it"
        )

    def _expect(
        self, helper: str, element: ElementRef, state: _State, extra: str = ""
    ) -> None:
        """Emit one `expect_` call against a named element."""
        state.capabilities.add("ELEMENT_TREE")
        state.helpers.add(helper)
        args = self._role_arg(element, state)
        tail = f", {extra}" if extra else ""
        state.lines.append(
            f"{helper}(gui, {args}, name={_literal(element.name)}{tail})"
        )

    def _check_comment(self, what: str, state: _State) -> None:
        """Label a check in the words of the person who will read it."""
        self._comment(f"Check: {what}", state)

    def _emit_drag(self, event: Drag, state: _State) -> None:
        """Render a drag as pyguitest's own drag primitive."""
        state.capabilities.update({"POINTER_MOVE", "POINTER_BUTTON", "TIMING"})
        start = self._point(event.start, state)
        end = self._point(event.end, state)
        button = "" if event.button == 1 else f", button={event.button}"
        state.lines.append(f"gui.drag(({start}), ({end}){button})")
        state.pointer = end

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
        text = self._secret(event, state) if event.sensitive else _literal(event.text)
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
        state.capabilities.add("TEXT_ENTRY")
        state.lines.append(f"gui.type_text({text})")

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

    def _emit_keystroke(self, event: KeyStroke, state: _State) -> None:
        """Render a single key tap."""
        state.capabilities.add("KEY_EVENT")
        state.lines.append(f"gui.tap_key({_literal(event.key)})")

    def _emit_hotkey(self, event: HotKey, state: _State) -> None:
        """Render a modifier combination in send_keys' grammar.

        `KEY_EVENT`, not `TEXT_ENTRY`: send_keys declares no capability of its
        own and reaches the keyboard through `press_key`, so a script that
        required TEXT_ENTRY could pass `require()` on a backend that cannot
        press a modifier at all.
        """
        state.capabilities.add("KEY_EVENT")
        state.lines.append(f"gui.send_keys({_literal(_hotkey_string(event.keys))})")

    def _emit_windowactivate(self, event: WindowActivate, state: _State) -> None:
        """Render raising a window, binding it to a variable first."""
        if not event.window.addressable:
            self._comment("a window with no title and no app id was activated", state)
            return
        name = self._window_var(event.window, state)
        state.capabilities.add("WINDOW_ACTIVATE")
        state.lines.append(f"gui.activate_window({name})")
        # Raising a window can move it, so both the cached origin and the
        # pointer position stop being trustworthy here.
        state.geometry_for = None
        state.geometry_origin = None
        state.pointer = None

    def _emit_waitforwindow(self, event: WaitForWindow, state: _State) -> None:
        """Render waiting for a window to appear."""
        if not event.window.addressable:
            self._comment("waited for a window with no title and no app id", state)
            return
        self._window_var(event.window, state, timeout=event.timeout)

    def _emit_waitforelement(self, event: WaitForElement, state: _State) -> None:
        """Render waiting for an element to appear."""
        state.capabilities.add("ELEMENT_TREE")
        args = self._role_arg(event.element, state)
        if event.element.name:
            args += f", name={_literal(event.element.name)}"
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

    def _move(self, target: Target, state: _State) -> None:
        """Put the pointer at `target`, unless it is already known to be there.

        Tracking the last emitted position is what keeps a click and the
        scroll that follows it at the same place from emitting the same move
        twice, and is why a scroll can afford to always ask for one.
        """
        point = self._point(target, state)
        if point == state.pointer:
            return
        screen = f", screen={target.screen}" if target.screen else ""
        state.lines.append(f"gui.move_mouse({point}{screen})")
        state.pointer = point

    def _element_call(
        self, element: ElementRef | None, state: _State, action: str
    ) -> str | None:
        """Return an element-based call, or None if no element can be named."""
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
        sugar = _SUGAR.get(element.role)
        if sugar is not None:
            return f"gui.{sugar}({_literal(element.name)})"
        args = self._role_arg(element, state)
        return f"gui.element({args}, name={_literal(element.name)})"

    def _role_arg(self, element: ElementRef, state: _State) -> str:
        """Render the `role=` argument, as a Role constant where one exists."""
        constant = _ROLE_CONSTANTS.get(element.role)
        if constant is None:
            return f"role={_literal(element.role)}"
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
        ones. The *key* prefers the app id, because that is what does not
        drift and two mentions of one window have to collapse to one binding.
        The *name* prefers the title, because it is what a reader recognizes:
        once X11 began reporting app ids, a window called "Recorder Check" was
        binding to `zenity`, which is true and unhelpful.
        """
        key = window.app_id or window.title
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

        `wait_for_window` takes a *regex* and searches with it, so a title is
        escaped on the way in -- "Untitled Document 1 (modified)" is otherwise
        a pattern that matches a different string than the one recorded, and
        one containing an unbalanced bracket does not compile at all.

        A title seen to drift during the recording is not used at all. Titles
        drift constantly, an editor appending its document name being the
        documented case, and matching on one is the most common reason a
        generated script stops finding its window. pyguitest has no lookup by
        application id, so this emits the small one it needs.
        """
        if window.title and (window.title_stable or not window.app_id):
            if not window.title_stable:
                self._comment(
                    "this window's title changed during recording and it has no"
                    " app id, so this match is fragile",
                    state,
                )
            return (
                f"gui.wait_for_window({_title_pattern(window.title)}, timeout={wait:g})"
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
        state.helpers.add(_APP_ID_HELPER)
        app_id = _literal(window.app_id)
        return f"{_APP_ID_HELPER}(gui, {app_id}, {wait:g})"

    # -- assembly ------------------------------------------------------------

    def _assemble(self, recording: Recording, state: _State) -> str:
        """Wrap the emitted body in imports, a preamble and an entry point."""
        out: list[str] = []
        if self.options.include_header:
            out.extend(_header(recording, state, self.options.header))
        out.extend(_imports(state))
        out.append("")
        out.extend(_secret_bindings(state))
        out.append("")
        for helper in _helpers_needed(state.helpers):
            out.extend(_HELPER_SOURCE[helper].splitlines())
            out.append("")
        out.append(f"def {self.options.function_name}() -> None:")
        out.append('    """Replay the recorded interaction."""')
        out.append("    with pyguitest.connect() as gui:")
        body = _collapse_taps(_drop_unused_bindings(state.lines))
        if self.options.capability_preamble and state.capabilities:
            body = _require_lines(state.capabilities) + [""] + body
        out.extend(f"        {line}" if line else "" for line in body)
        if not body:
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


def _helpers_needed(requested: set[str]) -> list[str]:
    """Every helper the file needs, including the ones helpers call themselves."""
    needed = set(requested)
    for helper in requested:
        needed |= _HELPER_NEEDS.get(helper, set())
    return sorted(needed)


_BINDING = re.compile(
    r"^(\w+) = (?:gui\.wait_for_window|gui\.active_window|_window_by_app_id)\("
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


def _imports(state: _State) -> list[str]:
    """Render the import block the generated body actually needs."""
    lines: list[str] = []
    stdlib = (["os"] if state.secrets else []) + (
        ["time"] if _APP_ID_HELPER in state.helpers else []
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
    supplied = custom.strip().splitlines()
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
    detail = [
        f"Recorder:    pyguitest-recorder {env.recorder_version or 'unknown'}",
        f"Profile:     {PROFILE}",
        f"Recorded on: {env.session_type or 'unknown'} "
        f"{('(' + env.compositor + ')') if env.compositor else ''}".rstrip(),
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


_HELPER_SOURCE = {
    _APP_ID_HELPER: '''def _window_by_app_id(gui, app_id, timeout=10.0):
    """Return the first window with this application id, waiting for it.

    pyguitest finds windows by title regex only, and this recording's window
    changed its title while it was being made. Application ids do not drift,
    so the lookup is done here instead.
    """
    deadline = time.monotonic() + timeout
    while True:
        for window in gui.windows():
            if window.app_id == app_id:
                return window
        if time.monotonic() >= deadline:
            raise LookupError(f"no window with app_id {app_id!r} appeared")
        gui.wait(0.25)
''',
    _DOUBLE_CLICK_HELPER: '''def double_click_element(gui, element):
    """Double-click a named element, which Element cannot do for itself.

    Two `element.click()` calls are not a double click: each is a separate
    round trip over the accessibility bus, which is slower than any toolkit's
    double-click interval, so the pair arrives as two single clicks and a
    double-clicked folder icon simply does not open.

    The element is still the locator -- its rectangle is read here, at replay,
    rather than baked in when the recording was made -- and only the gesture
    falls back to the pointer, because that is where pyguitest's real
    double_click lives.
    """
    x, y, width, height = gui.extents(element)
    gui.move_mouse(x + width // 2, y + height // 2)
    gui.double_click()
''',
    _ELEMENT_HELPER: '''def _expect_element(gui, role, name, timeout):
    """Return the named element, or fail saying it never appeared."""
    element = gui.wait_for_element(role=role, name=name, timeout=timeout)
    if element is None:
        raise AssertionError(
            f"expected an element named {name!r} with role {role!r} to be "
            f"showing, but none appeared within {timeout:g}s"
        )
    return element
''',
    "expect_text": '''def expect_text(gui, role, name, equals, timeout=5.0):
    """Fail unless the named element reads `equals`.

    Re-read until `timeout` rather than checked once. A check recorded right
    after the action it verifies would otherwise race the application, which
    has not necessarily finished redrawing by the time the click returns.
    """
    element = _expect_element(gui, role, name, timeout)
    if gui.wait_until(lambda: element.text == equals, timeout=timeout):
        return
    raise AssertionError(
        f"expected {name!r} to read {equals!r}, but it reads {element.text!r}"
    )
''',
    "expect_checked": '''def expect_checked(gui, role, name, checked, timeout=5.0):
    """Fail unless the named checkbox, radio button or toggle is in `checked`."""
    element = _expect_element(gui, role, name, timeout)
    if gui.wait_until(lambda: element.checked == checked, timeout=timeout):
        return
    wanted = "checked" if checked else "unchecked"
    actual = "checked" if element.checked else "unchecked"
    raise AssertionError(f"expected {name!r} to be {wanted}, but it is {actual}")
''',
    "expect_showing": '''def expect_showing(gui, role, name, timeout=5.0):
    """Fail unless the named element is present and visible."""
    element = _expect_element(gui, role, name, timeout)
    if gui.wait_until(lambda: element.visible, timeout=timeout):
        return
    raise AssertionError(f"expected {name!r} to be showing, but it is not visible")
''',
    "expect_window": '''def expect_window(gui, title, timeout=5.0):
    """Fail unless a window whose title matches `title` is open.

    `title` is a regular expression, the same as `wait_for_window` takes.
    """
    if gui.wait_for_window(title, timeout=timeout) is not None:
        return
    raise AssertionError(
        f"expected a window matching {title!r} to be open, but none "
        f"appeared within {timeout:g}s"
    )
''',
}
"""Functions the generated module carries when it needs them.

The `expect_` family is what makes a recorded check readable to whoever
inherits the script. `assert gui.element(...).text == "Saved"` says nothing
when it fails -- an `AssertionError` and a line number -- where these name
the element, what it was supposed to read and what it actually reads. The
retry is the other half: a check is written the instant the action returns,
which is earlier than the application finishes responding to it.
"""


def _title_pattern(title: str) -> str:
    """Render a window title as the regex literal `wait_for_window` wants.

    Escaped, because the title is matched as a pattern and not as text:
    "Document (1)" is otherwise a pattern that matches "Document 1", and a
    title with an unbalanced bracket in it does not compile at all.

    The space and hyphen escaping `re.escape` also does is undone. Neither
    changes what the pattern matches -- a hyphen is only special inside a
    character class -- and titles are full of both, so leaving them in makes
    every generated window lookup unreadable for no benefit: an escaped
    hyphen and escaped spaces read far worse than they need to, for the one
    bracket that actually had to be escaped.
    """
    return _literal(re.escape(title).replace("\\ ", " ").replace("\\-", "-"))


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
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        value = node.value
        if not isinstance(value, ast.Name) or value.id not in holders:
            continue
        message, known = holders[value.id]
        if known and node.attr not in known:
            problems.append(f"{message} {node.attr!r}")
    return problems


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
        return {(node.asname or node.name).split(".")[0]}
    if isinstance(node, ast.ExceptHandler) and node.name:
        return {node.name}
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return {node.name}
    if isinstance(node, ast.Global | ast.Nonlocal):
        return set(node.names)
    return set()
