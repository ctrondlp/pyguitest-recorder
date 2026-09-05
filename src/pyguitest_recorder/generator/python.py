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

PROFILE = "pyguitest-0.3"
"""The API profile this generator targets, recorded in the output header.

Bumped with the pyguitest whose surface the emitted calls were actually
checked against, not with this package's own version. It is what tells a
reader of a two-year-old generated script which API it was written for, and
what `--regenerate` re-renders against when that API has moved on.
"""

# AT-SPI roles pyguitest gives a dedicated accessor. Anything else is reached
# through the general `element(role=..., name=...)` form.
_SUGAR = {
    "push button": "button",
    "check box": "checkbox",
    "combo box": "dropdown",
    "menu item": "menu_item",
    "link": "link",
}

_TEXT_ROLES = frozenset({"entry", "text", "password text"})

# Role value -> the Role enum member name, for readable output.
_ROLE_CONSTANTS = {
    "push button": "PUSH_BUTTON",
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

    Reverse-DNS app ids are the common case and their leading segments carry
    no information, so `org.gnome.TextEditor` binds to `texteditor` rather
    than `org_gnome_texteditor`, which would dominate every line it appears
    in.
    """
    if "." in base and " " not in base:
        base = base.rsplit(".", 1)[-1]
    cleaned = "".join(c if c.isalnum() else "_" for c in base.lower()).strip("_")
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"win_{cleaned}" if cleaned else "win"
    if keyword.iskeyword(cleaned) or keyword.issoftkeyword(cleaned):
        cleaned = f"{cleaned}_window"
    candidate = cleaned[:28]
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
        if event.button == 1:
            call = self._element_call(event.target.element, state, "click()")
            if call is not None:
                state.lines.extend([call] * event.count)
                state.pointer = None
                self._note_repeat(event, state)
                return
        state.capabilities.update({"POINTER_MOVE", "POINTER_BUTTON"})
        self._move(event.target, state)
        self._note_button_fallback(event, state)
        button = "" if event.button == 1 else str(event.button)
        state.lines.extend([f"gui.click({button})"] * event.count)
        self._note_repeat(event, state)

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
        """Explain a repeated click, since pyguitest has no double_click."""
        if event.count > 1 and self.options.comments:
            word = "double" if event.count == 2 else f"{event.count}x"
            state.lines.insert(
                len(state.lines) - event.count,
                f"# recorded as a {word} click; pyguitest has no double_click,"
                " so this is",
            )
            state.lines.insert(
                len(state.lines) - event.count,
                "# consecutive clicks and depends on the toolkit's"
                " double-click interval",
            )

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
        """Append an explanatory comment, unless comments are switched off."""
        if self.options.comments:
            state.lines.append(f"# {text}")

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
        if self.options.locators != "element":
            return None
        if element is None or not element.addressable:
            return None
        state.capabilities.update({"ELEMENT_TREE", "ELEMENT_ACTION"})
        sugar = _SUGAR.get(element.role)
        if sugar is not None:
            return f"gui.{sugar}({_literal(element.name)}).{action}"
        args = self._role_arg(element, state)
        return f"gui.element({args}, name={_literal(element.name)}).{action}"

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
        self._ensure_geometry(name, state)
        dx, dy = relative
        return f"{name}_x + {dx}, {name}_y + {dy}"

    def _ensure_geometry(self, name: str, state: _State) -> None:
        """Read a window's origin once, and again whenever the window changes."""
        if state.geometry_for == name:
            return
        state.capabilities.add("WINDOW_GEOMETRY")
        if self.options.comments and state.geometry_for is None:
            state.lines.append(
                "# coordinates below are relative to this window's origin"
            )
        state.lines.append(f"{name}_x, {name}_y, _, _ = gui.geometry({name})")
        state.geometry_for = name

    def _window_var(
        self, window: WindowRef, state: _State, timeout: float | None = None
    ) -> str:
        """Bind a window to a variable, waiting for it the first time it is used."""
        key = window.app_id or window.title
        if key in state.windows:
            return state.windows[key]
        state.capabilities.add("WINDOW_LIST")
        name = _identifier(key or "window", set(state.windows.values()) | _RESERVED)
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
            out.extend(_header(recording, state))
        out.extend(_imports(state))
        out.append("")
        out.extend(_secret_bindings(state))
        out.append("")
        for helper in sorted(state.helpers):
            out.extend(_HELPER_SOURCE[helper].splitlines())
            out.append("")
        out.append(f"def {self.options.function_name}() -> None:")
        out.append('    """Replay the recorded interaction."""')
        out.append("    with pyguitest.connect() as gui:")
        body = _drop_unused_bindings(state.lines)
        if self.options.capability_preamble and state.capabilities:
            body = _require_lines(state.capabilities) + [""] + body
        out.extend(f"        {line}" if line else "" for line in body)
        if not body:
            out.append("        pass")
        out.append("")
        out.append("")
        out.append('if __name__ == "__main__":')
        out.append(f"    {self.options.function_name}()")
        return "\n".join(out) + "\n"


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
        "# Recorded in sensitive mode: the text typed here was never written to",
        "# the recording. Supply it through the environment before replaying.",
    ]
    lines.extend(f'{name} = os.environ["{name}"]' for name in state.secrets)
    return lines


def _header(recording: Recording, state: _State) -> list[str]:
    """Render the module docstring describing where the recording came from."""
    env = recording.environment
    lines = ['"""Generated by pyguitest-recorder. Edit freely.', ""]
    detail = [
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
    # Anything that degraded during the recording belongs here, because the
    # question a reader asks first is why a script is all coordinates when the
    # whole point of the tool is that it should not be.
    for note in env.notes:
        lines.append("")
        lines.extend(textwrap.wrap(note, width=74))
    for warning in state.warnings:
        lines.append(f"WARNING: {warning}")
    lines.append('"""')
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
}
"""Functions the generated module carries when it needs them."""


def _title_pattern(title: str) -> str:
    """Render a window title as the regex literal `wait_for_window` wants.

    Escaped, because the title is matched as a pattern and not as text:
    "Document (1)" is otherwise a pattern that matches "Document 1", and a
    title with an unbalanced bracket in it does not compile at all.

    The space escaping `re.escape` also does is undone. It changes nothing
    about what the pattern matches and titles are mostly spaces, so leaving it
    in makes every generated window lookup unreadable for no benefit.
    """
    return _literal(re.escape(title).replace("\\ ", " "))


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
