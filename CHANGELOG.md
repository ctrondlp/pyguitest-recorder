# Changelog

Notable changes, newest first. Dates are when the work landed, not when it
was released.

## Unreleased

### Fixed

- **A generated script's pointer actions had no synchronization at all when
  a click resolved to no window at all -- not even the desktop -- leaving
  it to a human's own recorded pause, which was too short to notice on a
  fast, confident click.** Same live KDE reproduction as the resolver retry
  below: dismissing GNOME Text Editor's own in-window "Discard changes?"
  sheet with two clicks close together in time meant `normalize.py`'s
  1-second `pause_threshold` never saw a gap worth turning into an explicit
  wait, so nothing paced the second click against the sheet still
  animating in. `target.window is None` is the one case a generated script
  has nothing else grounding the point in, so it is now also the one case
  that gets a small (0.3s) unconditional settle wait before the pointer
  moves there, regardless of what the recording human happened to notice.

- **A click resolving to no window at all -- not even the active-window
  fallback -- gave up permanently on the first empty answer, even when the
  window was real, already active, and resolvable moments later.**
  Reproduced repeatedly live on KDE: dismissing GNOME Text Editor's own
  in-window "Discard changes?" sheet (not a separate top-level window --
  confirmed live, it never shows up in a window list of its own) with two
  clicks close together in time recorded with zero window attribution
  every single time, forcing both into bare, unanchored absolute
  coordinates that then had to also survive the window opening at the same
  screen position on every future replay. `DesktopResolver._window()` now
  retries the whole hit-test / decoration / active-window chain up to twice
  more, a beat apart, before giving up -- most likely recovering from a
  slow kdotool subprocess round trip racing a fast second click, not a
  genuinely missing window. Bounded and best-effort: a click that truly has
  no window still degrades to a coordinate exactly as before, just after a
  fair chance to resolve first.

### Added

- **Two new, off-by-default flags for quieting warnings on a replay you
  already trust**: `--suppress-keymap-warning`/`suppress_keymap_warning`
  silences pyguitest's own `KeymapWarning` (uinput can type the wrong
  characters entirely silently when record and replay machines have
  different keyboard layouts -- real signal the first time, noise on
  repeat runs of a script you have already checked), and
  `--suppress-atspi-chatter`/`suppress_atspi_chatter` silences GLib's
  "dbind" log domain (native AT-SPI registry chatter that never goes
  through Python's `warnings` module at all -- confirmed unrelated
  background noise on a desktop where the accessibility bus is not scoped
  to one display, not a signal of anything wrong with the script). Both
  emit a one-line comment in the generated script explaining why the
  suppression is there. See `docs/recipes.md`'s "Quieting warnings you
  already know about".

### Changed

- **Emitted helper functions (`_expect_window`, `expect_text`, and the rest)
  now come after `def main():` instead of before it, and in parent -> child
  -> child order rather than alphabetical.** A generated script is about the
  recorded interaction; a reader should meet `main()` first and only reach a
  helper's own definition after something in the body already called it,
  not scroll past every helper to find where the script actually starts.
  When one helper calls another (`expect_text` calling `_expect_element`,
  say), the caller is now listed first and the callee right after it, so a
  dependency reads as an explanation of something already introduced rather
  than a forward reference to a name not yet used.

### Fixed

- **A freshly-opened window's geometry could be read before it finished
  animating into its final position, so a click computed from it could
  land wherever was really there instead -- and the keystrokes meant for
  the new window went there too.** Seen live on KDE: after the Kickoff
  scroll-to-"Text Editor" fixes above got it opening reliably, typing meant
  for GNOME Text Editor landed in the Konsole the replay script itself was
  running in. `_expect_window` found the window (it existed the moment
  KWin fired a window-created event) and immediately read its geometry to
  compute a click point -- but "exists" and "finished sliding into its
  resting position" are not the same moment, and nothing between those two
  calls waited for the second one. `_expect_window` now reads geometry
  again a couple of times, a beat apart, and only returns once two
  consecutive reads agree; a window whose geometry never stops changing
  within that budget is used as last read rather than waited on forever,
  so this costs nothing extra once a window is already settled (the
  overwhelmingly common case) and only spends time here when there is
  something real to wait for.

- **A window identified only by an ambiguous app_id could be silently
  matched to the wrong window instead of the one actually meant, or the
  generated script could time out waiting for it forever.** Seen live on
  KDE: a drag inside KDE's Kickoff menu resolved its end point to a window
  reporting app_id "plasmashell" with no usable title -- but the desktop,
  the panel, and the popup itself all report that same app_id, since one
  process owns all three. Depending on which backend a script replays
  against, matching on app_id alone either found some *other*
  plasmashell-owned window first (wrong window, no error at all) or found
  none, because not every backend fills app_id in yet (`KdotoolBackend`
  never did, see the matching pyguitest fix) and the wait then ran out its
  full timeout. `WindowRef` now records whether another window open at the
  moment this one was identified shared its app_id; when that is true and
  there is no title to fall back on, the window is no longer treated as
  addressable at all, and every locator that depended on it (a wait, an
  activation, a coordinate computed relative to its origin) falls back the
  same way it already does for a window with no identity whatsoever --
  absolute coordinates, or a plain comment explaining why.

- **A recording where every single event turned out unaddressable generated
  a script that did not even parse.** `with pyguitest.connect() as gui:`
  needs at least one indented statement under it; a body made entirely of
  explanatory comments (the fallback above, `_window_lookup`'s own "no
  title and no app id" case, and others) is not one, and the emptiness
  check guarding against exactly this only caught a body with *no lines at
  all*, not one with comments and nothing else. Now checks for at least one
  non-comment line and emits `pass` when there is none, regardless of
  whether comments are present.

- **The terminal the recorder itself was running in was recorded as if it
  were an application under test.** `ignore_pids` exists to keep the
  recorder out of its own recording, but it only ever held `os.getpid()` --
  and the recorder has no window of its own. It runs in a terminal, and it
  is that *terminal's* pid the window carries, so the window the recorder
  was being driven from looked like any other. Seen live on KDE: typing into
  GTK4's Text Editor was attributed to the Konsole the recorder was running
  in, because AT-SPI named a focused text field owned by that terminal and
  the resolver was happy to match it. The generated script then waited for a
  window titled after that terminal's foreground process -- reading
  `pyguitest-recorder` while recording and `python3` while replaying -- so it
  matched nothing and aborted the run on a window the typing never needed.
  The nearest ancestor process that owns a window is now ignored too. It
  stops at the *first* such ancestor rather than ignoring the whole
  ancestry: a desktop-launched chain reaches the session's own shell a few
  steps further up, and ignoring `plasmashell`/`gnome-shell` would blind the
  recorder to the panels and menus it most needs to see. An ancestry with no
  window-owning process in it -- the recorder driven over SSH -- contributes
  nothing, and the process table is read through `ps` rather than `/proc`,
  which FreeBSD does not have without linprocfs.

- **A drag that ended in a different window than it began in made the *next*
  click look like a return to that window, and the `activate_window` emitted
  for it dismissed whatever popup the recording was working in.** Seen live
  on KDE: a press-drag-to-scroll inside the Kickoff menu began inside the
  Xwayland Video Bridge's rectangle -- the capture apparatus, which happened
  to sit under the pointer -- and ended below it, on the desktop. Kickoff
  itself is a native-Wayland popup with no X11 window at all, so hit-testing
  cannot see the thing actually being scrolled and answers with whatever is
  behind it. Since a drag's window is tracked by its *start*, the click that
  followed read as "back on the desktop", and the generated script raised
  the desktop between the scroll and the click that depended on it: the
  scroll replayed at exactly the right screen coordinates, the raise closed
  the menu, and the click landed on bare desktop, so the application it was
  supposed to open never opened. Note that the coordinates were never wrong
  -- both endpoints reconstruct to the recorded screen positions -- which is
  why this looked like a failed scroll rather than a spurious window raise.
  A drag whose ends disagree about the window now changes neither the
  current window nor what counts as visited, since it says nothing
  trustworthy about where the recording *is*. Tracking the end instead would
  only move the same failure to drags that cross the other way; refusing to
  guess holds in both directions, and a genuine drag between two windows
  still gets its raise from the next event that acts in one of them.

- **A recorded window title could carry a trailing space real backends
  never agreed it had, so a matching window still failed to be found at
  replay.** Seen live on KDE: the same window's title came back as
  `"Desktop @ QRect(0,0 1920x1080) "` from whatever backend recording
  used, but neither kdotool nor KWin's own live `window.caption` scripting
  property ever reported that trailing space when checked directly against
  the running desktop -- confirmed both ways. Since `wait_for_window`
  matches literally, that one invisible character was enough to make an
  identical-looking window never match. Titles are now stripped of
  surrounding whitespace where a window's identity is first captured, so a
  whitespace-only difference can no longer decide a match, or register as
  the title having drifted.

- **A generated script assumed `wait_for_window` always finds its window,
  so a timeout crashed with a raw error naming neither the window nor the
  real cause.** `wait_for_window` returning `None` on timeout is documented,
  correct behavior -- but every generated call site handed the result
  straight to `gui.geometry()`/`gui.activate_window()`/etc. with no check,
  so a `None` slipped through to whichever one ran first and crashed several
  frames down inside that backend. Seen live on a KDE replay: the very first
  `wait_for_window(...)` call fed straight into `gui.geometry(...)`, which
  crashed with a bare `TypeError: expected str, bytes or os.PathLike object,
  not NoneType` from deep inside `subprocess.run` -- nothing in that error
  named the window, or said it had never appeared. Window lookups now go
  through a new `_expect_window` helper, mirroring `_expect_element`'s
  existing shape: a clear "expected a window matching ... but none
  appeared" failure, at the point that actually went wrong.

- **`Recorder.run()` could crash outright and lose the entire recording,
  including anything `--save-session` would have kept, on nothing worse
  than a menu closing at an unlucky moment.** `DesktopResolver.focused()`
  read `session.focused()` inside a `try`/`except`, but the very next line
  -- `element.role in WINDOW_ROLES` -- read the live accessibility bus
  again, unguarded. The two are separate reads, not one atomic snapshot:
  an element `focused()` could still hand back is not guaranteed to still
  exist by the time `.role` is asked, and a menu closing (Escape, most
  often) is exactly the kind of moment that un-existing happens in. Seen
  live: stopping a KDE recording with Escape, Escape crashed the whole
  process on a `gi.repository.GLib.GError` ("No such object path") that
  had nothing to do with the recording itself, with no recording saved to
  show for it. `.role` is now read inside the same guard as `focused()`.

- **A click on an element AT-SPI offered no action for at all generated
  `Element.click()`, which fails at replay on every non-GNOME Wayland
  compositor.** *Elements lead, coordinates follow* took "AT-SPI can name
  it" as reason enough to prefer the element path, without checking whether
  AT-SPI actually offered a way to click it. `Element.click()` needs either
  a `click`/`press` AT-SPI action or dogtail's own coordinate click, which
  itself needs GNOME's `gnome-ponytail-daemon` -- absent everywhere else.
  KDE's QML-based Kickoff menu (`Applications > Office`, `Development`, and
  its other categories) exposes neither: confirmed live on a real KDE
  session (`node.actions == {}`) after a recorded script crashed on exactly
  this line, one click after the launcher opened. `ElementRef` now carries
  the AT-SPI actions seen at record time, and the element path is offered
  for a click only when one of them is usable -- otherwise this falls
  straight to a coordinate, the same way it already did for a right click
  `Element.click()` cannot express. `actions=None` (a session saved before
  this field existed) is treated as unknown rather than "confirmed none", so
  `--regenerate` on an old `.json` does not downgrade elements that were
  working fine.

## [0.1.0] — 2026-09-10

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
- A double click at a coordinate emits pyguitest's own `gui.double_click()`,
  added to pyguitest after this project first shipped with no way to express
  one, and released in 0.4.0. On a *named* element it emits a
  `double_click_element` helper instead, since `Element` has no
  `double_click` of its own -- see the fix below for why two `Element.click()`
  calls are not a substitute.
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
  its own role lookups and shipped in **pyguitest 0.5.0** — which is why the
  dependency floor is 0.5.0 rather than the 0.4.0 that first provided
  `ELEMENT_GEOMETRY`. On an older pyguitest, `gui.button("Save")` searches
  `"push button"` and finds nothing on a current desktop, where the longhand
  `element(role="button", ...)` this replaces did work.
- Lint and type settings matched against pyguitest's, taking the stricter of
  the two throughout: `max-complexity` ratcheted from 15 to 11 (nothing under
  `src/` or `tests/` exceeds 8), mypy's `sqlite_cache = false` carried over so
  the type checker runs on a Python built without `_sqlite3`, and `strict`
  kept, which already implies the three flags pyguitest sets by hand.
- **`--doctor` and every recording now say when Chromium or Electron will
  resolve to no element**, rather than leaving it to read as a resolver bug.
  Chromium — and so Electron, VS Code, Slack and the rest — builds no
  accessible tree at all until something announces that an assistive
  technology is running (`org.a11y.Status.IsEnabled`), so element resolution
  can be genuinely working (a GTK or Qt window resolves fine) while a
  Chromium-family window silently finds nothing. `DesktopResolver` now checks
  the same probe pyguitest's own `assistive_technology_enabled()` measures,
  and adds a note — through the same warnings mechanism `--doctor` already
  surfaces and every recording's environment block already carries — only
  when the answer is a measured `False`; an unmeasurable answer (`None`, no
  `gdbus`, no bus) stays silent rather than warning about a gap that may not
  even apply.
- **Trimming a saved recording**, without hand-editing the generated script.
  `--from SECONDS`/`--to SECONDS` drop everything outside a time window;
  `--drop N[,N...]` drops specific events by their 0-based index in the
  original recording — indices are always into the *original* list, so
  `--from 8 --drop 0` unambiguously means "the very first event", not
  whichever event happens to be first among `--from`'s survivors. Works with
  `--regenerate` on a saved `.json`, and equally right after a live
  recording, ahead of both re-analysis and `--save-session` — so a trim is
  applied once and the saved copy keeps it, rather than needing to be
  repeated on every future `--regenerate`. An out-of-range `--drop` index is
  reported and refused rather than silently ignored.

### Fixed

- **A click on a window's own titlebar/close button recorded as a click
  inside a different, unrelated window.** pyguitest deliberately reports
  only a window's client rectangle, never the window manager's own
  decoration -- but `DesktopResolver`'s hit test used plain bounding-box
  containment over that rectangle alone, so a click on a titlebar or its
  close/minimize/shade buttons (which sit outside it) fell straight through
  to whatever *other* window's rect happened to occupy that screen pixel,
  which is nearly always something, since decorations hug a window's edge.
  Found live on Xfce/xfwm4: closing "Application Finder" by its titlebar X
  was recorded as a click deep inside a terminal window sitting behind it --
  confirmed by computing that the recorded coordinates landed within a
  couple pixels of the titlebar region twice, in two independent recordings.
  `_window()` now prefers the active window over a plain hit-test match when
  the point falls just outside the active window's rect (within the new
  `DECORATION_SLACK`, sized from a live `_NET_FRAME_EXTENTS` reading) but the
  plain match found something else -- the active window is almost always the
  one whose chrome was just clicked.

- **`Recorder.start()` leaked the pyguitest session if the capture backend
  failed to start after the session was already open.** The session opens
  before `self._backend.start()` is called, with no try/except around that
  last step -- so RECORD being missing, the second X11 connection being
  refused, or the capture thread failing to start left the session's
  connections open with nothing to close them: the CLI's own
  `except CaptureUnavailable: return 1` never reaches `Recorder.stop()`,
  which only runs in the `finally` around `Recorder.run()` -- a path
  `start()` failing never lets the caller reach. Fixed by wrapping
  `self._backend.start()` and calling `self.stop()` before re-raising,
  mirroring the identical try/except-then-`stop()` pattern
  `X11CaptureBackend.start()` itself already uses one layer down. Found by
  a repo-wide bug audit, not live.

- **A `--header` value containing `"""` corrupted the generated file.** The
  custom header text is spliced directly into the module's own
  triple-quoted docstring with no escaping, so a licence block or a ticket
  reference containing an embedded `"""` (a plausible value someone pastes
  in) closed the docstring early -- the rest of the intended header became
  bare top-level string statements, and the real closing `"""` further down
  reopened an unterminated string that swallowed the remainder of the
  module. `_header()` now escapes an embedded `"""` as `\"""`, which reads
  as a literal `"""` inside the rendered docstring rather than closing it.
  Found by a repo-wide bug audit, not live.

- **Two windows launched independently could still collapse onto one
  generated binding, the same failure class as the app_id-alone bug
  below, just reopened one field over.** `_window_var`'s dedup key was
  `(app_id, title)` -- safe against title drift (the resolver pins a
  window's *first-seen* title and reuses it), but two windows that are
  genuinely different can still be first seen with the identical title:
  two "Open File" dialogs from different processes, or two freshly-opened
  windows of one app with an unnumbered default title. `pid`, which
  already survives on `WindowRef` for exactly this reason, is now part of
  the key too, catching every case where the collision is between
  different processes. It does not catch two windows of the *same*
  process sharing both an app_id and a first-seen title (two dialogs from
  one running instance) -- `WindowRef` deliberately carries no live handle
  to distinguish those (see its own docstring), so they still collapse to
  one binding; see `_window_var`'s docstring for the full reasoning. Found
  by a repo-wide bug audit, not live.

- **Generated window lookups would have started silently failing to match
  any title containing regex metacharacters, once pyguitest is upgraded.**
  `_title_pattern()` used to escape a recorded title before emitting it
  (`"Document (1)"` -> `"Document \(1\)"` in the generated source), because
  `wait_for_window`/`find_window`/`window_element` used to compile a plain
  string as a regex unconditionally. That upstream pyguitest behavior
  itself changed -- found live, validating against a real KDE Plasma 6 /
  KWin session, where `window_element(window.title)` raised
  `WindowNotFound` for GNOME Text Editor's own default title, "New Document
  (Draft) - Text Editor" -- to match a plain string literally, as a
  substring, escaping it internally instead. Once pyguitest carries that
  fix, this generator's own pre-escaping would have doubled up: `re.escape`
  on a string that already contains literal backslashes turns `\(` into
  `\\(`, which matches nothing real. `_title_pattern()` now emits the raw
  title unchanged and lets pyguitest do the one escape; the floor on
  `pyguitest` bumped accordingly (see `pyproject.toml`).

- **Two same-named elements in different containers were indistinguishable
  in generated scripts.** `ElementRef.path` (the chain of named ancestors)
  was recorded but never read by the generator, so a Save button in a Save
  As dialog and one in Preferences both rendered as the identical
  `gui.element(role=..., name="Save")`, matching whichever one pyguitest's
  search happened to find first. The generator now scans the whole
  recording once for (role, name) collisions and scopes each colliding
  element to the nearest ancestor in its own path that is not shared by any
  other member of the group, emitting `within=<ancestor>` (walking outward
  past a shared immediate parent when needed, and skipping any ancestor
  with no name -- a nameless container cannot be looked up either). Where
  no named, unshared ancestor exists anywhere in the path -- two
  identically structured panes, say -- the elements are left unscoped, but
  the script now carries a header warning naming the collision instead of
  silently guessing. Checks (`expect_text`/`expect_checked`/`expect_showing`)
  and sync-inferred waits (`wait_for_element`) get the same scoping as
  clicks -- both render through separate paths (`_expect` and
  `_emit_waitforelement`, not `_element_expr`) that needed the identical
  fix, and now take `within=` too.

- **X11 keyboard capture only ever read Shift, ignoring CapsLock and
  AltGr/group-2 layouts entirely.** `_key()` looked up a keysym at index 0
  or 1 -- group 1, unshifted or shifted -- so a CapsLock-affected letter or
  an AltGr-produced character (group 2, selected by whichever modifier the
  server binds to Mode_switch/ISO_Level3_Shift) recorded whatever group 1
  happened to hold at that keycode instead. `_resolve_keysym` now finds the
  server's actual group-switch modifier once in `start()` (not assumed to
  be Mod5 -- `xmodmap` can bind it to any of Mod1-Mod5, and a layout with
  no third level leaves it unset, which reads as before this fix) and
  treats Lock as a second Shift only for a keysym pair that is actually one
  letter's two cases, matching `XLookupString`'s own interpretation rules.

- **A malformed or hand-edited recording could leak a display connection, or
  fail with a bare `KeyError`/`TypeError` instead of a message naming what
  was wrong.** Three items from the external review batch, all real:
  `X11CaptureBackend.start()` only ran cleanup for the "no RECORD extension"
  case, so any later failure -- the second connection refused,
  `record_create_context` rejected -- leaked whatever had already been
  opened; now the whole setup runs under one try/except that tears down
  everything opened so far. `event_from_dict`/`Recording.from_dict` did no
  validation of their own, so a missing `kind`, an unknown one, or a
  required field left out of a hand-trimmed recording (`--regenerate` exists
  precisely to invite that kind of editing) surfaced as a raw exception from
  wherever the code first touched the bad value; both now raise `ValueError`
  naming the event index and what was wrong, which `main()` already catches
  and reports cleanly. `Recording.save()` wrote directly to the target path,
  so an interrupt mid-write (a crash, a full disk, Ctrl-C) could leave a
  truncated, unparsable file in place of a working recording -- possibly the
  only copy of one that took real effort to make; it now writes to a temp
  file in the same directory and renames it into place, which is atomic on
  the same filesystem.

- **Two windows of one application could silently collapse into one
  generated binding.** `_window_var` keyed a window's variable on `app_id`
  alone, but `app_id` names the *application*, not the window -- two
  terminal windows of one app share an app_id, so clicking in the second
  one generated a click against the first one's `wait_for_window` binding
  instead, with no error anywhere in the pipeline. The key is now
  `(app_id, title)` together. Safe to add title back into the key, unlike
  before app_id existed: the resolver already pins a window's title to what
  it was first seen as and reuses that pinned value for every later
  mention, so two mentions of the *same* window always carry the same
  title here, however many times the real window renamed itself on screen
  -- only two genuinely different windows see different titles. The
  identical `app_id`-alone comparison in `_dragged_its_own_window` (deciding
  whether a drag moved its own window) got the same fix, for a drag that
  starts in one window of an app and ends in a different window of it.
  Found reviewing an external audit's claim that this reproduces; it does,
  and matters more now that X11 and GNOME Shell both fill `app_id`.

- **A recorded AltGr keystroke generated a script that failed at replay,
  not just at recording time.** The normalizer already mapped
  `ISO_Level3_Shift` to an "altgr" modifier and the generator already
  rendered it as `gui.send_keys("&...")`; what neither could fix is that
  pyguitest's own `Xlib.XK` never loaded the keysym group
  `ISO_Level3_Shift` lives in, so every such script raised `ValueError:
  unknown key name 'ISO_Level3_Shift'` the moment it ran, on any layout
  with a group-2 symbol. **Needs the matching pyguitest fix**, which loads
  that keysym group and shipped in **pyguitest 0.6.0** — which is why the
  dependency floor is 0.6.0 rather than the 0.5.0 that first provided the
  role-spelling fix. This was always a pyguitest-side bug, not a recorder
  one; the floor bump is what actually closes it for anyone recording on
  this version.

### Documentation

- **Restructured around recording a test rather than around how the recorder
  was engineered**, following an external review that found the same thing
  across all three of these projects: the depth was there, but a reader met
  the reasoning before the instructions. The README opened with a
  twelve-row verification table and an essay on why Wayland cannot be
  recorded, and the warning that keyboard capture sees *every* application's
  keystrokes sat at line 429 of 490 — well past where someone would have
  started recording.

  Now: a quick start at the top with that privacy warning beside the first
  record command, the element → window-relative → absolute ladder drawn
  before it is discussed, and `--save-session`/`--regenerate` promoted to the
  quick start as "record once, regenerate forever". The README is 374 lines
  shorter and nothing was deleted.

  New `docs/`: `getting-started.md` (doctor through replay), `recipes.md`
  (every flag that matters, by the task it serves), `troubleshooting.md`
  (opening with "why is my script all coordinates?", the question the tool
  actually generates), and an index. The engineering narrative moved intact
  to `docs/developers/architecture.md` (why X11 only, recording versus
  replaying, the element-resolution corroboration rules, focus-based
  targeting) and `docs/developers/status.md` (the verification table, the
  live-capture-check history, the known gaps).

- **`docs/testable-guis.md` reworked against a 40-point line review.**
  Accessibility is now framed as the *foundation* of robust automation rather
  than a synonym for it; a Role / Name / State / Value / Relationships mental
  model is introduced up front instead of being scattered through examples;
  form-field label association (`labelled-by`, `set_mnemonic_widget`,
  `setBuddy`) is covered, which was missing entirely and is the reason a
  visibly-labelled entry can still be unnamed; lists, trees, tables and
  custom widgets get real guidance rather than a paragraph; the app-id
  section now separates native Wayland, X11 and XWayland instead of treating
  the last as obvious; there is a worked before/after showing the same
  interaction recorded against a badly and a well labelled application; and
  `assert_accessible()` now states exactly what it checks *and what it does
  not*, since the name under-promises in one direction and over-promises in
  the other.

  The GTK4 "widgets report size but no position" finding is now qualified by
  the versions it was measured on, with a snippet for measuring your own
  stack — it is the kind of thing that gets fixed upstream without an
  announcement, and stating it as a permanent property of GTK4 would age
  badly.

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
- **A password reached a generated script in clear.** Redaction recognises a
  password field by its `password text` role, and the focus-based targeting
  added earlier required the focused element to be *named* before it would use
  it -- a sound rule for a locator, since an unnamed field cannot be found
  again at replay, and the wrong rule for secrecy. A GTK password entry
  commonly publishes no name at all (its label is a sibling), so the field was
  rejected, the run was attributed to the username box it had been tabbed out
  of, and a real network-share password went in verbatim. Locating and secrecy
  are asked separately now: a field must be nameable to be used as a locator,
  and needs no name to make the run secret.

  Two things around it, because recognition can always fail: a redacted run
  says so at the point of use rather than only in the binding block, and text
  typed somewhere the recording *could not identify at all* now raises a note
  saying it is in the file verbatim and to re-record with `--sensitive`. That
  case cannot be fixed by guessing -- withholding every unidentified run would
  redact most typing on a toolkit whose hit-testing does not work -- but it
  can stop being silent.
- **A drag that moved its own window rendered as a no-op.** Every other point
  in a generated script is window-relative, because that is what survives the
  window being somewhere else -- but a drag on a titlebar moves the window
  with the pointer, so the offset within it barely changes and both endpoints
  collapse. A recording of someone dragging a calculator around produced
  `gui.drag((x + 485, y + 49), (x + 485, y + 49))`. Such a drag is written in
  screen coordinates now, with a comment saying why.
- **A drag whose ends were the same point.** Whether a press and release is a
  drag is decided by where the button went down and came up, not by whether
  the pointer moved in between. Dragging out and coming back produced
  `gui.drag((x, y), (x, y))` -- seen in a real recording -- which moves
  nothing while looking like it does. It records as a click now, noting that
  the pointer wandered.
- **Every multimedia key was nameless.** python-xlib loads only the core
  keysym groups into `XK`, so anything outside them fell through to its hex
  value and generated `gui.send_keys("^({0x1008ff12})")` -- a name `press_key`
  cannot resolve, and one `validate()` cannot catch because it is a string
  argument rather than a method. Found on a laptop whose F-row sends media
  keys unless Fn is held, where Ctrl+F1 is really Ctrl+XF86AudioMute. The
  groups are loaded now, so that records as `XF86_AudioMute`.
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
