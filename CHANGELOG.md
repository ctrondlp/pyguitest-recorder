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
- `docs/testable-guis.md`, for the application developers on the other end of
  a recording that came out as coordinates: what to publish so a control can
  be named, with every claim marked as measured or as taken from toolkit
  documentation. Its app-id advice now says which value that actually is on
  X11 — `WM_CLASS` is a pair and tools read the *class* — since "set the app
  id" is not actionable without that, and its widget-position section carries
  the three-application GTK 4 measurement rather than the single dialog it
  started from.
- The generated script's header carries **the recorder's own version**, not
  only pyguitest's -- the profile says which API the calls were checked
  against, this says which recorder emitted them. `--no-header` drops the
  docstring; `--header TEXT`, or a multi-line `header` in the config file,
  puts your own text above it while keeping the provenance block below.
- **The diagnostic notes moved to the end of the file.** One recording of a
  text editor put forty lines of them above the first import; the header now
  carries a one-line pointer and the notes sit in a comment block after
  `main()`. Long emitted comments are wrapped, too -- a comment is opaque to
  `ruff format`, so it was the one thing in a generated file that could run
  past the line limit.
- A long run of one key collapses into the loop it obviously is. Clearing a
  field with Backspace rendered as twenty consecutive identical lines; the
  recording still holds every tap, so this is a rendering decision and an old
  recording picks it up on `--regenerate`.
- `--record-motion` has a command-line flag at last. It was a config-file key
  only, which made the one setting that records hover-driven menu navigation
  undiscoverable from `--help`.
- **Checks, which are what make a recording a test.** Pressing Ctrl+F1 over
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
- Both bound keys are configurable and take a chord syntax (`Escape`,
  `ctrl+Escape`, `ctrl+shift+F1`), matched exactly so binding one leaves the
  combinations around it to the application. The defaults moved to what
  laptops actually have: **Escape pressed twice** to stop, since many
  keyboards have no Pause key, and **Ctrl+F1** to check, since a bare F9 is a
  screenshot key on some laptops and a bare F1 is help nearly everywhere.
  Stop presses that do not complete a run are passed through and recorded, so
  "press Escape to close the dialog" stays recordable; `--stop-presses 1`
  restores single-press behaviour for a key nothing else wants.
- Window variables and comments are named from the **title**, while window
  *identity* still keys on the app id. The two want different fields: the app
  id is what does not drift, and the title is what a reader recognizes. This
  had no visible effect while X11 reported no app id at all; the moment
  pyguitest started populating it from `WM_CLASS`, a window called "Recorder
  Check" began binding to `zenity` and its comment read "the recording moved
  back to 'zenity'". Also stopped reading a one-dot app id as reverse-DNS,
  which bound `check_app.py` to `py`; two dots are required now, so
  `org.gnome.TextEditor` still shortens and a program name does not.
- **Typed text is attributed by keyboard focus**, not by where the pointer
  happened to be resting. A run of typing asks the toolkit what has focus, so
  a field reached by Tab, by an accelerator, or focused by the application
  itself is named — none of which the pointer sees. It also works where
  hit-testing cannot: GTK4 publishes a size at the origin and no position for
  every widget, so `element_at` returns the frame for essentially every point,
  while focus involves no geometry and is measurably reliable. The live check
  now generates `gui.text_field("Name").set_text("Ada")` where it produced
  `gui.type_text("Ada")` before — the first element this harness has ever
  named on that stack.

  Believed only when corroborated, since focus carries no coordinate to check
  against: a *toplevel* holding focus means the desktop publishes no
  per-widget focus (GNOME Shell holds it session-wide) and reads as no answer;
  the process must own a window on the recorded display, or the answer came
  from another session over the login-scoped accessibility bus; and the window
  is the one the element's accessible ancestry names, not the first the
  process owns — zenity owns both its dialog and a window called "zenity", and
  taking the first generated a stray `wait_for_window("zenity")` for typing
  that went into the dialog. The last clicked text field remains the fallback.
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
- A button recorded as `"button"` now generates `gui.button("Save")` rather
  than the longhand `element()` form. at-spi2 renamed `ATSPI_ROLE_PUSH_BUTTON`
  to `ATSPI_ROLE_BUTTON` without changing its integer, so which spelling a
  recording carries depends on the at-spi2 it was made against — and 2.61.1
  emits only the new one, so the sugar had stopped firing on a current
  desktop. Both names map to the same accessor and the same `Role` constant.

  **This needs the matching pyguitest fix**, which accepts both spellings in
  its own role lookups and is unreleased as of writing. On a pyguitest that
  lacks it, `gui.button("Save")` searches `"push button"` and finds nothing on
  a current desktop, where the longhand `element(role="button", ...)` this
  replaces did work. Move the dependency floor to whichever release carries
  it.
- Lint and type settings matched against pyguitest's, taking the stricter of
  the two throughout: `max-complexity` ratcheted from 15 to 11 (nothing under
  `src/` or `tests/` exceeds 8), mypy's `sqlite_cache = false` carried over so
  the type checker runs on a Python built without `_sqlite3`, and `strict`
  kept, which already implies the three flags pyguitest sets by hand.

### Found by the first recording of a real application

Everything above was found on a private X server driving a test dialog. The
first recording of somebody's actual desktop -- a file manager, a text editor
and a terminal on GhostBSD -- found three more in one pass.

- **A window that renamed itself became four windows.** The editor retitled
  itself on every keystroke, and window identity was "app id, or failing that
  the title", so each new title read as a new window: four `wait_for_window`
  calls for one window, three matching nothing at replay. `wait_for_window`
  answers None rather than raising, so the script then failed several lines
  further down on `None.pid`. Identity now follows the live window handle --
  which is what pyguitest's own `Window` compares on, precisely because
  titles move -- and the *first* title seen is the one every later mention
  reuses. That is also the right one to match on: replay starts from the same
  state and follows the same sequence, so the title the window had when the
  recording first touched it is the title the script will find.
- **The same element was waited for twice.** Two pauses in front of one
  element emitted two identical `wait_for_element` calls: the window rule
  marked its window seen, the element rule never marked its element.
- **A long title truncated mid-word**, giving variables like
  `hello_there_draft_text_edito`, which reads as a typo in every line it
  appears in. Cut at a word boundary now.
- A hyphen in a title is no longer escaped into the matching pattern. It is
  only special inside a character class, and titles are full of them.

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
- **A combination lost its Shift.** Shift and AltGr are excluded from the
  test for "is this a hotkey at all", because they make text rather than
  commands -- but they were then excluded from the combination itself, so
  Ctrl+Shift+S recorded as `send_keys("^(s)")`. A recorded "Save As" replayed
  as "Save": a script that runs cleanly and does the wrong thing, which is the
  failure this project cares about most. Every held modifier is kept now.
- Hotkey keysyms were truncated to three letters, so `Return` became `{RET}`,
  which is not the abbreviation.
- A wait *after typing* was never detected at all, which is the shape
  synchronization inference most wants to see.
- Nothing ever emitted `WindowActivate`, so a recording spanning two windows
  replayed into whichever one happened to have focus.
- A window that moved mid-recording kept one geometry read, putting every
  later coordinate out by however far it had travelled.
