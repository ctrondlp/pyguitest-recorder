"""Turn raw input into canonical events.

This is where "record intent, not input" is actually implemented. A press and
a release are not two events, they are a click; forty motion events between
two clicks are not forty events, they are nothing at all; and a run of
printable keystrokes is one string, not a list of keys.

The normalizer is a streaming state machine rather than a batch pass, because
the recorder UI shows events as they happen and cannot wait for the recording
to end. `feed` returns the events that became final as a result of that input,
which is usually none: a press is not yet a click, and typed characters are
held until something forces them out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..backends.base import RawEvent
from ..model import (
    Assertion,
    Click,
    Drag,
    ElementRef,
    Event,
    HotKey,
    KeyStroke,
    MouseMove,
    Origin,
    Pause,
    Scroll,
    Target,
    TextInput,
)
from ..windows.resolver import ContextResolver, NullResolver, Observation

__all__ = [
    "NormalizerOptions",
    "Normalizer",
    "MODIFIERS",
    "check_for",
    "chord_matches",
    "parse_chord",
]

MODIFIERS = {
    "Control_L": "ctrl",
    "Control_R": "ctrl",
    "Alt_L": "alt",
    "Alt_R": "alt",
    "Shift_L": "shift",
    "Shift_R": "shift",
    "Super_L": "meta",
    "Super_R": "meta",
    "Meta_L": "meta",
    "Meta_R": "meta",
    "ISO_Level3_Shift": "altgr",
}
"""X keysym names for modifiers, mapped to send_keys' modifier vocabulary."""

# Modifiers that change which character a key produces rather than what the
# keystroke means. Text containing capitals is still text, not a hotkey.
_TEXT_SAFE = frozenset({"shift", "altgr"})


def parse_chord(spec: str) -> tuple[frozenset[str], str]:
    """Split a key specification like `ctrl+F1` into its modifiers and key.

    The key keeps its case, because X keysym names are case-sensitive and
    `F1` is not `f1`; the modifiers do not, because `Ctrl` and `ctrl` plainly
    mean the same thing to anyone writing a configuration file.
    """
    parts = [part for part in spec.split("+") if part]
    if not parts:
        return (frozenset(), "")
    return (frozenset(part.lower() for part in parts[:-1]), parts[-1])


def chord_matches(spec: str, held: set[str], keysym: str) -> bool:
    """Whether `keysym`, with exactly `held` down, is the chord `spec` names.

    Exactly, not merely including: `ctrl+F1` must not fire on `ctrl+shift+F1`,
    so the combinations around a bound key stay usable in the application
    being recorded. An empty spec matches nothing, which is how a key is
    turned off.
    """
    modifiers, key = parse_chord(spec)
    return bool(key) and keysym == key and held == modifiers


# AT-SPI roles that receive typed text. Used to decide which element a run of
# typing belongs to.
_TEXT_ROLES = frozenset({"entry", "text", "password text"})

# Roles whose text is a reading rather than an input. Checked against a
# non-empty value only: a toolkit that publishes no text for these reports the
# empty string rather than nothing, and "this label is empty" is a check that
# passes against an application that has stopped drawing altogether.
_READING_ROLES = frozenset(
    {"label", "static", "heading", "table cell", "list item", "tree item"}
)

# Roles whose checked state is the thing worth asserting about them.
_CHECKABLE_ROLES = frozenset(
    {"check box", "radio button", "toggle button", "check menu item", "radio menu item"}
)


@dataclass
class NormalizerOptions:
    """Thresholds that decide how raw input is grouped."""

    motion_threshold: int = 8
    """Pixels of motion below which a press/release pair is a click, not a drag."""

    click_interval: float = 0.5
    """Seconds within which a press and release still count as one click."""

    double_click_interval: float = 0.4
    """Seconds within which consecutive clicks merge into one multi-click."""

    text_idle: float = 1.5
    """Seconds of keyboard silence that end a run of typed text."""

    pause_threshold: float = 1.0
    """Seconds of inactivity that become an explicit Pause event."""

    record_motion: bool = False
    """Emit pointer motion in its own right, not only as part of a drag."""

    sensitive: bool = False
    """Treat all typed text as sensitive, whatever the focused element is."""

    check_key: str = "ctrl+F1"
    """Key that records a check on whatever the pointer is over.

    Swallowed like the stop key is, so an application that binds this exact
    combination cannot be recorded pressing it. That is the price of a key
    that works while another application is full screen, which is the only
    time one is needed -- and the reason the default carries a modifier and
    matches exactly, which keeps the neighbouring combinations usable. Empty
    disables checks entirely and the key records normally.
    """


@dataclass
class _Pending:
    """A button held down, and where it went down."""

    button: int
    target: Target
    timestamp: float
    moved: bool = False
    last: tuple[int, int] = (0, 0)


@dataclass
class _Typed:
    """A run of printable characters not yet flushed."""

    text: str = ""
    target: Target | None = None
    timestamp: float = 0.0
    last: float = 0.0
    sensitive: bool = False


@dataclass
class Normalizer:
    """Group raw events into canonical ones."""

    options: NormalizerOptions = field(default_factory=NormalizerOptions)
    resolver: ContextResolver = field(default_factory=NullResolver)
    started: float = 0.0

    _held: dict[int, _Pending] = field(default_factory=dict, init=False)
    _typed: _Typed = field(default_factory=_Typed, init=False)
    _mods: set[str] = field(default_factory=set, init=False)
    _click: Click | None = field(default=None, init=False)
    _click_at: float = field(default=0.0, init=False)
    _clicked: Target | None = field(default=None, init=False)
    _last_input: float | None = field(default=None, init=False)

    def feed(self, raw: RawEvent) -> list[Event]:
        """Consume one raw event, returning whatever became final.

        Usually nothing: a press is not yet a click, a click is not yet known
        to be single rather than double, and typed characters are held until
        something ends the run. Events come out in recorded order regardless,
        because each carries the timestamp it was captured at.
        """
        out: list[Event] = []
        gap = self._gap(raw)
        if gap is not None:
            # Both happened before this gap, so both come out before the Pause
            # does. A gap long enough to be a Pause is always longer than the
            # double-click window, so nothing that could still have merged is
            # being flushed early.
            out.extend(self._flush_click())
            out.extend(self._flush_text())
            out.append(gap)
        elif raw.kind not in ("button_press", "button_release", "motion"):
            out.extend(self._flush_click())
        handler = getattr(self, f"_on_{raw.kind}")
        out.extend(handler(raw))
        self._last_input = raw.timestamp
        return out

    def flush(self) -> list[Event]:
        """Emit anything still buffered. Call once when recording stops."""
        return self._flush_click() + self._flush_text()

    def _flush_click(self) -> list[Event]:
        """Emit the buffered click, now that nothing can merge into it."""
        if self._click is None:
            return []
        click, self._click = self._click, None
        return [click]

    # -- pointer -------------------------------------------------------------

    def _on_motion(self, raw: RawEvent) -> list[Event]:
        """Track motion; emit it only when it is meaningful on its own."""
        for pending in self._held.values():
            if _distance(pending.last, (raw.x, raw.y)) >= self.options.motion_threshold:
                pending.moved = True
            pending.last = (raw.x, raw.y)
        if not self.options.record_motion or self._held:
            return []
        return [
            MouseMove(
                timestamp=self._at(raw),
                target=self._target(raw),
            )
        ]

    def _on_button_press(self, raw: RawEvent) -> list[Event]:
        """Remember the press; a click is only known at release."""
        out: list[Event] = self._flush_text()
        self._held[raw.button] = _Pending(
            button=raw.button,
            target=self._target(raw),
            timestamp=raw.timestamp,
            last=(raw.x, raw.y),
        )
        return out

    def _on_button_release(self, raw: RawEvent) -> list[Event]:
        """Resolve a held button into a click, a drag, or nothing."""
        pending = self._held.pop(raw.button, None)
        if pending is None:
            return []
        held = raw.timestamp - pending.timestamp
        moved = pending.moved or (
            _distance((pending.target.x, pending.target.y), (raw.x, raw.y))
            >= self.options.motion_threshold
        )
        if moved:
            return [
                *self._flush_click(),
                Drag(
                    timestamp=self._at(raw),
                    start=pending.target,
                    end=self._target(raw),
                    button=raw.button,
                ),
            ]
        # A long hold that never moved is still a click; the hold is kept as
        # a note rather than silently discarded.
        note = f"held for {held:.1f}s" if held > self.options.click_interval else ""
        return self._buffer_click(raw, pending, note)

    def _buffer_click(self, raw: RawEvent, pending: _Pending, note: str) -> list[Event]:
        """Buffer a click, merging it into the previous one where that applies.

        Nothing is emitted for the first click of a pair: whether it was a
        single or a double is not known until the double-click window closes,
        so the click waits there and is flushed by whatever comes next.
        """
        target = pending.target
        self._clicked = target
        if self._mergeable(raw, target):
            assert self._click is not None
            self._click.count += 1
            self._click_at = raw.timestamp
            return []
        out = self._flush_click()
        self._click = Click(
            timestamp=self._at(raw),
            target=target,
            button=raw.button,
            note=note,
        )
        self._click_at = raw.timestamp
        return out

    def _mergeable(self, raw: RawEvent, target: Target) -> bool:
        """Whether this click continues the buffered one."""
        if self._click is None or self._click.button != raw.button:
            return False
        if raw.timestamp - self._click_at > self.options.double_click_interval:
            return False
        held = (self._click.target.x, self._click.target.y)
        return _distance(held, (target.x, target.y)) < self.options.motion_threshold

    def _on_scroll(self, raw: RawEvent) -> list[Event]:
        """Emit wheel movement, already in pyguitest's detent convention."""
        out = self._flush_text()
        out.append(
            Scroll(
                timestamp=self._at(raw),
                target=self._target(raw),
                dx=raw.dx,
                dy=raw.dy,
            )
        )
        return out

    # -- keyboard ------------------------------------------------------------

    def _on_key_press(self, raw: RawEvent) -> list[Event]:
        """Record a check, accumulate text, or emit a hotkey or a named key."""
        if chord_matches(self.options.check_key, self._mods, raw.keysym):
            return self._check(raw)
        modifier = MODIFIERS.get(raw.keysym)
        if modifier is not None:
            self._mods.add(modifier)
            return []
        active = self._mods - _TEXT_SAFE
        if active:
            out = self._flush_text()
            # Every modifier actually held, not just the ones that decided
            # this was a hotkey at all. Shift and AltGr are excluded from that
            # decision because they make text rather than commands -- but once
            # Ctrl is down, Ctrl+Shift+S is a different shortcut from Ctrl+S,
            # and dropping the Shift turned a recorded "Save As" into "Save":
            # a script that runs cleanly and does the wrong thing.
            keys = (*sorted(self._mods), raw.keysym)
            out.append(
                HotKey(
                    timestamp=self._at(raw),
                    keys=keys,
                    target=self._target(raw),
                )
            )
            return out
        if raw.text:
            return self._accumulate(raw)
        out = self._flush_text()
        out.append(
            KeyStroke(
                timestamp=self._at(raw),
                key=raw.keysym,
                target=self._target(raw),
            )
        )
        return out

    def _check(self, raw: RawEvent) -> list[Event]:
        """Record a check on whatever the pointer is over, and swallow the key.

        Pending text is flushed first so the check lands *after* the typing it
        was made to verify, which is the shape it is nearly always used in:
        type a value, point at what should have changed, press the key.
        """
        out = self._flush_text()
        observed = self.resolver.inspect(raw.x, raw.y, raw.screen)
        check, expected = check_for(observed)
        element = observed.target.element
        out.append(
            Assertion(
                timestamp=self._at(raw),
                check=check,
                target=observed.target,
                expected=expected,
                sensitive=element is not None and element.role == "password text",
            )
        )
        return out

    def _on_key_release(self, raw: RawEvent) -> list[Event]:
        """Release a held modifier; other releases carry no meaning here."""
        modifier = MODIFIERS.get(raw.keysym)
        if modifier is not None:
            self._mods.discard(modifier)
        return []

    def _accumulate(self, raw: RawEvent) -> list[Event]:
        """Add a printable character to the pending text run."""
        out: list[Event] = []
        idle = raw.timestamp - self._typed.last
        if self._typed.text and idle > self.options.text_idle:
            out = self._flush_text()
        if not self._typed.text:
            target = self._text_target(raw)
            self._typed = _Typed(
                target=target,
                timestamp=self._at(raw),
                sensitive=self.options.sensitive or _is_secret(target),
            )
        self._typed.text += raw.text
        self._typed.last = raw.timestamp
        return out

    def _text_target(self, raw: RawEvent) -> Target:
        """Decide which element a run of typing belongs to.

        Keyboard focus is the principled answer and is asked first: it is what
        the toolkit itself believes the keys are going into, so it is right for
        a field reached by Tab, by an accelerator, or by the application
        focusing it on its own -- none of which the other rules see at all. It
        is also the only rule that works on a toolkit whose hit-testing cannot
        place a widget, which GTK4's cannot.

        The pointer is the worst answer: you click a field to focus it and then
        move the mouse away, so by the time the keys arrive the pointer is
        somewhere else entirely. The last clicked text field is a good one, and
        it stays as the fallback for every desktop that publishes no per-widget
        focus -- GNOME Shell holds FOCUSED on its own toplevel for the whole
        session, so this cannot simply be assumed to work.

        Asked once per run rather than per keystroke: it costs a walk of the
        accessible tree, and the answer that matters is where the text started
        going, not where focus drifted to by the last character.
        """
        focused = self.resolver.focused()
        if focused is not None and _is_text_field(focused.element):
            return focused
        clicked = self._clicked
        if (
            clicked is not None
            and clicked.element is not None
            and clicked.element.role in _TEXT_ROLES
        ):
            return clicked
        return self._target(raw)

    def _flush_text(self) -> list[Event]:
        """Emit the pending text run, if there is one."""
        if not self._typed.text:
            return []
        event = TextInput(
            timestamp=self._typed.timestamp,
            text=self._typed.text,
            target=self._typed.target,
            sensitive=self._typed.sensitive,
        )
        self._typed = _Typed()
        return [event]

    # -- shared --------------------------------------------------------------

    def _gap(self, raw: RawEvent) -> Event | None:
        """Turn a long stretch of inactivity into an explicit Pause.

        Measured against the last *input*, not the last emitted event: clicks
        are buffered for the double-click window, so the emitted-event clock
        runs behind the user and would miss the gap entirely.

        Typing is the awkward case. A gap inside a run of typing is hesitation
        between two characters of one string, not a wait between two actions,
        and it must not become a Pause -- but a gap long enough to *end* the
        run is exactly the "typed a query, waited for results" shape, which is
        one of the most common things there is to synchronize on. So pending
        text suppresses a Pause only while the run would survive it.
        """
        if self._last_input is None:
            return None
        idle = raw.timestamp - self._last_input
        if idle < self.options.pause_threshold:
            return None
        if self._typed.text and idle < self.options.text_idle:
            return None
        return Pause(
            timestamp=self._at(raw) - idle,
            seconds=round(idle, 2),
            origin=Origin.INFERRED,
        )

    def _target(self, raw: RawEvent) -> Target:
        """Resolve what was under the pointer when this event happened."""
        return self.resolver.resolve(raw.x, raw.y, raw.screen)

    def _at(self, raw: RawEvent) -> float:
        """Seconds since the recording started.

        Inter-event delay is deliberately not computed here. `Recording.add`
        derives it from these timestamps, which keeps one source of truth --
        and an editor that reorders events has to recompute it anyway.
        """
        return max(0.0, raw.timestamp - self.started)


def check_for(observed: Observation) -> tuple[str, str | bool | None]:
    """Decide what a check on this point should assert, and against what.

    The ladder runs most specific first, because the value of a generated
    check is exactly how much it would notice. "This checkbox is ticked" and
    "this field reads 'report'" fail when the application misbehaves; "this
    button is showing" mostly does not, and is the floor rather than the aim.

    A reading role -- a label, a cell -- is only asserted on when it has
    something to say. Toolkits report the empty string for text they do not
    publish, and a check that a label is empty would then pass against an
    application that had stopped drawing entirely. An entry is the opposite
    case: empty is a real state there, and clearing a field is a thing worth
    verifying, so the empty string stands.
    """
    element = observed.target.element
    window = observed.target.window
    if element is None or not element.addressable:
        if window is not None and window.addressable:
            return ("window", window.title)
        return ("nothing", None)
    if element.role in _CHECKABLE_ROLES and observed.checked is not None:
        return ("checked", bool(observed.checked))
    if observed.text is not None:
        if element.role in _TEXT_ROLES:
            return ("text", observed.text)
        if observed.text and element.role in _READING_ROLES:
            return ("text", observed.text)
    return ("showing", None)


def _distance(a: tuple[int, int], b: tuple[int, int]) -> float:
    """Straight-line distance between two points."""
    return float(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5)


def _is_text_field(element: ElementRef | None) -> bool:
    """Whether this element is somewhere a run of typing could have gone.

    A name is required as well as a role: an unnamed field cannot be located
    at replay, so preferring it over the clicked one would trade a locator
    that works for one that does not.
    """
    return element is not None and element.role in _TEXT_ROLES and element.addressable


def _is_secret(target: Target) -> bool:
    """Whether the element under the pointer is a password field."""
    return target.element is not None and target.element.role == "password text"
