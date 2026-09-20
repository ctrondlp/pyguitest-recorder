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
    """Emit every pointer position, not only where the pointer rested.

    Off by default because a position costs a window lookup to consume -- see
    `_window_target`, the half a move is rendered from -- and renders a line of
    its own, which under `motion = "teleport"` is a wall of `gui.move_mouse`
    nobody wants to read. It is for the route itself -- a drag, a drawing, a
    gesture, all of which are *made* of their path -- rather than for what the
    pointer reveals: the rests are recorded as hovers either way, so
    `hover_threshold` below is what anything driven by hovering actually needs.

    A drag is where that promise was not kept: its travel used to be dropped
    outright, leaving an event with two ends and a straight line between them,
    which is a different gesture. See `_record_route`.
    """

    hover_threshold: float = 0.3
    """Seconds the pointer must rest somewhere for that to be a hover.

    A hover is an input, not an accident of moving: it opens submenus, shows
    tooltips, and starts the auto-scroll on a long menu. None of that is a
    click, so without this a recording of it replays as a pointer that
    teleports straight to a coordinate which is only valid *because* of the
    hover that never happened. Found live on MATE: the Applications menu's
    categories open their submenu on hover, so the recorded click on 'Text
    Editor' at (226, 368) landed on bare desktop -- the submenu containing
    it was never opened -- and the replay dismissed the menu instead.

    Above the incidental slowing-down that happens while aiming at a target,
    below a deliberate rest. Set to 0 to disable hover detection entirely.
    """

    sensitive: bool = False
    """Treat all typed text as sensitive, whatever the focused element is."""

    check_key: str = "ctrl+1"
    """Key that records a check on whatever the pointer is over.

    Swallowed like the stop key is, so an application that binds this exact
    combination cannot be recorded pressing it. That is the price of a key
    that works while another application is full screen, which is the only
    time one is needed -- and the reason the default carries a modifier and
    matches exactly, which keeps the neighbouring combinations usable. Empty
    disables checks entirely and the key records normally.

    A bare function key is riskier than it looks here: measured live, a
    laptop's F1 commonly sends a hardware media keysym (`XF86_AudioMute`)
    rather than the literal `F1` this matches against, so the check key
    silently never fires -- every press recorded as an ordinary keystroke
    instead, with nothing to say why. `ctrl+1` avoids that; keyboards do not
    remap plain digits.
    """


@dataclass
class _Pending:
    """A button held down, and where it went down."""

    button: int
    target: Target
    timestamp: float
    moved: bool = False
    last: tuple[int, int] = (0, 0)
    route: list[Target] = field(default_factory=list)
    """Where the pointer went while this button was down, when that is recorded.

    The gesture, not decoration: see `_record_route`.
    """


@dataclass
class _Typed:
    """A run of printable characters not yet flushed."""

    text: str = ""
    target: Target | None = None
    timestamp: float = 0.0
    last: float = 0.0
    sensitive: bool = False


@dataclass
class _Rest:
    """A rest in progress: where it opened, and where the pointer stopped.

    Two positions, because they answer different questions. `where` is the
    sample that opened the still window and stays the anchor the
    `motion_threshold` tolerance is measured from, so a hand trembling over a
    menu row is still one rest rather than a new arrival on every jitter.
    `settled` is the last position still inside that window -- where the pointer
    actually came to a stop -- and it is the one the hover is emitted and
    resolved at. Reporting the rest at the point it *began* puts the pointer up
    to `motion_threshold` from where the interface saw it stop, which on a menu
    row is the width of a row.
    """

    where: tuple[int, int]
    settled: tuple[int, int]
    since: float
    screen: int


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
    _rest: _Rest | None = field(default=None, init=False)
    """The rest the pointer is sitting in, if it is sitting in one.
    Set only by real motion, so a pointer that simply has not moved since a
    click is not mistaken for someone hovering. See `_dwell`."""

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
        if raw.kind in ("button_press", "scroll"):
            # A hover ends where the next pointer action begins, and has to
            # be emitted before it. Keyboard events deliberately do not end
            # one: the pointer is wherever it was left while typing, which
            # is not someone hovering anything.
            out.extend(self._flush_dwell(raw))
        handler = getattr(self, f"_on_{raw.kind}")
        out.extend(handler(raw))
        self._last_input = raw.timestamp
        return out

    def flush(self) -> list[Event]:
        """Emit anything still buffered. Call once when recording stops.

        Includes a hover still in progress when the recording ended: without
        this, a rest with no later event to flush it was dropped outright
        rather than merely reported a little short. `_last_input` -- the
        last raw event actually seen; stop-key presses never reach `feed`,
        so they cannot advance it -- stands in for "now". A rest with
        nothing at all after it reports as zero-length and is correctly
        dropped by `_dwell`'s own threshold check, rather than this
        inventing a duration nothing observed.
        """
        out = self._flush_click()
        if self._rest is not None:
            rest, self._rest = self._rest, None
            until = rest.since if self._last_input is None else self._last_input
            out.extend(self._dwell(rest.settled, rest.since, until, rest.screen))
        out.extend(self._flush_text())
        return out

    def _flush_click(self) -> list[Event]:
        """Emit the buffered click, now that nothing can merge into it."""
        if self._click is None:
            return []
        click, self._click = self._click, None
        return [click]

    # -- pointer -------------------------------------------------------------

    def _on_motion(self, raw: RawEvent) -> list[Event]:
        """Track motion; emit it only when it is meaningful on its own.

        Two ways it can be meaningful. `record_motion` emits every position, for
        a recording that wants the whole path -- and it tracks the rests as
        well, so that somewhere the pointer *rested* is still emitted as the
        hover it was: a recorded path replays a pointer passing *through* the
        points it visited, and a submenu that opened because the pointer stayed
        is not opened by that. Otherwise the pointer is still followed --
        cheaply, position and time only, no resolver -- and only the rests are
        emitted. See `hover_threshold`.

        Both cases resolve a rest fully and a position for its window only:
        naming what was hovered is the entire point of a hover, while the
        element under a move is never read at all -- see `_window_target`.
        """
        for pending in self._held.values():
            if _distance(pending.last, (raw.x, raw.y)) >= self.options.motion_threshold:
                pending.moved = True
            pending.last = (raw.x, raw.y)
        if self._held:
            # Motion under a held button is a drag being dragged, not a rest.
            self._rest = None
            self._record_route(raw)
            return []
        out = self._close_rest(raw)
        if self.options.record_motion:
            out.append(
                MouseMove(timestamp=self._at(raw), target=self._window_target(raw))
            )
        return out

    def _record_route(self, raw: RawEvent) -> None:
        """Keep where a dragged pointer went, which is the gesture itself.

        A drag is press, travel, release -- and the travel used to be thrown
        away, with the event keeping only where it began and where it ended: a
        straight line between them is a *different* gesture from the one made.
        A real recording showed it as a 1.97s hole in the motion stream, which
        is what a drag leaves behind when nothing records its path.

        Only when motion is being recorded in its own right, which is the
        setting whose own description names this case ("the route itself -- a
        drag, a drawing, a gesture"). Each position is resolved for its window
        only: a drag is rendered from coordinates and one origin, and the
        element under the pointer is not what it is drawn from -- see
        `_window_target`.
        """
        if not self.options.record_motion:
            return
        for pending in self._held.values():
            pending.route.append(self._window_target(raw))

    def _close_rest(self, raw: RawEvent) -> list[Event]:
        """End the rest in progress, emitting it if it turned out to be a hover.

        Run on every motion event, whichever way motion is recorded: a rest is
        only known to have been one once the pointer leaves the place it rested
        at, and the event that leaves it is the first event of the next one.
        Emitted ahead of the move that leaves it, because that is the order the
        two happened in.
        """
        here = (raw.x, raw.y)
        if self._rest is None:
            self._rest = _Rest(here, here, raw.timestamp, raw.screen)
            return []
        rest = self._rest
        if _distance(rest.where, here) < self.options.motion_threshold:
            # Still resting. The window stays anchored where it opened and the
            # arrival time deliberately stays the earliest one, so a hand that
            # trembles over a menu entry still reads as one rest rather than
            # restarting the clock on every jitter. Where the pointer *stopped*
            # moves with the pointer, though: that is the position the rest is
            # reported at, and it is where a replay has to park it.
            rest.settled = here
            return []
        self._rest = _Rest(here, here, raw.timestamp, raw.screen)
        return self._dwell(rest.settled, rest.since, raw.timestamp, rest.screen)

    def _flush_dwell(self, raw: RawEvent) -> list[Event]:
        """Emit the hover the pointer was in the middle of, if it was one."""
        if self._rest is None:
            return []
        rest, self._rest = self._rest, None
        if _distance(rest.settled, (raw.x, raw.y)) < self.options.motion_threshold:
            # The action is happening where the pointer already was: whatever
            # renders it will move there itself, so a move saying the same
            # thing would only be noise. Compared against where the pointer
            # came to rest, not where the rest opened -- a click on the point
            # the pointer settled on needs no move, and one on the point the
            # rest happened to open at is a different place.
            return []
        return self._dwell(rest.settled, rest.since, raw.timestamp, rest.screen)

    def _dwell(
        self, settled: tuple[int, int], since: float, until: float, screen: int
    ) -> list[Event]:
        """A MouseMove for a point the pointer rested at long enough to mean it.

        `settled` is where the pointer came to a stop rather than where the rest
        began -- see `_Rest` -- and the element is resolved there, because that
        is the point a replay has to put the pointer on for the interface to see
        what this recording saw. `since` is when the rest began, which is what
        the dwell is measured from, drift included.

        The only place hover detection pays the resolver, and it pays it once
        per hover rather than once per motion event -- which is the whole
        reason the pointer can be followed at all without `record_motion`'s
        cost.
        """
        rested = until - since
        if not self.options.hover_threshold or rested < self.options.hover_threshold:
            return []
        # A rest is asked the same question as a press, with one difference a
        # resolver may care about: it is not the act that chose anything. See
        # `DesktopResolver.resolve_hover`; a resolver with no such distinction
        # simply has no such method.
        resolve = getattr(self.resolver, "resolve_hover", self.resolver.resolve)
        hover = MouseMove(
            timestamp=max(0.0, since - self.started),
            target=resolve(settled[0], settled[1], screen),
            dwell=round(rested, 2),
        )
        # A buffered click always predates this hover: a button press flushes
        # any dwell in progress before it is ever buffered (see feed), so a
        # click still waiting out its double-click window can only be from
        # before the pointer settled here. It comes out ahead of the hover
        # unconditionally -- events carry their own timestamps, but a
        # recording is a list, and emitting the hover first would put the
        # click in the wrong place in it.
        click = self._flush_click()
        # Buffered text is not the same guarantee: feed() deliberately lets a
        # hover sit undisturbed through typing, so a run of text can have
        # started before this hover began (comes out ahead of it, like a
        # click) or during it, after the pointer had already settled (comes
        # out after, since that is when it actually happened). Order by the
        # run's own start time rather than assuming.
        text = self._flush_text()
        if text and text[0].timestamp < hover.timestamp:
            return [*click, *text, hover]
        return [*click, hover, *text]

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
        travelled = _distance((pending.target.x, pending.target.y), (raw.x, raw.y))
        if travelled >= self.options.motion_threshold:
            return [
                *self._flush_click(),
                Drag(
                    timestamp=self._at(raw),
                    start=pending.target,
                    end=self._target(raw),
                    button=raw.button,
                    route=tuple(pending.route),
                ),
            ]
        # Where the button went down and came up is what decides this, not
        # whether the pointer moved in between. A press and release at one
        # point is a click however far the pointer wandered first: a drag
        # whose ends are the same point cannot be replayed as a drag at all,
        # and `gui.drag((x, y), (x, y))` -- which a real recording produced --
        # moves nothing while looking like it does.
        return self._buffer_click(raw, pending, self._hold_note(held, pending))

    def _hold_note(self, held: float, pending: _Pending) -> str:
        """What to say about a click that was not a plain press and release."""
        if pending.moved:
            return "the pointer left and came back before the button was released"
        # A long hold that never moved is still a click; the hold is kept as
        # a note rather than silently discarded.
        return f"held for {held:.1f}s" if held > self.options.click_interval else ""

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
            focused = self.resolver.focused()
            target = self._text_target(raw, focused)
            self._typed = _Typed(
                target=target,
                timestamp=self._at(raw),
                sensitive=self.options.sensitive or self._is_secret(target, focused),
            )
        self._typed.text += raw.text
        self._typed.last = raw.timestamp
        return out

    def _is_secret(self, target: Target, focused: Target | None) -> bool:
        """Whether this run of typing must never reach the generated script.

        Asked of the focused element as well as the one the text is attributed
        to, because those are different questions and only one of them needs a
        *name*. A field has to be nameable to be used as a locator; it does not
        have to be nameable to be a password.

        That distinction leaked a real password. A GTK password entry commonly
        publishes no accessible name -- its label is a sibling -- so the
        focused field was rejected as a locator, the run was attributed to the
        username box it had been tabbed out of, and a network-share password
        went into the script verbatim.
        """
        if _is_secret(target):
            return True
        return focused is not None and _is_secret(focused)

    def _text_target(self, raw: RawEvent, focused: Target | None) -> Target:
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

    def _window_target(self, raw: RawEvent) -> Target:
        """Resolve only the window under the pointer, for a recorded move.

        A move is rendered from its coordinates and the origin of the window it
        was in -- the generator reads `target.window` and `target.relative` for
        one and never the element -- so the element half of `_target` is nothing
        but the accessibility hit-test behind it. Under `record_motion` that is
        once per motion event, hundreds of times a second, which is what made a
        live recording consume slower than the hand making it. Hovers and clicks
        still resolve fully: a rest is one call per rest, and naming an element
        is the reason a click is resolved at all.
        """
        return self.resolver.resolve_window(raw.x, raw.y, raw.screen)

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
