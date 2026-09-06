# Changelog

Notable changes, newest first. Dates are when the work landed, not when it was
released; nothing has been released yet.

## Unreleased

The first working version. Records X11 input, resolves what each action
pointed at, works out what every pause was waiting for, and writes pyguitest
source.

### Added

- Canonical event model with a JSON format, so a recording outlives the
  script generated from it and can be re-rendered against a later pyguitest.
- XRecord capture backend, the only interface on this stack that lets one
  client observe another's input.
- Window and element resolution: a click becomes `gui.button("Save").click()`
  where AT-SPI can name what was under it, a window-relative coordinate where
  it cannot, and an absolute one only as a last resort.
- Element resolution now goes through pyguitest's own
  `Capability.ELEMENT_GEOMETRY` — `element_at()`, `extents()` and
  `Element.pid`, added upstream for this and released in pyguitest 0.4.0 —
  instead of calling
  `Atspi.Component` through `gi` directly. The recorder opens one session
  composing `x11` (windows, scoped to the recorded display) with `atspi`
  (elements), and the capability doubles as the version check: a pyguitest
  without it degrades the recording to coordinates rather than failing.
- A double click at a coordinate now emits pyguitest's own `gui.double_click()`,
  added to pyguitest after this project first shipped with no way to express
  one, and released in 0.4.0. A double click on a *named* element still emits
  two `Element.click()` calls, since `Element` itself has no `double_click`.
- **Checks, which are what make a recording a test.** Pressing F9 over
  something records a check on it, and the generated script verifies it: a
  checkbox against its state, a field or a label against what it read, any
  other named element against being on screen, and a window against being
  open. Without them a generated script asserts nothing and passes as long as
  it does not raise — clicking Save and never looking at the result passes
  against a build where saving silently fails. The `expect_` functions are
  written into the generated file rather than imported, so its only dependency
  is still pyguitest; they name the element, the wanted value and the actual
  one when they fail, and retry until their timeout, because a check recorded
  the instant an action returns races an application that has not redrawn.
  Password fields are redacted as typed input is, and a check that resolved to
  nothing is reported in the script's header rather than dropped.
- **Synchronization inference.** Each recorded pause is asked what it was
  waiting for and answered from what the events themselves saw: a window that
  had never been seen becomes `wait_for_window`, a new element in an open
  window becomes `wait_for_element`, and an unexplained pause becomes
  `wait_for_idle` on the window's process. A sleep is what is left when
  nothing better could be established, and the script says so.
- Generated scripts open with `gui.require(...)` naming what they use, and are
  validated before being offered: every `gui.<method>`, `Capability` and `Role`
  is checked against the installed pyguitest, and any name the module reads
  without binding is reported.
- `scripts/live-capture-check.py`, which records a real application on a
  private X server and runs the whole pipeline over the result. CI runs it.
- Lint and type settings matched against pyguitest's, taking the stricter of
  the two throughout: `max-complexity` ratcheted from 15 to 11 (nothing under
  `src/` or `tests/` exceeds 8), mypy's `sqlite_cache = false` carried over so
  the type checker runs on a Python built without `_sqlite3`, and `strict`
  kept, which already implies the three flags pyguitest sets by hand.

### Fixed before anyone could hit them

Everything below was found by running the thing rather than by reading it, and
each had passed the test suite, the type checker and the generated-source
validator first.

- The XRecord context was created on one display connection and enabled on the
  other, which the server refuses with `BadContext`. **Capture had never
  worked.**
- Window context came from the compositor rather than the recorded display, so
  a recording made on a private X server resolved its clicks onto whatever
  application happened to be in front on the real desktop.
- The accessibility bus is scoped to the login session, not to a display, so
  elements leaked across sessions the same way. Four corroboration checks now
  stand between an AT-SPI answer and a generated locator.
- A GTK4 dialog reports every widget at the origin, so hit-testing returned
  the same label for every point in the window; that is now detected instead
  of believed.
- `wait_for_idle` emitted a pid read off a variable nothing defined.
- A window titled `gui` generated `gui = gui.wait_for_window(...)`.
- Window titles went into `wait_for_window`'s **regex** unescaped.
- Hotkey keysyms were truncated to three letters, so `Return` became `{RET}`,
  which is not the abbreviation.
- A wait *after typing* was never detected at all, which is the shape
  synchronization inference most wants to see.
- Nothing ever emitted `WindowActivate`, so a recording spanning two windows
  replayed into whichever one happened to have focus.
- A window that moved mid-recording kept one geometry read, putting every
  later coordinate out by however far it had travelled.
