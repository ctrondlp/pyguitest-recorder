# Changelog

Notable changes, newest first. Dates are when the work landed, not when it
was released.

## [Unreleased]

### Fixed

- **A click on a GTK3 tree row's disclosure triangle recorded as a bare
  coordinate, so the generated script never opened the row.** The triangle that
  opens -- and closes -- a row is a *single* click in the strip just left of the
  row's cell, measured live at about 30px left of the cell's x and one 18px
  indent further in for each level below the top. That point is outside the
  rectangle AT-SPI reports for the row, so the hit test answers the *view*
  (`tree table 'Folders'`) and the only locator left was a coordinate: the one
  that stops working the moment the window moves. Found by a record -> generate
  -> replay -> re-record round trip against a two-level GTK3 tree on a private
  Xvfb, where the replayed script folded the tree back while the recording it
  was made from held no name for the click at all.

  The resolver now recovers the row from the view's own children -- an
  expandable row whose vertical band holds the point and whose rectangle begins
  to its right -- and the generator renders that click as the
  `expand()`/`collapse()` toggle, decided at replay, that a double click on an
  expandable row already got. The band is read half-open, so the line where one
  row ends and the next begins belongs to the lower row rather than to both. A
  click on the row's *cell* is deliberately untouched: that one still selects,
  which is what clicking the body of a GTK3 tree row does. On the same round
  trip `Documents` and `Reports` now record as named `table cell` elements with
  a toggle apiece, where every triangle click used to be a `tree table
  'Folders'` coordinate.

  A *double* click on the triangle is the one gesture that replays as nothing:
  it toggles the row twice and leaves it as it was, so the pair -- the analyzer
  merges two presses at one point into a single two-count click, without asking
  what either of them landed on -- gets a comment where a line would have been.
  Rendered as the toggle a double click on an expandable row gets, it left the
  row open where the recording left it shut: one toggle, in the wrong
  direction, for a gesture that asked for no change at all. A pair whose row
  cannot be named (`--locators absolute`) keeps the coordinate `double_click()`
  the fallback already renders, which replays both toggles by position rather
  than dropping the gesture.

  One thing the toggle does not reproduce, measured on that round trip and left
  as it is: the triangle click that opened a row *also selected* it, and
  `expand()`/`collapse()` do not, so a recording that ends on a row's triangle
  can come back with that row unselected. Selecting a row is not part of opening
  it, and a `select()` written on the off chance would act where the recording
  did not ask for it.

## [0.8.2] — 2026-10-01

### Changed

- **The pyguitest floor is now 0.16.1.** The release fixes two calls a generated
  script makes, both silent: on Linux `Element.click()` on a GTK switch or
  toggle button did nothing (it offers AT-SPI no `click` or `press` action), and
  the `input` backend's pointer landed one pixel short of every absolute
  position, so a recorded coordinate click missed by one. Neither raises on an
  older install, which is what the floor is for. The generator `PROFILE` stays
  `pyguitest-0.16`: it names the minor series the emitted calls were checked
  against, and no emitted call changed.

- **The package metadata gains the macOS environment facet and a link to
  pyguitest.** The `Environment :: MacOS X` classifier joins the X11 and Win32
  ones -- without it a macOS reader filtering PyPI by environment did not find a
  recorder that has a macOS backend -- and `[project.urls]` links pyguitest,
  whose scripts this writes. Every classifier was checked against the published
  trove list, and the sdist and wheel pass `twine check --strict`.

### Fixed

- **Shift+Left was recorded as Left, so a recorded selection replayed as cursor
  movement.** Shift and AltGr are not counted as command modifiers, because with a
  character key they make text -- `Shift+a` is the letter A. But with a key that has
  *no* text (Left, Home, End, Tab, Return, the function keys) there is nothing for
  them to be making, and `Shift+Left` is a different command from `Left`: it
  selects. The Shift was silently dropped, a three-character selection became three
  cursor moves, and the copy and paste after it acted on nothing -- found when a
  replayed text-editing session ended with different text from the original,
  against a live desktop. A held modifier now makes a non-text key a hotkey, so the
  script says `send_keys("+({Left})")`. `Shift+letter` is still typed text, and
  Ctrl+Shift combinations are unchanged.

- **Every press of Delete was recorded as typing the letter "ÿ".** The X11
  backend decides whether a keysym is text by asking python-xlib's
  `keysym_to_string`, which answers `chr(0xFF)` for Delete (0xFFFF). The recording
  then held a `text_input` of `ÿ` where it should hold a `Delete` keystroke, and the
  script replayed it as `type_text("ÿ")` -- which a scancode backend such as uinput
  refuses, so the whole replay stopped there. Found replaying a recorded
  text-editing session against a live GNOME desktop. The 0xFF00-0xFFFF block is X11's
  miscellaneous keys (Delete, Home, the arrows, F1-F35, the keypad, the modifiers)
  and holds no character, so it is now excluded outright; Latin-1 and the direct
  Unicode block are untouched. A test sweeps the whole block.

- **Typed text was attributed to the wrong field when the recorder fell behind, and
  a password could land in the script in clear.** Keyboard focus is read when a
  typed run is *consumed*, and the read walks the accessible tree -- about 400ms on
  a busy desktop. Behind a backlog it answers where focus is *now*, which after a
  Tab is the next field. Found validating recorded-and-replayed sessions on a live
  GNOME Wayland desktop: a driver typed Name, Tab, Email, Tab, Password at machine
  speed; the recorder fell 1.1s behind, the email was attributed to the password
  field and withheld, the Tab after it to the first field, and the replay script
  asked for a `SECRET_1` that was an email address. The wrong name is the mild
  half. A stale read naming an earlier, ordinary field while a password is being
  typed writes that password into the script verbatim -- the same failure
  `_note_unidentified_text` records happening for real in a network-share dialog.
  Past `NormalizerOptions.focus_max_lag` (0.25s, measured per run at the moment it
  starts) a run now claims no field and is withheld as sensitive, with a note on
  the event and in the script saying the recorder was behind rather than that it
  was a password field, and it no longer pays for the focus read it cannot trust.
  Typing at a human pace keeps up and is unchanged; what is still open is making
  the read cheap or event-driven, which would remove the cause rather than the
  consequence.

- **Late typing is no longer withheld when nothing queued could have moved focus.**
  The rule above failed closed on every run that started past 0.25s behind, and
  resolving a click or a Tab costs 0.2-0.8s, so typing straight after either was
  nearly always withheld: a live recording of a plain form came out with the email
  address as `SECRET_1`, and which fields were masked depended on how fast the
  machine was. Focus moves because input moves it, so a late read is still right
  when nothing that moves focus is waiting. The X11 backend now shows its queue
  (`pending()`), and a late run is trusted when nothing in it is a click, a key
  with no text (Tab, Return, Escape, the arrows) or a key held with Ctrl, Alt or
  Super. It still fails closed when the queue holds any of those, when the backend
  cannot show its queue (Windows and macOS, for now), and past
  `NormalizerOptions.focus_stale_lag` (3s), because input is the only thing the
  queue can speak for -- an application moving its own focus leaves no trace in it.

- **`docs/troubleshooting.md` explains why plain text can come out as `SECRET_n`.**
  The password-field reason was documented in the generated script's comment only;
  the new lag reason above would otherwise look like a bug.

- **`kill`, a dropped SSH session or a closed terminal threw away the whole
  recording, and a signal that did end one could hang it.** Ctrl-C ends a
  recording cleanly -- the tail is collected and the script written -- but
  Python's default for SIGTERM and SIGHUP is to end the process on the spot, so a
  CI timeout, a `kill`, or a terminal window closed mid-recording lost everything
  captured. Found by working through an external audit's abrupt-termination
  questions against the code. Those two signals, and Ctrl-Break on Windows, are
  now handled like Ctrl-C while a recording runs, and the previous handlers are
  restored afterwards. SIGINT is taken over too where it is *ignored*: a shell
  starts a background job (`pyguitest-recorder -o x.py &`, `nohup`, a CI step)
  with SIGINT ignored, Python honours that by installing no Ctrl-C handler, and
  `kill -INT` -- the documented way to stop a recording -- did nothing at all,
  silently. Found when a validation harness started under `nohup` spawned recorders
  that never stopped, each one idle with `SigIgn` showing SIGINT.

  **On X11 the signal no longer raises anything.** The first version raised
  `KeyboardInterrupt` from the handler, as Ctrl-C does, and a recording made while
  the recorder was behind live input then sometimes never stopped: 2 of 9 stops
  hung for over 90 seconds, with faulthandler showing the main thread spinning
  inside python-xlib. An exception delivered asynchronously lands wherever the
  thread happens to be, which for a recorder working through a backlog is almost
  always inside an X request, and python-xlib keeps per-connection bookkeeping it
  does not protect: the interrupted connection then believes another thread is
  still receiving, and the tail collection that reuses it waits for ever. It
  reproduces with no recorder -- interrupt `intern_atom` with an async exception,
  call it again, and three runs in three hang -- so a plain Ctrl-C had the same
  hazard. Now, where `Recorder.cooperative_interrupts` is true (X11), the first
  signal stops capture from a helper thread exactly as the stop key does, `run`
  finishes the backlog capture had already delivered and returns, and a second
  signal gives up on what is left (`Recorder.abandon_tail`), checked between
  events. SIGINT is replaced there whether or not it was ignored. Windows and
  macOS keep the exception, since the hazard is proven only for python-xlib.

  The same exception cost the event it landed in. Twelve `Tab` taps recorded as
  eleven in 7 of 9 stress trials, always exactly one, at a different position each
  time, and never for letters or arrows (cheap to consume) -- the key in flight
  when the signal arrived, aborted mid-event while the recorder was working out
  which window and element it had gone to. The raw capture showed all twelve
  delivered, and replaying that raw stream through the normalizer offline gave
  twelve. It was also why the second recording of a replay kept coming out one
  event short: a dropped drag, a dropped Tab. After the change, 18 of 18 trials
  recorded all twelve and none hung.
  `docs/troubleshooting.md` gains a section on what ends a recording cleanly and
  what cannot.

- **A recording with no element context said so only after it ended.** When the
  pyguitest session could not open, the note -- "clicks will carry bare
  coordinates" -- was printed in the summary after the stop key, by which point
  the whole interaction had been performed for a script of screen coordinates.
  Notes the session already holds are now printed as recording starts.

### Tests

- **Generated scripts are executed, not only read.** `tests/test_replay.py` runs
  the generator's output through `exec` against a real `pyguitest.Session` over
  a fake backend that records what reached it. It pins that an element
  recording reaches the named elements and no coordinates; that a window-relative
  click follows the window to where it now is (recorded at 100,50, replayed at
  300,200, lands at 410,280); that a hotkey leaves no modifier held; and that
  quotes, backslashes, tabs, newlines and non-ASCII text in an element name or
  in typed text arrive unchanged. `validate()` already checked names and keywords
  against the installed pyguitest; nothing ran the result.

## [0.8.1] — 2026-09-30

### Changed

- **`PROFILE` and the `pyguitest` floor both move to 0.16.0, and this is a step
  an older install gets wrong rather than one it merely lacks.** The header a
  generated script carries is a claim about the API its calls were checked
  against, so it follows the pyguitest this generator was last validated against,
  and the floor follows it there — the convention 0.15.0 followed when nothing
  emitted here needed it. 0.16.0 is different: five calls in a generated script
  changed behaviour there. A Windows `gui.tap_key(...)` now carries the scan code
  and extended bit the layout gives the key, where it went out with neither and
  `Home`, an arrow or Right Ctrl arrived as a key no keyboard sends;
  `expect_checked` reads a selected radio button as checked, where it answered
  None and a generated check failed against a button that was plainly selected;
  macOS `double_click()` counts its clicks, where AppKit read two clicks of count
  1 and double-clicking a word selected nothing; `gui.drag(...)` posts drag
  events where it posted moves, which the window server does not deliver as a
  drag at all; and `find_window`/`wait_for_window` name the topmost window, where
  they named the one behind. `window_element()`, whose 4.24s per call is what
  0.15.1's step was about, is 0.11s. An install left at 0.15.1 therefore has to
  move up to run *this* recorder even though most of what it writes would still
  run there — the whole cost, stated rather than discovered. Landing it wants
  pyguitest 0.16.0 *published*, not merely tagged: CI here installs pyguitest
  from PyPI like every other job, so until that release exists the requirement
  cannot resolve at all.

### Fixed

- **A Windows recording begun with Caps Lock on captured every letter in the
  wrong case.** The capture backend keeps its own copy of the keyboard state for
  `ToUnicodeEx` -- the low-level hook is documented as the one place the system's
  copy cannot be trusted -- and that copy started with every toggle off. Caps Lock,
  Num Lock and Scroll Lock are latched, so the key-downs seen during a recording
  say nothing about where they were when it began. Found live on Windows 11: `a`
  pressed with Caps Lock on typed `A` in the application and was recorded as
  `'a'`, so the replay typed the wrong case. The three toggles are now read with
  `GetKeyState` once, when capture starts, and the same keystroke records as `'A'`.

- **Windows of one application were one window to the analyzer.** Sync inference
  keyed a window by its app id alone, which every window of one application
  shares -- every TextEdit window on macOS, where the app id is the owning
  process's name, and every window of one `WM_CLASS` on X11. A second window of the
  same application therefore looked already seen: a pause spent waiting for it to
  open got no `wait_for_window` with the time it actually took, and moving back and
  forth between two of them got no raise, so a replayed click could land on
  whichever was in front. The generator already bound windows by app id, title and
  pid for exactly this reason; the analyzer now uses the same identity.

- **A recording file could put code into the script generated from it.** A
  recording is user-editable JSON -- `--regenerate` exists so one can be trimmed
  by hand, or received from someone else and re-rendered -- and three routes from
  it reached the generated source as code rather than as data. A `Comment`'s text
  or any event's `note` holding a line break ended its `# ...` comment and started
  a line of code. A numeric field that was not a number -- a click's `button`, a
  coordinate, a scroll amount -- was written into the call unquoted, so
  `"button": "1); import os; os.system(...); ("` became a statement. And the
  environment fields shown in the header docstring were inserted unescaped, so
  three double quotes in one closed the docstring early. All three were
  demonstrated with an `os.system` call in the generated file. Loading now checks
  every field against the type it declares (an integer field still takes a finite
  float, which some platforms record coordinates as), comments and notes are
  flattened to one line, and every header line is escaped.
- **A header naming a Windows path produced a script that did not parse, and a
  header holding a run of quotes could still put code into one.** The header
  docstring escaped only a run of exactly three quotes, so `C:\Users\...` in the
  `header` setting put a `\U` into the source -- the start of a unicode escape to
  Python, and a `SyntaxError` -- and four quotes in a recording's own environment
  field (a `desktop`, say) came out as one escaped quote followed by three bare
  ones, which closes the docstring just as surely as three did; five more
  reopened a short string, and the line of code between the two runs landed in the
  generated module as code. Backslashes are escaped, and then every double quote.
- **A recording whose click count was written as `1.0` failed to generate a
  script.** JSON has one number type, so a file that has been through another tool
  can hold `1.0` where the recorder wrote `1`, and the generator repeats the click
  once per count -- `TypeError: can't multiply sequence by non-int`, naming neither
  the field nor the event and landing outside the `ValueError` `--regenerate`
  promises. A whole float is now the integer it says, and a fractional one is
  refused by name. Coordinates keep their floats, which is the different case the
  type check was widened for: they are formatted into the script as numbers rather
  than counted with.
- **On Windows, a menu item chosen by an injected click was recorded as whatever
  was behind the menu.** The recorder names a click when it *consumes* the press,
  and choosing an item closes the menu, so by then the point answered for the
  control underneath: a synthetic click on `Actions` and then on `Do Thing`
  recorded the second as `page tab 'General'`, and the replay never chose the item.
  A person's click is saved by the tenth of a second the button is held; one
  injected by another tool -- recording a replay, which is exactly how the live
  check reads a replay back -- is not. The popup memory that already covered this
  on Linux was switched off on Windows on the reasoning that UI Automation
  hit-tests popups itself, which is only true while the popup is still open. It
  now runs there too, for the shape UI Automation actually publishes (an
  expandable `menu item` on the menu bar, and a `menu` of the same name parented
  by the *window*, whose items are destroyed when it closes -- so they are kept as
  a snapshot). Measured on Windows 11: the same injected clicks now record as
  `menu item 'Do Thing'`.
- **A Windows submenu choice was named for whatever was underneath it.** An entry
  that opens a submenu is an ordinary `menu item`, not a menu owner, so the press
  that opened the submenu spent the remembered popup *without* remembering the one
  it opened -- and the press that chose from the submenu was consumed after that
  had closed too, with nothing left to answer it from. The same misattribution the
  bullet above fixes, one level down, on an item that is a bare coordinate in the
  generated script the moment the window moves. The entry is recognised from the
  `expandable` the snapshot now keeps -- before anything live is read, since over
  UI Automation a read is a round trip -- and the submenu it opened is remembered
  once the popup it came from has been spent.
- **A check recorded on an item in a remembered Windows menu kept no state.** The
  layout kept for a closed popup is a snapshot, and a snapshot carries what naming
  an item needs rather than what reading one gives: no text, no checked state. A
  check made while the menu was still open therefore raised on the first attribute
  read, and the recorder's own rule -- an unreadable state is no state -- dropped
  it to "this is showing", with the live item sitting right there. The live item is
  looked up again for the state and paired with the snapshot by role and name.
- **Touchpad scrolling never reached a Windows recording.** A precision touchpad
  reports a two-finger scroll as a stream of wheel deltas well under one detent
  each, and every report under a whole detent was dropped on its own -- so only a
  mouse wheel's notches were recorded, and a touchpad scroll left nothing behind.
  Fractions are now summed per axis and each whole detent the sum crosses is
  recorded, the remainder carried; a change of direction or a half-second pause
  starts the sum again, so a leftover fraction cannot become a detent nobody
  scrolled. Measured live: six 40-unit reports -- two detents' worth -- recorded as
  two scrolls, where they had recorded as nothing. A button press ends the sum too,
  and that one is not an edge case: the reports either side of a click are close
  enough together to look like a single gesture, so half a detent from before the
  click completed a detent with the report after it, and the scroll was recorded at
  the *later* coordinates -- a point the person had not scrolled at.
- **The test suite now passes on a Mac.** Four tests exercised a Linux or Windows
  rule without pinning the platform, so on a Mac host the code correctly took the
  macOS branch and the tests failed: two about skipping the containment checks on
  a scaled screen (a Mac never reads its scale as a unit mismatch), and two about
  falling back from a composed session (a Mac recording asks for one backend, so
  there is nothing composed to fall back from). They pin the platform they are
  about now; the code is unchanged. Run on macOS 26.7: 941 passed, 0 failed.
- **`max_timeout` now says it has no "no cap" value.** `max_waypoints` takes 0 for
  "uncapped" and `max_timeout` refuses 0 and infinity -- correctly, since the cap is
  what a `timeout=` is clamped to -- but nothing said so, and the two sit next to
  each other. The setting's docstring and `config.example.toml` both do now.

## [0.8.0] — 2026-09-28

### Added

- **`max_timeout` and `timeout_factor` settings, with `--max-timeout` and
  `--timeout-factor` flags.** The analyzer's cap and multiplier on an inferred
  wait were reachable only by hand-editing a generated file, one script and one
  wait at a time -- there was no way to change the rule generation-wide for a
  suite that consistently needs more headroom (CI hardware slower across the
  board) or less (a wait that should fail fast rather than sit out a
  five-minute guess). Both are ordinary `Settings` fields now, threaded through
  `cli._sync_options` and `cli._generator_options` the same way `default_timeout`
  already was, so the generated header's `Timeouts:` line reads whichever
  values were actually applied rather than the defaults. Raising `timeout_factor`
  above 1 reopens the one thing the rounding change below closed -- a wait's two numbers no longer
  agreeing -- so the header now states which rule is in force: "rounded up" at
  the default, "`Nx` the wait the recording observed, rounded up" away from it,
  rather than a fixed sentence that would otherwise misdescribe numbers a raised
  factor no longer matches. Both are rejected at construction if set to
  anything `math.ceil()` in the analyzer cannot use -- `inf` and `nan`, which a
  plain `type=float` CLI flag accepts as readily as an ordinary number, and
  zero or a negative value, which produced a non-positive `timeout=` or
  silently collapsed every inferred wait to the floor with no error at all.
  Checked wherever a value can arrive from, config file or command line,
  before a capture backend ever opens -- and named rather than replaced with
  the default, since a caller's mistake should be reported, not hidden.
- **Every threshold setting under `[analyzer]` now says what it does.**
  `motion_threshold`, `click_interval`, `double_click_interval`, `text_idle`
  and `pause_threshold` carried no docstring at all -- `hover_threshold` and
  the stop/check-key settings beside them did, so these five were the only
  ones in that section a reader had to go find the analyzer code to
  understand. Each now names the exact behavior it gates (click-vs-drag and
  double-click-merge distance for `motion_threshold`; the "held for N.Ns"
  comment threshold for `click_interval`; the merge window for
  `double_click_interval`; the split-into-two-runs gap for `text_idle`; the
  noise-vs-wait cutoff for `pause_threshold`).
- **A settings-time conflict is now rejected rather than silently misbehaving,
  in four more specific ways, on top of the `timeout_factor`/`max_timeout`
  check above.** `Settings.__post_init__` now also refuses `default_timeout`
  the same way (it reaches the same arithmetic and was never checked), a
  `max_timeout` set below `default_timeout` (every inferred wait would
  silently clamp to the cap regardless of what was observed, defeating the
  floor with no error), `motion = "recorded"` or `"verbatim"` without
  `record_motion` (nothing was ever captured to render as a route, so every
  move renders as a plain teleport with no sign the setting did anything), a
  `check_key` that is, chord for chord, the same as `stop_key` (the stop key
  is consumed before normalization ever sees it, so pressing `check_key` in
  that case can never reliably record a check -- it either breaks the stop
  sequence, or completes it and never arrives), and a non-empty `header`
  paired with `include_header = False` (the generator only ever reads
  `header` from inside the block that flag turns off, so a licence or ticket
  number set there was silently never written to any generated script -- the
  exact opposite of what `header`'s own docstring promises).

### Changed

- **Timeouts in a generated script now read as the seconds they are.** A wait is no
  longer multiplied on the way out -- `SyncOptions.factor` drops from 3 to 1 -- and is
  instead rounded up to the next whole second, so a 3.8-second pause becomes
  `timeout=10` (the floor, which is what a short wait always got) and a 12.4-second one
  becomes `timeout=13`. Nothing sits between the number in the call and the comment
  above it any more, which is the point: the two used to disagree on purpose, and a
  reader taking that at face value had found a bug that was not there. The 0.7.0 entry
  below documents the 3x as the policy of the release before this one; the header line,
  the README, `docs/getting-started.md` and `docs/troubleshooting.md` all describe the
  rule that exists now. What the 3x bought was blanket tolerance for a replay machine
  slower than the one that recorded, and it bought it silently -- which is also the only
  reason a timeout was ever unreadable. That tolerance is now a timeout that surfaces,
  and the floor is where the remaining headroom lives: `min_timeout` is ten seconds for
  the hesitation a person leaves in a recording, not for machine speed. The `Timeouts:`
  line a generated file's header carries now reads `seconds; the wait the recording
  observed, rounded up (floor 10s, cap 300s)` -- unless the analyzer was run under a
  custom `SyncOptions`, which that line has never reflected and still does not: a caller
  who raises `factor` gets a header claiming a rounding its numbers no longer have.
- **The ceiling on a single wait moves from two minutes to five.** `max_timeout` is the
  cap the analyzer puts on any one wait it writes, applied after the floor: a 25-second
  pause is `timeout=25`, and a ten-minute one is cut back to 300. Two minutes was the
  wrong number to cut back to -- a desktop under load, a window drawing slowly because
  the machine running the suite is the machine being recorded, is a wait that would have
  passed had the cap not overridden it. With the multiplier gone there is no headroom
  left for the cap to override, so it is purely a limit on a pause that was genuinely
  that long in the recording. It is close to free: a `wait_*` returns the moment its
  condition holds, so a ceiling is only ever spent by a wait that was going to expire
  anyway. The one rule it lengthens with no observed gap behind it is `idle`, whose
  `wait_for_idle` is a guess -- a wrong inference now takes five minutes to fail instead
  of two, and `--no-idle-inference` remains the switch for callers who would rather not
  pay it.
- **Element and idle waits state the cadence they check at.** `wait_for_element` now
  carries `interval=0.5` and `wait_for_idle` `interval=0.2`, pyguitest's own defaults
  written into the file rather than left implicit, so the cadence the numbers beside
  them were tuned against is visible where those numbers are and cannot move under a
  later pyguitest release. Window waits take none, because `Session.expect_window` has
  no such parameter: whether the wait it makes is answered by an event feed or by a poll
  is pyguitest's decision, and it makes it on two things at once. The feed exists on five
  backends -- GNOME Shell, KWin, niri, sway and Windows -- and two of the three platforms
  this recorder captures on are not among them: an `AXObserver` on macOS is a later phase,
  and no X11 session has a `WINDOW_EVENTS` backend at all, so a window wait polls at
  pyguitest's own default on both. An `app_id` wait polls on *every* backend, feed or no
  feed, and that is the form this generator writes on XWayland. An earlier draft of this
  entry said the feed answered window waits on "a backend offering `WINDOW_EVENTS`" and
  left it there, which reads as the rule rather than as the exception it is -- the same
  wrong sentence had also been written into `_WAIT_INTERVAL`'s own docstring, and the two
  are corrected together.

### Fixed

- **The README's links were dead on PyPI, and the sdist carried none of the pages they
  name.** The package page is where most people meet this tool, and every `docs/...`
  cross-reference in its README was relative. PyPI serves that one rendered file and no
  repository files beside it, so each one was a 404 there while working exactly as intended
  in a checkout -- which is why it went unnoticed. They are absolute URLs now,
  `[project.urls]` gained the `documentation` and `changelog` entries pyguitest already
  declared (two routes the PyPI sidebar was missing), and this package gained the
  `MANIFEST.in` it had never had at all: setuptools infers README, LICENSE,
  `pyproject.toml` and `tests/` and infers nothing else, so `docs/`,
  `config.example.toml` and `scripts/` existed in the repository and in no sdist, which
  a distro packager finds out by running this suite out of one. `tests/test_docs.py` now
  holds all three: the README may carry no relative file link, every absolute link into
  this repository must name something that exists, and every page these tests read has to
  be covered by `MANIFEST.in`.

- **`--help` now names the three modes, one of which is not a flag.** Recording is what
  happens when no flag is given, and `--doctor` and `--regenerate` are flags rather than
  subcommands, so the listing offered forty options and never stated what the tool does by
  default: the one thing a reader opens `--help` for was the one thing it did not answer.
  An epilog says it, and `tests/test_cli.py` holds the three names to the parser.

- **`docs/getting-started.md` taught one platform's install and said nothing about the
  only permission that stops a macOS recording.** Its quickstart install line was
  Linux-only, so a Windows or macOS reader was handed a command with the wrong extras on
  the first line of the page; all three are shown now, and the replay line names the
  interpreter each platform has rather than assuming `python3` is on every `PATH`. The page
  also had nothing to say about the grant, which is the one thing that beats every package
  on macOS: it is Accessibility, TCC composes `kTCCServiceListenEvent` from it rather than
  keeping a row of its own, so **Input Monitoring says "No Items" on a machine where
  recording works** -- measured on macOS 26, and now written down beside `--doctor`,
  including the fact that a grant applies to processes started after it was made.

- **Unsupported keyword arguments in generated scripts are now reported as
  `INVALID`.** `validate()` held every `gui.*` call to the installed
  `Session`'s method names and stopped there -- so a call to a method that *does* exist,
  carrying an argument it no longer takes, went out unchallenged. The name is right, the
  file compiles, and the failure waits for a replay machine to reach that line: `interval=`
  on a wait that dropped the parameter, or a `within=` spelled for a factory that never had
  one. It now reads each emitted keyword against the installed signature as well, which is
  the same check one release later and the quiet half of an API moving --
  `tests/test_generator.py` writes `cadence=1.0` into a `wait_for_element` call and expects
  the complaint, and holds `_WAIT_INTERVAL`/`_IDLE_INTERVAL` to the defaults of the two
  calls they are written into, since both of those sentences were prose until now. The check
  covers `gui.<factory>(...).<method>(...)` as well as `gui.<method>(...)` -- an Element
  method call missing its own signature check would have been the same quiet failure one
  level down -- and a `*args` parameter's name (`require(*capabilities)`) is no longer
  accepted as a keyword, since Python refuses one regardless of whether the name matches.
  The keywords are read off `Session` and `Element` themselves rather than off a list, so no
  part of this can fall behind the library it is checking.
- **The header's `cap` is now the ceiling the analyzer applied, rather than a literal.**
  The `Timeouts:` line stated `cap 300s` as a string while the floor beside it was derived
  from the options, so raising `SyncOptions.max_timeout` -- the number the analyzer cuts a
  long wait back with -- left every generated file advertising a ceiling its own timeouts
  were no longer held to, and the test that appeared to hold the two together compared the
  literal against the dataclass default and could only pass. The cap travels the way the
  floor does now: `cli._generator_options` asks the analyzer's own options for it, and
  `tests/test_generator.py` and `tests/test_cli.py` hold the generator's default and the
  CLI's wiring to `SyncOptions` respectively.

- **`element_context = False` silently defeated `locators = "element"` (the
  default) with no explanation anywhere in the recording.** Turning off
  element resolution at capture time makes every event's `element` field
  `None`, so a generator asking for named locators has nothing left to
  prefer and every click and text entry falls back to a bare coordinate --
  correct, but previously undiagnosed: the header's own note mechanism
  already covered a resolver that *failed* to find elements, and said
  nothing when a user had turned it off on purpose but forgotten the
  generator was still asking for names. `Recorder._open_session` now adds an
  "element context off" note whenever this combination occurs, the same way
  it already does for `window_context`.

- **`window_context = False` was a silent no-op on macOS whenever
  `element_context` stayed on (the default) -- confirmed live, every click
  still carried a window.** `DesktopResolver._window()` looked up the window
  under a point unconditionally; on Windows and Linux a session composed
  without the window half simply cannot answer that query, so the setting
  worked there by accident of session composition rather than by being
  checked. macOS composes one backend for both halves, and that backend
  answers window queries regardless of `window_context` -- so with element
  resolution left on, nothing changed. A new `DesktopResolver.resolve_windows`
  field (wired from `Settings.window_context`) now gates the one place
  `_window()` is called from, on every platform, so the setting is honest
  everywhere rather than correct by coincidence on two of three.

## [0.7.0] — 2026-09-27

### Changed

- **`Development Status :: 4 - Beta`, up from `3 - Alpha`.** The classifier had
  carried its own comment withholding the promotion on coverage -- every piece
  working end to end, the note said, was not the same as the coverage `4 - Beta`
  claims -- and that note is retired with the change. What the row claims is what the
  0.6.0 entry below describes: a capture backend on each of Linux, Windows and macOS,
  and the resolver and `SysListView32` click fixes those live runs landed against.
  pyguitest's own list has carried `4 - Beta` for the same reason.
- **The keyword list was missing two names the metadata around it already uses.**
  `xwayland` joins `x11` and `macos` — an XWayland session is exactly what this
  records, and the description names it — and `bsd` joins `freebsd`, which the
  classifiers list as a supported platform.
- **`PROFILE` and the `pyguitest` floor both move to 0.15.0.** The header a generated
  script carries is a claim about the API its calls were checked against, so it
  follows the pyguitest this generator was last validated against; pyguitest's 0.15.0
  is that release. The floor follows it there, which is one step past what the version
  before this one argued: nothing this generator emits needs 0.15.0 -- the macOS
  resolver still reads through the same Accessibility backend and translates its key
  names through the same `macquartz` vocabulary -- and a script generated here still
  replays under 0.14.0. The floor is held to the release the output was verified
  against rather than the oldest that might work, because that is the only version the
  check behind it can speak for, and an install left at 0.14.0 therefore has to move up
  to run *this* recorder even though the scripts it writes would still run there. That
  is the whole cost, stated rather than discovered. Landing it wants pyguitest 0.15.0
  *published*, not merely tagged: CI here installs pyguitest from PyPI like every other
  job, so until that release exists the requirement cannot resolve at all and the
  profile test compares the header against 0.14.x, red for a reason that has nothing to
  do with this change.
- **A generated script now says what its timeouts are in.** Every file opens with
  a `Timeouts:` line — `seconds; 3x the wait the recording observed (floor 10s,
  cap 120s)` — because the two numbers a wait carries disagree on purpose: the
  comment above it reports the pause the recording actually took ("the recording
  waited 3.8s here"), and the call beside it allows three times that. Read
  together with nothing to explain them, that pair looks like a bug, and the
  README's own example was read exactly that way — which is what turned this up.
  Both pages that quote the example now say which number is which, and the
  header line's three numbers are held to the scaling `SyncOptions` actually
  applies, so the prose cannot drift from the code under a green suite.

## [0.6.0] — 2026-09-26

### Added

- **`macos`: capture on a Mac through a `CGEventTap`.** The third capture backend,
  and the one ADR 004 in the pyguitest checkout orders after its Accessibility read
  path: a listen-only tap on the HID tap, whose callback is translated into the same
  `RawEvent`s XRecord and the Windows hooks produce. Pointer, buttons, drags, scroll,
  keys and modifiers are all covered, keys are named with X11 keysyms (`Return`,
  `Super_L`, `bracketleft`) so the rest of the pipeline -- `stopkey`, `normalize` and
  the generator's `send_keys` strings -- needs no Mac-specific spellings, and scroll
  passes `kCGScrollWheelEventDeltaAxis1`/`2` straight through so a recorded scroll
  replays with the same sign through `Session.scroll(dy=)`.
- **The permission question is asked, not inferred.** `unavailable_reason()` creates
  and releases a probe tap, because that is the whole answer: measured on macOS 26.7,
  `CGEventTapCreate` returns a live tap on a machine whose only grant is Accessibility
  and `NULL` without one, TCC composing `kTCCServiceListenEvent` from Accessibility
  rather than filing a row of its own. The sentence a reader gets names that pane.
- **`backend = "auto"` on a Mac means the tap**, not `xrecord`, for the reason it means
  `win32` on Windows rather than an X server that happens to be running: XQuartz is an
  X server, and recording its clients would be a fraction of the desktop with no error
  to show for it. `backend = "macos"` is also accepted by name.
- **A `macos` extra**, pointing at `pyguitest[macos]` rather than naming PyObjC's
  distributions a second time, so the requirement cannot drift from the library's.

### Changed

- **The `pyguitest` floor moves to 0.14.0** — the release whose Accessibility backend
  the macOS resolver reads windows and elements through, and whose `macquartz` key
  vocabulary the tap's names are derived from. `PROFILE` moves with it: the header's
  `Profile: pyguitest-0.14` is what tells a reader which API a generated script was
  written for, and what `--regenerate` re-renders against. The reasoning is in
  `pyproject.toml` beside the earlier bumps.
- The package description, keywords and `--display` help name macOS, which had been
  X11 and Windows only since the win32 backend landed.

### Fixed

- **A tap the window server switches off was ignored.** `kCGEventTapDisabledByTimeout`
  and `kCGEventTapDisabledByUserInput` are not mask bits and are not in `_TAPPED`, so the
  callback handed them to `_translate`, which answered `None` -- and a disabled tap is
  not called again, so a recording that hit one would have gone on capturing nothing at
  all, with no error and nothing in the log. `_re_enable` answers both now, and only
  while the run is live. Measured over five live runs on a granted macOS 26.7 Mac: what
  actually arrives is the user-input one, once, after `stop()` had disabled the tap
  itself, with every event already delivered -- 48 of 48 posted events, including across
  a three-second gap between two bursts. The guard is what that measurement buys, since
  putting the tap back during teardown would fight the teardown it is part of.

- **Every special key recorded the window server's control code as typed text.** A
  `Recorder` run against a live Mac desktop -- the first, see below -- captured `Left` as
  `'\x1c'`, `Return` as `'\r'`, the forward delete as `'\x7f'`, the function keys as
  `'\x10'`, and AppKit's private-use range (`U+E000`-`U+F8FF`) for the keys it has no
  character for. `CGEventKeyboardGetUnicodeString` answers for *every* key event, and a
  control code is not text: carried through, `'\x1c'` reaches the generator as typed text,
  and a script that types `'\x1c'` types nothing like a Left arrow. Measured over the
  113-event control sequence: `_typed` now refuses anything below space, the forward-delete
  code and the private-use range, so every special key carries `text=''` and only real
  characters carry text -- the rule `x11`'s `_printable` already applied to X11's own
  control codes.

- **A generated script could not replay on the Mac it was recorded on.** The header's
  `pyguitest.connect()` had no backend list, and on a Mac the default ordering answers with
  the Accessibility read path: `macquartz` is `opt_in` and sits second in the band order on
  purpose, so the script failed its own `require(Capability.KEY_EVENT)` before posting a
  single key. `_connect(state)` names `["macquartz", "macos"]` on macOS and leaves every
  other platform on the bare call, in the generated header and in `--regenerate` both.

- **A Mac recording had no window or element context at all.** The context the
  resolver asks pyguitest for is composed per platform -- `win32` + `uia` on Windows,
  `x11` + `atspi` everywhere else -- and macOS was still on "everywhere else" after the
  capture backend landed. A Mac has neither an X server nor an accessibility bus, so
  `connect` raised, the resolver was built with no session, and every click in a
  recording came out `window: null, element: null` behind a note reading *"no pyguitest
  session on $DISPLAY (backend 'atspi' cannot drive this session); clicks will carry
  bare coordinates"* -- a display and a bus a Mac does not have, on top of losing the
  names the tool exists to produce. A Mac now asks for **`macos`**, the one backend
  there that answers both halves (`WINDOW_AT_POINT` and `ELEMENT_GEOMETRY` out of the
  same session), so `Recorder._context_backends` names one backend on that platform
  instead of a pair. Measured on the same granted macOS 26.7 Mac, before and after:
  `probe_context` went from `windows: False elements: False` with that note to
  `windows: True elements: True` with **no notes at all**, and a live record → replay
  round trip now generates `gui.expect_window('e2e_doc.txt', timeout=10)` with
  window-relative coordinates, where the pre-fix run's script had a bare `gui.click()`
  and the comment "nothing resolved a window for this point when it was recorded".
- **Notes a Mac reads are written for a Mac.** Six of them named machinery only Linux
  has, and every one was unreachable before the fix above and would have been visible
  afterwards: "on the recorded display" is now "in this recording" (three call sites,
  including the stacked-window sentence, which no longer says "on this display"),
  `NO_AT_BRIDGE` and `org.a11y.Status.IsEnabled` are Linux-only notes behind a new
  `DesktopResolver._atspi`, the session type the wording reads is built as `DARWIN`,
  and `--no-element-context`'s help no longer promises AT-SPI. The one note that had
  to be *added*: a session with no `ELEMENT_GEOMETRY` on a Mac names the Accessibility
  grant, because without it the window half still answers and a reader would otherwise
  go looking for a backend that is working perfectly.
- **A stray `DISPLAY` no longer reaches a Mac recording's header.** Installing XQuartz
  puts `DISPLAY` into the login environment, so a Mac recording its own desktop would
  have carried a display it recorded nothing from -- the mirror image of the Windows
  bug the same code already fixed for Xming and VcXsrv. `xrecord` on a Mac keeps it,
  for the reason it does on Windows: that recording really is of an X display.
- **`Screen.scale` on macOS no longer switches the geometry corroboration off.** The
  scale is DPI/96 wherever it is reported, and on a Mac that is a property of the
  panel rather than a statement about coordinates -- the live machine's display answers
  **0.75**. AX extents, `CGWindowList` and the event tap all report points there, so
  the containment checks that reject a toolkit's nonsense rectangles now run on a Mac
  instead of being skipped as uncomparable. `_any_screen_scaled` says so, and
  `test_a_scaled_screen_on_a_mac_is_not_a_unit_mismatch` pins it against the X11
  behaviour it is exempted from.
- **A Windows hook Windows removed behind the recording's back is noticed now, and the
  recording says so.** `WH_KEYBOARD_LL`/`WH_MOUSE_LL` are dropped silently when a
  callback misses `LowLevelHooksTimeout` (300ms by default), and that was the one
  failure this backend could only *document*: the hook stops being called, and the
  callback -- the only part of the process the platform speaks to -- cannot notice its
  own absence. The session can. `GetLastInputInfo` is maintained for idle detection
  whether or not a hook exists, so `Win32CaptureBackend._unseen_input_seconds` compares
  "time since the system last saw input" with "time since a callback last ran", and the
  difference between those two ages *is* the input that went unrecorded. A heartbeat
  asks once a second and records the answer as `hook_lost_seconds`, which the recorder
  turns into a note on the recording (`_hook_lost_note`) naming the mechanism, the size
  of the gap and what to try next -- instead of a script with a hole in it reading as
  complete. It says *may* be missing rather than naming a cause, because input on a
  desktop these hooks cannot see (a UAC prompt, the lock screen, a dropped RDP session)
  advances the same clock in the same way. Proved live by
  `scripts/win32-hook-health-check.py`: on a real Windows 11 desktop the control run saw
  the hooks fire and reported nothing, and 2.7s after the mouse hook was removed behind
  the backend's back the heartbeat said so. Periodic re-installation is still not
  attempted -- whether re-installing restores a silently-removed hook is unmeasured, and
  this heartbeat is what would verify it -- so the state is *reported* rather than
  guessed at.
- **`scripts/win32-live-capture-check.py` was cutting the end off its own recording, and
  blaming the generator for it.** The drive's whole sequence takes about 92 seconds --
  fixed settling sleeps plus pyguitest's -- and the watchdog that exists to stop a *hung*
  drive was set to 90, so it fired during a healthy run. The probe's last click never
  reached the hook, and the run reported the consequence three steps later: *"the generated
  script does not name role=Role.TREE_ITEM, name=\"Q1\""*, which reads as a resolver or
  generator defect and is neither. The backstop is 180 seconds now, it prints why it fired,
  and a run it fires in says so in its own list of problems. The same run also had nowhere
  to keep the session JSON -- `round_trip` writes one to a temporary directory and deletes
  it -- so there was no raw stream to compare the finished recording against, which is the
  only thing that tells "the hook never saw it" apart from "the analyzer dropped it". A new
  `--save-session PATH` writes exactly what the command line's flag writes, raw stream
  included.
- **A `MacosCaptureBackend` stopped right after starting could leave its tap thread
  parked forever.** `start()` created the tap and returned as soon as the capture
  thread had been asked to run; the thread itself only learns its own run loop's
  identity (`CFRunLoopGetCurrent()`) once it actually starts executing, and `stop()`
  reads that same `self._loop`/`self._source` to unwind it. A `stop()` landing in the
  gap between those two moments found `None` for both, so it disabled the tap (input
  stopped arriving) but had no run loop to tell `CFRunLoopRun()` to return -- the
  thread stayed blocked in it, `thread.join(timeout=2.0)` always timed out, and the
  daemon thread leaked for the rest of the process. Found by code review rather than a
  live run: the window is a race, not a reliable repro, and `win32`'s own `_ready.wait()`
  pattern already existed for the identical problem there. `start()` now blocks on the
  same kind of event until the capture thread has recorded its run loop (or failed
  trying to), which is what closes the window rather than narrowing it. Verified live on
  the same granted macOS 26.7 Mac: 30 back-to-back `start()`/`stop()` cycles with no work
  between them -- the exact shape that used to race -- held the worst `stop()` to 7ms and
  left the thread count exactly where it started, and a capture backend built the same
  way still delivered real injected motion events (`macquartz` posted, the tap read them
  back) once running.
- **`hook_lost_seconds` could report a gap from a recording that already ended.** Nothing
  reset it between one `Win32CaptureBackend.start()`/`stop()` cycle and the next on the
  same object, and `_watch_health` returns for good the moment it finds a gap -- so a
  backend reused for a second recording after its hooks were dropped once would carry
  that stale answer forward and never check again, reporting a hole in a recording whose
  hooks never missed a beat. `choose_backend` builds a fresh backend per `Recorder.start()`
  today, so this was not yet reachable through the CLI, but `start()` resets the field now
  rather than leaving a trap for the next caller that reuses one. Verified live on Windows
  11: one backend run through two cycles, mouse hook removed by hand with real injected
  motion in both -- the first correctly reported `hook_lost_seconds = 2.01s`, and the
  second, with the hooks intact, started at `None` and stayed there through two more
  seconds of real motion, instead of carrying the first cycle's answer forward.
- **A recorded click on a `SysListView32` row could come out with no working route on
  Windows, and which of two symptoms it showed was down to luck.** Report-view list
  rows publish two accessibles at the same point: the row itself (role "list item",
  with a real `invoke`/`select`), and the cell's own text, hit-tested more precisely at
  the exact centre, whose action list flip-flops between empty and a bogus "do default
  action" -- the same universal MSAA-bridge fallback pyguitest's `uia._CLICK_IS_AN_EXPAND`
  was written for a combo box. Empty, `click_by_pointer`'s coordinate fallback reached it
  fine; offered-and-refused, the click ladder's loud-failure policy correctly reported it
  as a failure, since a published route refusing is supposed to be reported rather than
  silently downgraded -- so the same recorded line, `gui.element(role=Role.TEXT,
  name="Gamma").click()`, worked or raised `(-2146233079, ...)` depending on which
  accessible the shim felt like answering with that run. `DesktopResolver._live_at` now
  recognises the shape structurally -- a "text" element whose actions are *exactly*
  `["do default action"]`, with a "list item" parent -- and names the row instead,
  before anything is ever invoked, so a recording writes `gui.element(role=Role.LIST_ITEM,
  name="Gamma").click()` and never hands the flaky text leaf to the shim at all.
  Windows-only, and scoped as narrowly as the combo-box fix it parallels: a text element
  that genuinely offers a real action, or has no list-item parent, is left exactly as it
  was. `docs/developers/status.md` has the two live runs that found it and the one that
  closed it.

### Live

Everything above is fake-driven on CI, and the capture path has now been run against a
live window server too: on a granted macOS 26.7 Mac, over SSH, with a child process
posting through `pyguitest.backends.macquartz` -- the tap skips the installing process's
own events, so the source has to be another process. Nine events translated, including
text (`(1, 'a')` for a letter and `(1, '\x7f')` for the forward delete), scroll at the
sign it was posted with, and every one reporting `injected=False`, which is what that
field is on a platform with no injected bit to read. `CGEventKeyboardGetUnicodeString`
is no longer the unverified call, and the three pyguitest-side bugs this found --
fifteen key names the tap emits that `macquartz` could not press, the `Delete`/`delete`
collision, and a replayed modifier being captured as a *release* -- are all fixed in the
pyguitest 0.14.0 this repository's floor named at the time. `docs/developers/status.md`
carries the numbers.

**Then a whole session: recorded, generated, replayed, and recorded again.** Same Mac,
same day, with the same arrangement of a child process posting through `macquartz`. A
113-event sequence covering every special-key family, the modifier chords and every
pointer verb ended by posting the stop chord, which is the live test of stop recognition
through a tap. It normalised to 35 events, the generated script replayed cleanly through
`connect(backend=["macquartz", "macos"])`, and that replay was captured by a second run
through the same tap. The two generated scripts are 98 lines each and differ in 13: one
`gui.wait(0.16)` and its comment, which the replay's own tighter pacing did not warrant,
and five `gui.wait()` values that moved by 0.01-0.02 s. Remove the waits and the two
bodies are identical -- including `gui.drag((454, 204), (554, 234))`, `gui.scroll(dy=-3)`,
`gui.scroll(dy=5)`, `gui.scroll(dx=3)` at the magnitudes and signs that were posted, and
`^(a)`, `%(+({Up}))`, `%(^(+(a)))` for the chords. Both runs normalised to the same 35
events, which is why the scripts match at all: the raw streams differ in four ways a
reader should know about. Layout-independent typing posts every character on keycode 0, so
a tap reads `key_press 'a' 'b'` where the recording has `key_press 'b' 'b'` -- the text is
right in both, and that keysym is `macquartz.type_text`'s documented trade. Modifier
transitions arrive in a different order inside a burst. A chord's own key carries its
letter in the replay, where the recording read `''` because the control code was refused.
And the replay's `drag` interpolates 38 motion events where the sequence posted 7, which
is most of the 113-versus-141 raw difference.

The macOS context was checked on the same granted macOS 26.7 Mac over SSH the capture
work used, with the pre-fix recorder and its own generated script kept for comparison
(`~/before/`): `probe_context`, a `Recorder` run against an open TextEdit window, and
that script replayed and re-recorded through the tap. What the run cost, measured on
that machine: `windows()` 15 ms, `window_at()` 11 ms, `element_at()` 1 ms,
`active_window()` 12 ms and `focused()` **190 ms** at the median -- the last of which
is why a Mac recording that resolves its text targets falls seconds behind live input
and says so in a note. pyguitest's `screens()` was broken on a real Mac the whole time
(see pyguitest's own `0.14.0`), which is what the run's environment block
reported as `capability probe failed: CGGetActiveDisplayCount`; with that fixed, the
recorder's screen block now arrives on macOS too.

The Windows side was re-run against a real Windows 11 desktop on the same day, twice:
`scripts/win32-hook-health-check.py` (new, and the live half of the heartbeat above)
passed -- the control run saw the injected motion with no gap reported, and the gap was
reported 2.7s after the hook was removed by hand -- and
`scripts/win32-live-capture-check.py` recorded the probe window's full control set,
generated a script, validated it clean and found it re-rendered identically, with **no
hook-loss note** in either run, which is the heartbeat not crying wolf under a real
desktop's input. Its replay half stopped on a combo box, reproducibly, and that turned out
to be pyguitest's to fix: `uia.Element.click` had no route for a control whose click *is*
an expand, and pyguitest 0.14.0 gives it one -- see that package's CHANGELOG and
pyguitest's `docs/validation.md`. The check was re-run the same day with that in place, and with the
two bugs above fixed so the capture is whole: **the replay passes the dropdown line** and
carries on through the recorded sequence, stopping further in at a `SysListView32` cell
whose only advertised action is the same MSAA shim that then declares none. That one is
recorded, named and left open rather than papered over -- what to render for an element
whose only action is a shim is this package's decision -- and
`docs/developers/status.md` has the details.

### Not yet done

Human input. Nothing a person did has been through the tap: every event measured here was
posted, which is not the same as arriving from HID, and provenance and modifier-flag
behaviour are exactly where the two could differ. One gap sits beside it, recorded in
`docs/developers/status.md`: a recording made on a Mac still resolves no window or element
for its clicks, so it names bare coordinates where a Linux or Windows recording names
`Window`/`Element`.

## [0.5.0] — 2026-09-24

### Fixed

- **Text typed through a keycode remapped per character could record as
  the wrong characters, or none.** `xdotool type` types anything outside
  the base layout by remapping one spare keycode to each character in
  turn, and a key event was decoded against the server's mapping at the
  moment the pump reached it -- by then often a later character's, or the
  layout restored after the last one. Found reviewing the remap fix below,
  then confirmed on a private Xvfb with the pump held back while
  `xdotool type "héllo你好ßü"` ran: the recording read `hllo`. The two remap
  requests, `ChangeKeyboardMapping` and `SetModifierMapping`, are now
  recorded in the same ordered stream as the key events and applied to a
  mapping kept here once the server's `MappingNotify` confirms them, so
  each press is decoded by the mapping in force when it was made: the same
  run now records `héllo你好ßü` exactly. A remap no core request explains --
  XKB, which is how `setxkbmap` and a desktop's layout switch work -- is
  still read back from the server when its notify arrives. This also drops
  the round trip every key event used to make to look for a remap.

- **Every recorded click fell to a coordinate on Windows -- `Element.click()`
  was never once named for it.** `ElementRef.clickable` checked the
  element's own `actions` for `"click"` or `"press"`, matching
  `AtspiBackend.Element.click()`'s own fallback -- a GTK measurement, with
  no Windows counterpart. UIA's `Element.click()` answers to a different
  vocabulary entirely: `"invoke"`, `"toggle"`, `"do default action"`, never
  `"click"` or `"press"` -- confirmed live against the win32 probe window in
  this repository, where a push button, a checkbox and a page tab each
  publish `invoke` and/or `do default action` and none of them ever
  publishes `click`. So `clickable` read False for literally every element
  on that platform, and a script recorded there named nothing: `Element
  .checkbox("Enable feature")` existed, addressable and correctly
  resolved, and every click on it still rendered as a bare
  `gui.move_mouse(...)` / `gui.click()` pair. Found the way the `selectable`
  gap in the entry below was: record a session against the win32 probe
  window and read the script it generated. `clickable` now recognises both
  vocabularies.

- **The press that opened a drop-down was recorded as a click on an item
  inside it, and the replay crashed.** A press is resolved when it is
  *consumed*, which is after the application has reacted to it -- so a click
  that opens a combo box is asked about at a moment when the popup is already
  on screen and over the point that was clicked. GTK positions a combo's popup
  so the selected item sits on top of the combo and publishes those items as
  children of the combo itself, so `element_at` answers with the item: smaller
  than the combo, equally covering the point, and with no stacking order to
  tell them apart. Found driving a real GTK combo box: the opening press
  recorded as `gui.menu_item("Alpha").click()` -- an item nothing had chosen --
  and the replay raised `ValueError: Attempting to generate a mouse event at
  negative coordinates: (-2147483647, -2147483647)`, because a closed popup's
  items report AT-SPI's unplaced sentinel. It cascaded, too: `_popup_at` only
  recognises a popup whose opening it saw, and this one registered no owner,
  so the press that *did* choose an item went unnamed as well. The press is
  now attributed to the nearest ancestor that is not itself part of a popup --
  the combo box -- and the popup is remembered on the way past, so the choice
  after it resolves normally. The same interaction now records as
  `gui.dropdown("Size").click()` then `gui.menu_item("Gamma").click()`, and
  replays clean.

  Two limits on that, from review of this change rather than a live run: the
  recovered owner has to be a control that opens a popup (a combo box or a
  menu) -- walking up from a menu item the resolver never saw open could
  otherwise land on the menu bar and name *that* for the click -- and it is
  treated as coming from the popup still on screen over it, so the
  same-process toplevel check below does not reject it because the window
  under the point is that popup rather than the combo's frame.

- **`--doctor` said "ready to record, and clicks will be named" on a session
  where no GTK3 or Qt application could be named at all.** `NO_AT_BRIDGE` --
  exported by plenty of shells, containers and IDE terminals to silence GTK's
  "couldn't connect to accessibility bus" warning -- stops GTK3 and Qt
  registering with the bus at startup, so an application launched from such a
  shell to be recorded publishes nothing. The element probe cannot see it: the
  desktop's own components registered when the session started, long before
  the variable was in anyone's environment, so the tree has children and the
  probe passes. Found on a MATE session where `--doctor` gave that verdict and
  a `mate-calc` launched from the same environment never appeared in the tree,
  while pyguitest's own `doctor` named the variable in the same minute. The
  recorder now reports it too -- in `--doctor` and in every recording's notes --
  and the verdict says "a GTK3 or Qt application that inherits NO_AT_BRIDGE
  will have no click named" rather than contradicting the note three lines
  above it. Stated as the risk rather than the outcome, deliberately: the
  recorder reads its own environment as a proxy for the one the application
  was launched in, and a real run that launched the application without the
  variable did name every click.

- **A click resolved to the wrong toplevel's element whenever a process owned
  two overlapping windows.** `_belongs`'s process check treats a matching pid
  as proof an element belongs to the window resolved under the same point --
  true for a single-window application, but one process routinely owns
  several toplevels at once, most commonly a dialog and its parent. Found
  live: mate-calc's Help > About opens a second window almost the same size
  as `Calculator` and at the same origin, both pid-identical, and a click on
  the About dialog's Close button resolved to `Calculator`'s own `=` button
  instead -- the window was named correctly (window lookup uses real X
  stacking order), but AT-SPI's hit test has no concept of one window being
  stacked above another window of the *same* process, so it answered from
  whichever toplevel's tree it reached first. `path` -- the element's own
  ancestry, already captured for locator disambiguation -- carries the
  (role, name) of the toplevel the element is actually nested under, and now
  settles it: a same-pid element whose own path names a different toplevel
  than the one resolved is refused, the same way an element from a different
  process stacked underneath already was, with a note explaining why.
  The toplevel name read from `path` is stripped before it is compared, the
  same way the window title it is compared against already was: backends
  disagree on trailing whitespace, and a trailing space was enough to refuse
  an element from the very window clicked.

- **Typing anything outside the base X keyboard layout was captured as
  nothing at all.** `xdotool type` (and several input methods) type such a
  character by remapping an unused keycode to it via `XChangeKeyboardMapping`
  and pressing that keycode -- and this backend's keysym lookups run against
  a connection whose local keymap cache is built once and never refreshed,
  because nothing here ever read that connection's own event queue for the
  `MappingNotify` the server broadcasts on every remap. Confirmed live:
  recording real Chinese text typed into gedit captured every keystroke as
  keysym `0x0` with no text, and the generated script called
  `gui.tap_key("0x0")` four times instead of typing anything. Two fixes,
  found together: `_resolve_keysym` now drains and applies any pending
  `MappingNotify` before every lookup, and `_printable` now decodes ICCCM's
  direct-Unicode keysym block (`0x01000000 + codepoint`), which
  `Xlib.XK.keysym_to_string` never covered and which is exactly where a
  character with no legacy X keysym of its own -- all of CJK, among others --
  lands. Re-run after both fixes: the same recording produced
  `gui.type_text("你好世界")`, and replaying it into a fresh gedit read back
  the same text through AT-SPI. The same shape of bug as the Windows
  `VK_PACKET` fix below, on X11 instead.
  A remap that moves AltGr itself -- which modifier bit `Mode_switch` or
  `ISO_Level3_Shift` is bound to, or which keycode carries it -- now also
  re-reads the group-switch mask, which was found once at start and would
  otherwise keep reading every AltGr press after such a remap as group 1.

- **A recording made on a private Xvfb reported the developer's own desktop
  environment.** `scoped_environment` strips `WAYLAND_DISPLAY` so a recording
  of X clients is not described as a Wayland session, but never stripped
  `XDG_CURRENT_DESKTOP`/`XDG_SESSION_DESKTOP`/`DESKTOP_SESSION` -- variables
  scoped to the *login session*, not to any one X display. Found live:
  recording through `--display :99` from a MATE session put
  `XDG_CURRENT_DESKTOP=MATE` into the scoped environment unchanged, and the
  generated script's header read `Recorded on: x11 (other, MATE)` for a bare
  Xvfb with no window manager and nothing resembling MATE running on it.
  Those three variables are now dropped whenever `display` names a server
  other than the ambient one, since unlike `DISPLAY` there is no override to
  put a correct value in their place -- a private Xvfb cannot be asked which
  desktop environment it belongs to, because it does not belong to one. Left
  alone recording the desktop you are sitting in front of, where the
  variables are exactly what they claim to be.

### Changed

- **A recorded double click that opened or closed a tree row, a notebook
  page, or another disclosure control now comes out as an `expand()`/
  `collapse()` toggle on the named element, not `double_click()`.** Measured live
  on a GTK3 GtkTreeView: double-clicking a tree row *selects* it -- it does
  not expand, so the script this used to generate replayed clean and did
  nothing to the tree. A double-click on a Windows tree row happens to
  expand it, which is exactly the kind of agreement-by-accident that stops
  holding the moment a script recorded on one platform runs on the other.
  This needed pyguitest's new `Element.expand()`/`collapse()` (see that
  repository's changelog). Replayed for real against the same window after
  recording: the generated `expand()` opened the row and its children where
  the raw double-click recorded to produce it had only selected the row.

  Which of the two is called is decided at replay -- `collapse()` if the
  row is open, `expand()` if not -- rather than fixed from the `expanded`
  the recording captured. That read was meant to be the state before the
  click, and on Windows it often is not: an element is described when its
  press is consumed, and a native tree view has already toggled the row by
  then whenever the consumer is behind the second press. Found live on the
  win32 probe window: a double click on a collapsed row captured
  `"expanded": true`, rendered `collapse()` -- a no-op on replay -- and the
  nested row the recording went on to click never existed. No read made at
  consume time can be sure of being early enough, so the script replays
  the toggle a double click is, which reproduces the recording from the
  state it started in. `locators = "relative"` or `"absolute"` still
  favours coordinates, and a session saved before `expanded` was captured
  renders exactly as it used to.

- **A recorded click on a radio button, a page tab, a list row or a tree item
  now comes out as `select()` on the named element rather than a coordinate.**
  Measured on Windows 11: a coordinate click at a radio button's own reported
  centre did nothing whatever, while
  `gui.element(role=Role.RADIO_BUTTON, name="High").select()` moved it and
  left the other radio group alone, and a nested tree item came back
  `selected` after a replay that had only ever sent clicks. `locators =
  "relative"` or `"absolute"` still says to favour coordinates, and a
  recording made before `ElementRef.selectable` was captured renders exactly
  as it used to.

  `selectable` is read from pyguitest's own `Element.selectable`, not
  inferred from `actions` the way this first shipped internally: the Windows
  11 measurement above found `select` (and `invoke`) in `actions` for both
  controls, and the first version of this checked for that string there. GTK
  does not agree -- a page tab on the probe window in this same repository
  publishes `actions=[]`, no Action interface entries at all, while
  `Element.selectable` still correctly read True and `.select()` worked,
  because GTK exposes the Selection interface directly rather than naming it
  as an action. The `actions`-only check was blind to that on every GTK page
  tab, tree row and list row, silently falling all of them back to a
  coordinate on Linux while the changelog above claimed to have fixed exactly
  that. Confirmed both ways on a live GTK3 window: recording a click on the
  `Tree` page tab produced `gui.element(role=Role.PAGE_TAB,
  name="Tree").select()`, which switched the tab on replay against a fresh
  window -- where it had rendered as a bare `gui.click()` before this.

  Where one of these roles offers both a selection and a click action,
  `select()` now wins. UIA publishes `do default action` on nearly every
  control, so on Windows the radio and the tree item named `click()` instead
  -- and a tree row's default action there is a double click, which toggles
  the row rather than selecting it. Limited to these roles on purpose: an
  AT-SPI menu item is SELECTABLE too, and selecting one would only highlight
  what the recording chose.

- **The pyguitest floor moved to 0.12.0, and the generated header moved with
  it.** A script generated now says `Profile:     pyguitest-0.12`, because
  `PROFILE` follows the API surface `validate()` checks the emitted calls
  against rather than this package's own version. `.expand()`/`.collapse()`
  and reading `Element.selectable` directly, both above, are pyguitest 0.12.0
  additions -- a floor left at 0.11.0 would let a recording generate calls an
  installed pyguitest does not actually have.

### Added

- **`scripts/gtk_probe_window.py`, a GTK window with a real control set, for
  the X11 live checks.** The X11 counterpart of `win32_probe_window.py`, and
  the answer to this file's own long-standing note that the routine live check
  drove two bare GTK windows -- an entry and a label -- so every control a real
  application is mostly *made of* went unexercised on this platform while the
  Windows side had a tab control, a combo box, a list view and a real menu bar
  to aim at. It publishes an entry, a password entry, a button, a check box, a
  radio pair, a combo box, a spin button, a notebook with two pages, a tree
  view, a two-menu menu bar and a button raising a real modal dialog, each
  with an explicit accessible name. It earned its place immediately:
  recording against it found the notebook hit-test bug fixed upstream in
  pyguitest (see that repository's changelog), the drop-down misattribution
  above, and the `NO_AT_BRIDGE` gap in `--doctor`.

## [0.4.0] — 2026-09-23

### Fixed

- **`scripts/live-capture-check.py`'s private bus was not private enough, and
  evicted the developer's accessibility bus.** It re-execs on a session bus of
  its own precisely so the recorded application does not land on the desktop's
  accessibility bus -- but `at-spi-bus-launcher` derives its socket path from
  `XDG_RUNTIME_DIR` rather than from the bus it was started on, so the launcher
  it starts bound `$XDG_RUNTIME_DIR/at-spi/bus`, the path the real session keeps
  its own accessibility socket at, and replaced it. Silently, for every GTK3 and
  Qt application on that desktop, until the next login; and pyguitest processes
  then aborted outright with SIGABRT, because the launcher outlives the bus it
  launched and goes on handing out its address. Found on 2026-09-22 with four
  dead sockets in that directory, one per session this and pyguitest's own
  harness had run. It now creates a private `XDG_RUNTIME_DIR` before the
  re-exec and removes it after the daemons that hold sockets in it are stopped.

- **The pyguitest floor moved to 0.11.0, and the generated header moved with
  it.** A script generated now says `Profile:     pyguitest-0.11`, because
  `PROFILE` follows the API surface `validate()` checks the emitted calls
  against rather than this package's own version — and the test holding the
  two together failed the moment the floor moved ahead of it, which is what it
  is for. An older script is unaffected: the profile is a claim in its own
  header, not something a later run compares a recording against.

- **The Windows install instructions carried a workaround for a pyguitest that
  could not be imported there, and no longer need to.** This package's floor
  was `pyguitest>=0.10.1`, and 0.10.1 predates Windows support: it imports
  `grp` -- a Unix-only standard-library module -- at module scope, so `import
  pyguitest` raised `ModuleNotFoundError` on Windows and every use of it from
  here failed at that import. `--doctor` degraded honestly (`pyguitest: not
  importable`), `docs/troubleshooting.md` carried the two-step install that
  worked anyway (pyguitest from git, then this package), and the CI job here
  installed pyguitest from git for the same reason. The floor is now
  `pyguitest>=0.11.0`, the first release carrying `pyguitest.backends.win32`,
  so the troubleshooting section is gone, the job installs from PyPI like
  every other job, and a Windows install of either package is a plain
  `pip install`.

- **A `stop()` arriving before `run()` marked itself as consuming could still
  close the context out from under it.** The flag that fixed the larger version
  of this was set after `run()`'s setup checks, leaving a gap in which a stop
  saw no run in progress, closed the session, and left the run to work through
  its backlog against a dead one -- losing exactly the window and element
  attribution the flag was added to protect. Admission and teardown now happen
  under one lock, so whichever arrives first owns the context and the other
  leaves it alone. Found by review, not by a failing run: the window is
  narrow, which is what makes it the kind that survives testing.

- **Priming the resolver cost one window list per window, not the one its own
  docstring claimed.** `_identify` asks `_app_id_ambiguous` whether anything
  else shares the app id, and that listed the windows again for each one --
  N+1 lists on a desktop with many windows, all for an answer already in hand.
  The snapshot `prime()` has just taken is passed down instead. It is not a
  cache: it is that moment's list, which is what the ambiguity rule wants.

- **"It came from another session" was usually the wrong explanation on a
  Wayland desktop.** An element belonging to a process that owns no window on
  the recorded display is refused, correctly -- but the note saying why named
  a cause it cannot actually distinguish. The commonest one by far under
  XWayland is a *native Wayland window in the very same session*: invisible to
  XRecord and to the X window list, while publishing to the same accessibility
  bus. Recording gedit on GNOME Shell 51.rc raised it for GNOME Shell's own
  widgets, from the session the recording was being made in. Both the focus
  and the element wording now name both causes instead of asserting the
  rarer one. (The separate "stacked underneath" case, which the window list
  *can* tell apart, is unchanged.)

- **A `stop()` from another thread no longer strips the window and the element
  off every event still being consumed.** Resolution happens when an event is
  consumed, not when it is captured, and `stop()` -- the documented way to end
  a run from another thread, and what every watchdog and check script uses --
  closed the resolver's pyguitest session the moment it was called, while
  `run()` was still working through the backlog. Everything left in it kept its
  coordinates and lost its names: a recording of a real application came out as
  bare `gui.move_mouse(...)`/`gui.click()` with no `expect_window` in it at
  all, validated clean, and said nothing about what it had lost. The lag note
  it did carry describes a different problem -- events *missing* from the end,
  not events present and unnamed. Proved by control on a live GNOME Shell 51.rc
  session: the identical gedit interaction generated `gui.button("Open").click()`
  when the consumer was allowed to catch up first and a bare coordinate when it
  was not. Capture still stops immediately; `run()` now closes the context when
  it has finished with it.

- **A recording made on a Wayland desktop said it was made on X11**, so the
  XWayland warning it exists to carry appeared in neither its header nor its
  notes. `scoped_environment` removes `WAYLAND_DISPLAY` deliberately, so that a
  recording of X clients is not described as a Wayland session -- and that same
  stripped environment was handed to the detection whose answer set
  `environment.xwayland`, which needs exactly that variable to say XWayland.
  `--doctor` reported `xwayland` for the same session in the same minute,
  detecting against the ambient environment instead: two paths disagreeing,
  with the wrong one going into the file, and
  `docs/developers/architecture.md`'s own claim that this "says so in the
  recording and in the generated script's header" false in practice. Now asked
  of the X server being recorded, which advertises an `XWAYLAND` extension --
  the only thing that can tell a session's own XWayland from a private Xvfb
  started on that same session, since both have the two variables set. Verified
  both ways on one machine: `:0` lists it, an Xvfb on `:77` does not. The
  environment heuristic remains the fallback where python-xlib is absent.

- **A window's identity is fixed before recording starts, not the first time an
  event resolves to it.** That first resolve happens at consume time, so a
  consumer that had fallen behind met the window only after it had already
  reacted to the input being consumed: gedit was first seen as `*Untitled
  Document 1 - gedit`, the modified-marker title that does not exist until the
  recorded typing has happened. Nothing had seen it drift, so the generator
  judged the title stable, matched on it, and the replay raised `WindowNotFound`
  on its first line against a freshly opened copy of the same application. The
  resolver is now primed at `start()` with every window already open -- one
  window list, 2ms measured -- so a title that moves during a recording is seen
  to have moved and the generator reaches for the app id instead.

- **`environment.display` recorded the ambient `DISPLAY` rather than the one
  being recorded**, so a recording made with `--display :99` from a desktop
  session put that session's own `:0` in its header -- the one fact the header
  exists to carry. It reads the environment it was handed, which already has
  the right display in it.

### Added

- **A generated script says when its `app_id` match is protocol-specific.** An
  app id recorded through XWayland is the class half of `WM_CLASS`, and the
  same application running as a native Wayland client publishes a different one
  -- gedit is `Gedit` and `gedit`, gnome-calculator is `gnome-calculator` and
  `org.gnome.Calculator`, and neither is derivable from the other. The match is
  exact, so such a script raises `WindowNotFound` on its first line when
  replayed against the native copy: loud, which is the design, and silent about
  why. pyguitest's `expect_window` has always accepted several ids for exactly
  this case, so a recording made through XWayland now carries a comment saying
  so and naming the fix. Confirmed live: editing the line to
  `app_id=("Gedit", "gedit")` made the same recording replay into a native
  Wayland gedit with the document reading back.


- **`motion = "verbatim"`: every recorded position, each with the wait that
  preceded it.** The rest of the axis replays *where* the pointer went and
  drops the clock -- `teleport` and `natural` keep one position per movement
  and no timing at all, and `recorded` keeps the corners and still hands them
  to a shaper with a duration derived from distance. A hover-driven menu is
  decided by the clock: a row opens its submenu once the pointer has stayed on
  it, and pops down once the pointer has been away from it for long enough, and
  neither of those is in the route. Found on a third live take of the MATE demo
  that produced the two menu fixes here: the route was by then exactly right --
  the corners the hand turned on, down the submenu column -- and the replay
  still clicked something the recording never chose, because it crossed the
  menu's rows in 0.6s where the hand had taken 1.7s and rested on none of them.
  `verbatim` is the value that answers that, and it is the longest by a long
  way: the 602 recorded positions above become a script of some twelve hundred
  lines, which is why it is asked for rather than assumed. Gaps are measured
  between the events the script actually replays rather than between the
  recording's own, so a hover's wait is not charged a second time to the
  position that leaves the rest, and the recording's opening gap is not slept
  through. `--verbatim-motion` sets it from the command line.

- **A Windows CI job.** The suite on `windows-latest`, which is what would
  have caught both of the portability bugs below before they reached a user.
  It also asserts the `needs_ruff` tests actually ran, since the `dev` extra
  installs ruff and a skip there would mean thirty-three assertions had
  quietly stopped running on the one platform the job exists to cover.

### Fixed

- **A chord's `keys` could crash -- or silently misread -- the same way.**
  The field was converted *after* `event_from_dict`'s error translation rather
  than inside it, so `"keys": 5` and `"keys": null` escaped as a bare
  `TypeError: 'int' object is not iterable`: outside the `ValueError` that
  function and `--regenerate` promise, and so one more case of a traceback and
  exit 1 where the message and exit 2 belong. The shapes that did *not* crash
  were the worse outcome -- `"keys": "ctrl+c"` became a six-key chord of one
  character each (`tuple("ctrl+c")`) and `"keys": {"ctrl": 1}` became
  `("ctrl",)`, both accepted in silence, in the very field a generated
  `gui.hotkey(...)` line is built from. `keys` is now read as a list or tuple
  of key names and anything else is refused by name. A hand-typed
  `"ctrl+c"`-style spelling is refused along with them, deliberately: this
  recorder writes and reads the list form, and accepting a second spelling
  would be inventing syntax no `--regenerate` output contains.

- **A quoted `format` still crashed `--regenerate` outside the error it
  promises.** The pass above checked `environment`, `started_at` and `raw` and
  left the version line over them alone: `"format": "1"` -- what a hand-edited
  file, or any writer that quotes its numbers, produces -- reached
  `version > FORMAT_VERSION` as a `str` and raised `TypeError: '>' not
  supported between instances of 'str' and 'int'`. `main()` catches only
  `ValueError`, so the first line of the file a reader is invited to edit gave
  a traceback and exit 1 instead of `pyguitest-recorder: ...` and exit 2: the
  same defect, one field over, and the one reached first. `"format": true` was
  quieter than that -- a `bool` is an `int` subclass, so it loaded as format 1.
  `format` is now read through the same check every other field uses, which
  refuses a `bool` and a float by name.

- **A hand-edited recording could crash `--regenerate` outside the error it
  promises.** `Recording.from_dict` documents that a malformed file raises
  `ValueError` -- `--regenerate` is where someone goes to edit one -- and it
  checked `events` for that while leaving `environment`, `started_at` and
  `raw` unchecked. An `environment` that was not an object raised
  `AttributeError: 'str' object has no attribute 'get'` from inside
  `Environment.from_dict`, and `main()` catches only `ValueError`, so a
  mistyped line gave a traceback and exit 1 instead of
  `pyguitest-recorder: ...` and exit 2. The other two were quieter and worse:
  `"started_at": "3.0"` was stored on a `float` field and round-tripped, and
  `"raw": "x"` came back as a list of characters. Both objects' fields are now
  read through a check that names the field and the type it got instead.

- **The README's link to `docs/developers/` landed on a file listing.** That
  directory holds `architecture.md` and `status.md` and has no `README.md`, so
  the link went to nothing a reader can read -- pyguitest's equivalent
  directory has an index page, which is why the same link works there. The
  entry now names the two pages, and the test that checks relative links
  requires a directory target to have a README rather than merely to exist.

- **Eight missing docstrings, all on private helpers.** `_POINT`, `_noop`,
  `_Entry`, the `__init__`s of `_KeyVocabulary`, `_KeyboardState` and
  `_SurrogatePairs`, and the two inner helpers in the Windows capture scripts.
  None is lint-enforced -- `D105` is ignored and those `__init__`s sit on
  private classes -- but they are the same set the sibling repo closed in its
  own docstring-coverage pass.

- **A click on an open menu item was recorded as a click on whatever lay
  underneath the menu.** Found live on GhostBSD/MATE, recording `File → New` in
  pluma: the script said `gui.button("Open").click()`, the toolbar button
  beneath the popup, and replaying it opens a file dialog instead of making a
  document -- clean, plausible, and wrong. `element_at` walks down from each
  application's frames, and a popup is a window of its own that is no
  descendant of them, so the point is answered for the widget under it; and
  every check the resolver makes passed, because that button is the same process
  as the window and its rectangle does contain the point. pyguitest's own
  docstring says as much (its tree "carries no stacking order"). Menu items are
  in the tree, though, and are found by name inside the menu that was opened, so
  a click on something that opens a popup (`menu`, `combo box`) is now remembered
  as its owner and the popup's items are read while it is open. The script comes
  out as `gui.element(role=Role.MENU, name="File").click()` and
  `gui.menu_item("New").click()`, and replays: two document tabs, not one and a
  dialog.

  It took three attempts to make that true live, and each passed its unit tests
  and changed nothing. **The popup is gone by the time the press is consumed** --
  choosing an item closes it and the recorder handles events after they
  happen -- so the layout is kept from when the popup was open and a press is
  answered from it, and spent by the press that chose an item (a rest beside it,
  which is consumed first, is not a press and spends nothing; nor is one outside
  the popup, which dismisses nothing). **A closed item does not always shrink**:
  mate-calc's report `1x1`, but pluma's kept `233x25` with the position at
  `-2147483648`, so a test on size called nine closed items showing and built the
  popup's bounds from them. Found by tracing the real recorder rather than
  reading it. Windows is skipped, where UI Automation hit-tests popups itself.
  Still weak where the recorder falls far behind (the note on that is in the
  file): a popup that closed before the click that opened it was consumed is
  never seen, and this answers as before. Verified on pluma and mate-calc only.
- **A click within 40 pixels of a window's corner could be recorded against a
  window one pixel wide.** Found by the same run: the File menu of pluma, 20px
  from the corner, came out as an absolute coordinate, and because the window
  looked as if it had only just appeared the 2.5s wait before the next click was
  explained as `expect_window` rather than the popup it was actually waiting
  for. `_prefer_decoration_owner` swaps the hit-test's answer for the *active*
  window when a click is that close, on the theory that a titlebar was just
  clicked -- but GTK maps a 1x1 untitled leader window beside every application
  and under marco that is what `_NET_ACTIVE_WINDOW` names, so its slack claimed
  every click near the real window's corner. A window a pixel wide has no chrome
  and is no longer preferred.
- **A generated script's header read `Recorded on: SessionType.X11
  (Compositor.OTHER, MATE)`.** `Environment` stores the `str()` of pyguitest's
  enum members and always has, because `is_windows` and every saved recording
  read that form; the header printed it as stored where the README shows `x11
  (mutter)`. It is now written as a reader would (`x11 (other, MATE)`), in the
  header and in `--doctor`, and what is stored is unchanged.
- **The note for an element refused for the wrong process blamed "another
  session" when it was from the window next door.** With two windows overlapping
  on one private display, the hit-test answered for the one underneath and the
  note said the accessibility bus was not scoped to one X display, so it came
  from another session -- true of the bus, and no help to someone looking at a
  script with two windows in it. When the element's process owns a window on the
  recorded display the note now says that, and names the window.
- **The live capture check silently tested nothing on GhostBSD, and left a
  daemon behind on every run.** It looked for the accessibility daemons in
  `/usr/libexec`, `/usr/lib/at-spi2-core` and `/usr/lib`; FreeBSD's port
  installs them in `/usr/local/libexec`, so it fell to "elements off" and
  passed. Fixing that showed that a shell exporting `NO_AT_BRIDGE=1` -- as this
  one's tool runner does, to quiet GTK -- stopped the application under test
  registering at all, so the private bus was up and empty and the check passed
  again. `NO_AT_BRIDGE` is now dropped from the application's environment when
  there is a private bus to register on. The registry was started and never
  stopped, leaving an orphaned `at-spi2-registryd` per run, and the daemons
  started before `DISPLAY` was set, which attaches the registry to whichever
  display the developer's shell has -- their own desktop -- and, with none, leaves
  it unable to inject anything: an element `click()` returned success and did
  nothing. Both are fixed, and every process the check starts is stopped.
- **On a keyboard layout with AltGr, typing `@` or `€` on Windows was
  recorded as a Ctrl+Alt chord and the character was lost.** Found reading the
  win32 backend against what Windows documents, not by a live run -- the
  machine here has a US layout, which has no AltGr. On every layout that does,
  a press of it reaches a low-level hook as a left Control with scan code
  `0x21D` that nobody pressed, then the real right Alt; the normalizer read
  `ctrl`+`alt` held and turned `AltGr+Q` into a hotkey. X11 has one key,
  `ISO_Level3_Shift`, which the normalizer already treats as making text, so
  the backend now says that here too: the fake Control is kept in the shadow
  keyboard state (`ToUnicodeEx` needs Ctrl and Alt both down to answer with the
  AltGr character) but never reported, and the Alt is named to match. A real
  Ctrl+Right-Alt on a US layout, whose Control has the ordinary scan code, is
  still a chord. Covered by tests against faked hook structures only.
- **A recording written to a file on Windows was in the wrong encoding.**
  Windows encodes redirected output in the ANSI code page, so
  `pyguitest-recorder > demo.py` turned an emoji into an error or a `?`, in a
  script Python then read as UTF-8. Found live: `Ada😀` typed, recorded and
  replayed. The script now goes to stdout's buffer as UTF-8 whenever stdout is
  not a terminal, and a diagnostic containing a character the code page cannot
  hold prints with a backslash escape rather than raising.
- **Ctrl-C did nothing while a Windows recording was running.** The consumer
  sat in an untimed `queue.get()`, which Windows never interrupts, so the only
  way out was the stop chord. The wait is now timed (a quarter of a second),
  and Ctrl-C lands in about two seconds, measured.
- **A recording made in Windows Terminal recorded the terminal.** Its window
  belongs to a process that is not an ancestor of the recorder -- the console
  is hosted by a pseudo-console -- so the ancestry walk that excludes the
  recorder's own terminal found nothing and the recording began with clicks on
  it. The console's owning window is excluded as well now.
- **A recording that quietly fell back to a smaller UI Automation context said
  nothing about why.** When the fuller connection failed and a smaller one
  opened, the clicks in the result had lost their element names and the notes
  did not say so. They now name what failed. It was seen once and did not
  reproduce, so its cause is still unknown.
- **`--doctor` reported X11 facts on Windows.** It printed `display: unset` --
  Windows has no display to set -- and said nothing about the one thing that
  decides whether a Windows recording sees a window at all. It now says whether
  the recorder is elevated, whether the window in front is, and the hook
  timeout, and warns that Task Manager, `regedit` and other administrator
  windows are not seen by an unelevated recorder.

- **A win32 recording of typed text produced `gui.tap_key("0xe7")` instead of
  `gui.type_text(...)`, because the recorder never read the character
  `SendInput`'s `KEYEVENTF_UNICODE` actually sent.** Found live, the first
  time the win32 capture backend ever recorded a real keystroke on an
  interactive desktop rather than a fake `user32`: typing `"Ada"` produced
  three raw `key_press` events named `0xe7` with no text at all, and the
  generated script replayed three meaningless key taps instead of typing
  anything. `0xE7` is `VK_PACKET`, the virtual key `KEYEVENTF_UNICODE`
  arrives as -- not only from a synthetic probe, but from IMEs composing
  CJK text, on-screen keyboards, and other remote-input tools -- and
  `ToUnicodeEx` cannot translate it: it maps a virtual key through the
  active keyboard layout, and no layout defines `VK_PACKET`. The character
  was never missing, only unread: `KBDLLHOOKSTRUCT.scanCode` carries it
  verbatim for this one virtual key. `Win32CaptureBackend._record_key` now
  reads it directly there instead of asking `ToUnicodeEx` a question no
  layout can answer.
- **A hover that kept running through typing was replayed after it, so the
  wait that let a window appear landed after the text that needed it.** A
  hover is deliberately not ended by keyboard input -- the pointer resting
  while someone types is not someone hovering something -- so its length is
  only known once something else closes it, and `Normalizer.flush()` can hand
  it over last still carrying the time it began at. `Recording.add` appended
  that, `max(0.0, ...)` clamped its negative delay to zero, and the script
  moved the pointer only after the typing it preceded. The normalizer's own
  rule is "the hover began first and must be emitted first"; `add` now keeps
  events in timestamp order so that holds however they arrive, and
  `Recording.from_dict` sorts on load so `--regenerate` repairs a recording
  already saved out of order.

  Measured on a real Windows recording: clicking **OK** in the Run dialog
  launched a console, and the next line typed into it with nothing in
  between -- no `expect_window`, no wait -- so the typing went nowhere. With
  the order restored the script waits for the console before addressing it.

- **Generated scripts named mechanisms Windows does not have.** A control
  that published no action was described as having "offered AT-SPI no click
  or press action" in a Windows script, and a recording's notes spoke of "the
  recorded display" and an accessibility bus "not scoped to one X display" --
  all three naming machinery that desktop has never had. `platforms.py` now
  answers what a given session calls these things, and the generator asks it
  about the *recording's* platform rather than the machine rendering it, so
  regenerating a Windows recording on Linux still says UI Automation.

- **Every widget inside a Store app was thrown away.** Windows hosts a UWP
  toplevel in an `ApplicationFrameWindow` owned by `ApplicationFrameHost.exe`
  while the widgets inside belong to the application, so the element's process
  and its window's differ by design -- which pyguitest already documents on
  `WINDOW_PID`. The resolver treated that as evidence the accessibility bus
  had answered about another login session, which is what it means on Linux,
  and refused them. Measured on a real Calculator recording: window pid 8824,
  buttons pid 16672, and the only element that survived was `Close Calculator`
  on the frame's own title bar, which the host does own. On Windows the pid is
  no longer evidence either way: the element is kept where its window is one of
  the host's `ApplicationFrameWindow`s -- the recorded *class* name is how a
  recording can tell -- or where the element's own rectangle corroborates that
  it sits inside that window. A mismatch with neither is refused, and the
  recording says which of the two was missing; off Windows nothing changes.

- **Keystrokes injected by another process were reported as typed.**
  `RawEvent.injected` is read from `LLKHF_INJECTED` -- something XRecord
  cannot see at all -- and then went no further than the raw log, which is off
  by default. A keep-awake script sending `{F15}` once a minute therefore put
  a `gui.tap_key("F15")` in the middle of a recording with nothing saying
  where it came from. They are still recorded, since this recorder does not
  drop what it saw, but the recording now carries a note naming the keys and
  the count. Key presses only: an injected pointer move is what every
  remote-control tool does constantly, and a note on every recording made over
  RDP would be noise.
  The count is taken where a key enters the recording, not where it arrives:
  the two presses of a completed stop chord are absorbed and never recorded,
  and were being reported as keystrokes "in this script" all the same.

- **The recorder recorded its own terminal on Windows.** `_ancestor_pids`
  shells out to `ps -eo pid=,ppid=` to find the window-owning process the
  recorder is driven from, so that terminal can be excluded. Windows has no
  `ps`: the call raised `FileNotFoundError`, was swallowed, and left the
  ancestry empty -- silently switching off the exclusion. A real recording's
  script then waited for a window titled `C:\WINDOWS\system32\cmd.exe -
  pyguitest-recorder -o script3.py --save-session session3.json`, the very
  console the recorder was running in, under a title that exists only while
  recording and so can never match on replay. Windows now reads parent pids
  from Toolhelp. The walk also stops on a repeated pid, since a process table
  read while processes exit can hand back a cycle.

- **The recorder claimed it could record where it could not.** A low-level
  hook is scoped to the window station and desktop of the thread installing
  it, so a process off the interactive desktop installs one successfully
  against a desktop nobody is using. `unavailable_reason()` took that success
  as proof and answered None -- `--doctor` printed "ready to record" over SSH
  on a real Windows 11 box, for a process that could not have captured a
  keystroke. Its own failure message already named SSH as the usual cause; the
  branch just never fired. It now asks pyguitest whether this process is on
  the interactive window station, and a probe that cannot tell is still not a
  refusal.

- **A Windows recording resolved no window and no element, so every click was
  a bare screen coordinate.** `_open_session` named `x11` and `atspi` on every
  platform, and neither can open on Windows -- no X server for the first, no
  accessibility bus for the second -- so the context session never built.
  Windows now names its own pair, `win32` and `uia`. Found in a real Windows 11
  recording: thirteen events, every one `window: null, element: null`, which is
  the recorder losing the thing it exists to do.

- **A keystroke's effect was not given time to appear before the next line
  replayed.** `normalize.py` records an idle of `pause_threshold` (1.0s) or
  more as an explicit `Pause` and drops anything shorter, which is exactly
  where it costs a replay: a chord routinely *opens* something the next line
  types into. Measured on a real recording of `Win+R`, `cmd`, Enter -- gaps of
  0.59s, 0.53s, 0.69s and 0.49s, all four dropped, so the script fired the
  chord, the text and the Return back to back and the Run dialog never took
  focus before the text arrived. The recorded gap is now restored after a
  keystroke or chord, floored so a run of fast keys gains nothing and capped
  so a long think does not become a long sleep. The same bug class
  `_COORDINATE_CLICK_SETTLE` already fixed for two adjacent clicks, in the
  place it was still open.

- **Generated scripts described things Windows does not have.** The
  no-session note read "no pyguitest session on $DISPLAY" in every Windows
  script and session file, naming a variable that machine has none of; it now
  says "for this desktop" there. `Environment.display` recorded a stray
  `DISPLAY` set by an X server (Xming, VcXsrv) or WSLg on a machine whose
  capture backend is `win32`, and with `WAYLAND_DISPLAY` set beside it -- as
  WSLg does -- the snapshot claimed the recording came "through XWayland", in
  a recording made entirely of native Windows input. Both are now Windows
  facts on Windows.


- **`motion = "verbatim"` replayed a rest for up to twice as long as it
  lasted.** A rest is stamped where it *began* and only known to have been one
  once the pointer leaves, so its hover comes out after the positions taken
  inside it -- and under `record_motion` a resting hand takes plenty, a tremor
  every few tens of milliseconds, each replayed with the wait that preceded it.
  The hover then waited out the whole dwell on top of that. Found by running
  the shape of a real recording through both halves rather than by reading
  either: a pointer that arrived at a menu row, trembled there for 1.7s and
  left came out as a script that slept for 3.58s over a 1.92s recording. A
  hand that stopped dead was no better once the rest passed `pause_threshold`,
  because an idle that long is also an inferred `Pause` over the same interval,
  and that spent it in full a second time. The hover's wait is now whatever is
  left of it: the clock is brought up to the end of the rest and no further, so
  the positions inside it and a `Pause` beside it have each already spent their
  part, and the position that leaves it is measured from there. The same
  recordings now replay at the length they were made at, trembling or still, at
  0.6s, 1.7s and 3.5s. The test that covered this built a hover with nothing
  inside it, which is not what a hand makes. The other three values are not
  touched: a hover and a `Pause` over one idle still both wait there.

- **A recorded move was attributed to the window beneath the one it was over,
  until the pointer left that window.** The cache that lets a run of moves skip
  the window lookup (`_recent_window`) answered from the last lookup for as long
  as the point stayed inside the rectangle that lookup had covered -- and
  containing the point is not the same as being under it. A pointer that began
  over bare desktop and moved onto an application drawn over it stayed inside
  the desktop's rectangle the whole way, so every move on the application came
  out against the desktop's origin and not its own, until a click looked the
  window up properly: three of five moves in a two-window reproduction. The
  answer now expires (`MOTION_TRUST_SECONDS`, a quarter of a second), so a
  stacking change goes unnoticed for at most that long, and a recording pays a
  few lookups a second to notice it where the uncached version paid hundreds.
  It is a bound and not exactness -- a window that opens over the pointer is
  still attributed to the one beneath for up to that long -- because pyguitest
  offers no cheaper question than the window list whose cost the cache exists to
  avoid.

- **A pointer move over nothing waited out a retry meant for a click, and asked
  the whole question again on the next move.** `_window` sleeps 50ms and asks
  twice more when it finds no window at all, a retry earned by a click that
  landed while a window was still animating in, and the motion path went
  through it -- uncached, because only a *hit* was ever remembered. A miss is
  any point no listed window covers, so each move there cost three full
  lookups and two sleeps, on the stretch of screen where a pointer spends much
  of its time. Found reading the motion path end to end for what else it costs per
  event, once the cache had made the hit case cheap. A move now takes the first
  answer and does not retry, and a miss is remembered for as long as a hit is;
  a click keeps its patience.

- **A second Ctrl-C while an interrupted recording was collecting its tail lost
  the whole recording, and the note on the terminal said the opposite of what
  interrupting did.** `Recorder.run` collects what capture had already
  delivered when it is interrupted, at the pace consuming anything goes -- on a
  recording that fell behind, that is the very thing that made the tail long,
  so it is a wait a user cuts short with another Ctrl-C. Only the live loop was
  guarded against `KeyboardInterrupt`, so the second one raised out of `run`
  and past everything it had collected. It now gives the rest of the tail up
  and keeps the recording as it stands, with a note saying its end is missing.
  The line the CLI prints while the recorder is behind said that interrupting
  "would drop whatever is still queued", which stopped being true when the tail
  started being collected; it now says the stop key and Ctrl-C both wait for
  the backlog, and that Ctrl-C a second time gives up on the rest. A backend
  with no `drain` at all -- the protocol asks for one, but a recording is worth
  more than its tail -- no longer costs the recording either.

  That last case was the Windows backend, which was merged after the interrupt
  path was written and had no `drain`: the same Ctrl-C there ended in an
  `AttributeError` once the recording had been made, and nothing on Linux could
  see it, because type-checking there skips the `sys.platform == "win32"`
  branch that builds the class. It has one now, with the contract
  `X11CaptureBackend.drain` has, and each backend's tests ask it whether it
  offers the whole `CaptureBackend` protocol, so a member added to the protocol
  next is checked without anyone remembering to. `mypy --platform win32`, which
  is how this was found, is clean. What Windows still does not do is recognise
  the stop chord at capture: `stop_pressed_at` is always None there, so a
  Windows recording that falls behind answers the stop key only once the backlog
  has been worked through.

- **A rest was reported at the position where it *began*, up to
  `motion_threshold` away from where the pointer actually stopped, so a replay
  waited there and then moved elsewhere.** The still window has to stay
  anchored where it opened -- that is what keeps a hand trembling over a menu
  row one rest rather than a new arrival on every jitter -- but the position
  it was *reported* at was that same opening sample, and a pointer that drifts
  7px on its way to stopping reported the 7px it had passed through. Read off
  a live recording of MATE's Applications menu: the rest that opens the
  submenu holding "Text Editor" spans y=41 to y=48, and the generated script
  waited 0.67s at y=41 before moving back *down* to y=48. A third of a menu row
  above where the hand had settled is exactly what decides whose submenu is
  open when the next click lands, and the element named for the hover was read
  from that same stale point. A rest now keeps the window's opening time and
  the pointer's final position (`_Rest`), so the wait happens where the
  interface saw the pointer stop and the hover names what is under it there.
  The move to a hover is also dropped where the route before it already ended
  at that point, which is now the common case rather than the exception.

- **A pyguitest without `backends.win32` crashed the win32 backend instead of
  explaining itself.** The key vocabulary this backend records against --
  `VK` and `key_name_for_virtual_key` -- lives in pyguitest's own Windows
  backend, which arrived with pyguitest's Windows support and is in no release
  before it. An older pyguitest reached the deferred import and raised
  `ModuleNotFoundError` four frames inside a backend constructor, naming
  nothing a reader could act on. `unavailable_reason()` now asks first and
  refuses with a sentence naming the upgrade, ahead of the window-station
  probe, since it is equally true on a machine whose desktop is perfect.

- **An exception inside either hook callback cost the application its input.**
  A Python exception escaping a `ctypes` callback reaches no caller: ctypes
  prints the traceback and returns 0, so `CallNextHookEx` never ran and the
  rest of the hook chain was skipped for that message -- the keystroke or the
  pointer event the person actually made. `_text_for` alone makes four
  `user32` calls, so this was reachable. Both callbacks now contain their
  processing and reach `CallNextHookEx` down every path.

- **`ToUnicodeEx` was consuming the keyboard layout's dead-key state, breaking
  composition in the application being recorded.** The keyboard hook calls it
  on every key-down to work out what a keystroke types, and called with no
  flags it does not merely read the layout -- it *consumes* a pending dead
  key, which is kernel-mode state belonging to the layout rather than to this
  process. Running ahead of the application the keystroke is going to, that
  meant a person typing `'` then `e` on an international layout got `'e` in
  their editor instead of `é`: the recorder altering exactly what it exists to
  observe. It now passes `ToUnicodeEx`'s "do not change keyboard state" flag,
  which Windows 10 1607 and newer honour.

- **Recorder configuration had no Windows location.** `config_paths()` looked
  under `~/.config` on every platform, which on Windows is neither
  conventional nor discoverable -- `~` there is the profile root a user sees
  in Explorer, not a place for dotfiles. `%APPDATA%` is now the default there,
  with `XDG_CONFIG_HOME` still winning wherever it is set (a Cygwin or MSYS2
  session that sets it means it), and the home-directory dotfile unchanged on
  every platform. Nothing moves on Linux or the BSDs.

- **Thirty-three tests failed rather than skipped where `ruff` was absent.**
  The generator shells out to `ruff format` and is documented to degrade
  silently without it; the tests asserting on an exact rendering were
  therefore asserting that a formatter had run. They now carry a `needs_ruff`
  marker and skip with a reason. Found on a fresh Windows box and reproduced
  identically on Linux with ruff hidden from `PATH`, which is what showed it
  was never a platform problem.

- **A pointer move recorded on Windows was the one injected event that came
  back unmarked.** `RawEvent.injected` exists so a replay's own synthetic input
  can be told apart from the input it replayed; the win32 backend computes it
  once from `LLMHF_INJECTED` for every mouse event it builds, but the motion
  branch spelled its `RawEvent` out with five arguments and no sixth. A
  replayed recording therefore came back with every click and keystroke marked
  and every move in between claiming to belong to whoever was at the keyboard,
  and a move is the event a replay generates most of. The flag is now on the
  motion too.

- **Typing into an already-focused field while the pointer sat still could
  come out of the generator before the hover that preceded it, reversing
  the recorded order.** `_dwell()` always placed buffered click/text ahead
  of the hover `MouseMove` it was about to emit, on the assumption that
  anything still buffered predates the hover -- true for a click (a button
  press always flushes an in-progress hover before a new click can be
  buffered), false for text: `feed()` deliberately lets a hover sit
  undisturbed through typing, so a run of text can start *during* an
  already-open hover, after the pointer had already settled. `_dwell()` now
  orders a buffered text run against the hover by comparing when the run
  actually started, rather than assuming it always came first.

- **A hover the pointer was still resting in when the recording stopped was
  dropped outright instead of being emitted, even when it had already run
  well past `hover_threshold`.** `flush()` only drained the buffered click
  and text run; nothing flushed a `_rest` still in progress, since nothing
  else does either -- every other path to `_dwell()` is triggered by a later
  event that also supplies the "until" timestamp, and there is no such event
  at the end of a recording. `flush()` now flushes a trailing rest too,
  measured against the last raw event actually seen (stop-key presses are
  swallowed before reaching the normalizer and cannot supply this); a rest
  with no later event at all reports as zero-length and stays correctly
  dropped by `_dwell`'s own threshold check rather than this inventing a
  duration nothing observed.

- **A recording that fell behind live input lost its tail without saying so,
  and could not be stopped.** Consuming an event is not free -- a window
  lookup, and under `record_motion` an element hit-test for every motion -- so
  a busy recording consumes slower than the hand making it. Capture's queue is
  unbounded, so nothing is dropped while that lasts, and that is what hid it:
  the stop key is read off that same stream, so a press made from behind the
  backlog had not stopped working, it had not arrived yet -- and whatever was
  still queued when the run ended went with it. Found live on a demo recording:
  39 seconds of wall clock, 209 motions and one click consumed, the first 1.92
  seconds of input, while over a hundred Escape presses and every keystroke
  typed during it stayed in the queue. The generated script had no typing in it
  at all, and the presses could not stop a recorder that had never seen them.
  The notes did carry the whole demo's worth of window titles, because the
  resolver reads the window list live: it was watching the text arrive while
  still working through the motions that preceded it.

  An interrupted run now collects what capture had already delivered before it
  finishes (`CaptureBackend.drain`), and sorts that tail exactly as live input
  is, so a stop press inside it still ends the run instead of landing in the
  script as keystrokes. Falling behind is said out loud as well, on the
  terminal while it lasts (`Recorder.on_lag`, wired up by the CLI) and in the
  recording's own notes, which the generated script's header carries -- a
  script whose end is missing now says so.

- **A pointer move raised the window it crossed, part-way through a script.**
  `_activation` counted any pointer event as the recording *acting* in a
  window, and a move is not one: it says where the pointer is, not what it is
  doing there. A recording of the demo desktop came out with three of them --
  `focus_window` for the desktop and for the panel, between moves -- because
  crossing the desktop/panel boundary on the way to the panel reads as a return
  to each. Raising a window mid-script puts it in front of whatever the next
  line was about to use, which is the failure the drag rule beside it already
  refuses to guess at. Only a click, a drag or a scroll raises a window now;
  the raise a real switch needs still comes from the click that acts in it,
  which is what the drag rule relies on as well.

- **`record_motion` cost an accessibility hit-test per motion event, which is
  what let a recording fall behind live input in the first place.** The element
  under a pointer move is never read -- a move is rendered from its coordinates
  and the origin of the window it was in, and `_point` looks at nothing else --
  so resolving one bought nothing while paying for a round trip to the
  application, hundreds of times a second. Recorded motion now resolves its
  window only (`ContextResolver.resolve_window`, used by
  `Normalizer._window_target`); hovers and clicks still resolve fully, a rest
  being one call per rest and a click being the reason elements are resolved at
  all. What is left per motion event is a window-list lookup, so a recording can
  keep up with the hand making it instead of queueing behind it. The pause
  rules do lose one thing they were never meant to have: a recorded move no
  longer marks the element it crossed as "already seen", so an inferred wait
  can no longer be aimed at one. It only shows up on a recording that has
  pauses to infer from, and recorded motion is the setting that suppresses them
  -- every motion event counts as input, so the idle clock rarely reaches
  `pause_threshold` at all.

- **The window lookup left in the motion path costs a window list per motion
  event, so `record_motion` still fell behind live input.** The change above
  took the accessibility hit-test out of a recorded move and took what was left
  -- a hit test of the window list -- to be affordable hundreds of times a
  second. It is not: `Session.window_at` lists every toplevel and reads a
  rectangle for each one, so "which window is this point in" is O(windows)
  round trips on *every* motion event. Measured on a real demo recording (MATE
  on X11, 21 seconds of wall clock): 583 events consumed, 572 of them motion,
  and the recorder 4.6s behind the hand making it -- about five times the
  throughput that produced the original 39-second recording that started this
  work, and nowhere near enough. The stop key was answered 4.6s late as a
  result, which is the whole of the note that recording carries.

  A recorded move now reuses the window the last lookup answered for as long as
  the point stays inside the rectangle that lookup covered
  (`DesktopResolver._recent_window`), and reads that rectangle again every time,
  so a window that moved is noticed exactly as before. What is left per motion
  event is one geometry read where there was a window list. Clicks and hovers
  are untouched: both still resolve fully, and a click leaves its answer behind
  for the moves that follow it.

- **A recording that fell behind could not be stopped, and said the wrong thing
  about the end it lost.** The stop key is read off the same stream as
  everything else, so a press made from behind the backlog was answered only
  once the loop had worked through everything done *before* it -- and while
  input outpaced consumption that moment never arrived at all. Over a hundred
  presses stayed in the queue unread, which is what "the stop key does not
  work" looks like from the outside. Capture now recognises the chord as it
  captures it and ends the stream at the completing press (`StopKey`, one
  implementation, run by both the pump thread and the consumer), handing over
  nothing captured after it. A recording therefore ends where the press was
  made however far behind consumption is, and the presses still reach the
  consumer first, so a single Escape is still recorded as the application's
  keystroke and "press again within 2s" still appears. Presses handed back
  because a run never completed are counted in one place now, so a press still
  held when a run ends some other way is reported as the keystroke it becomes
  instead of going into the script unmentioned.

  The lag note can tell the two endings apart now, which it could not before. A
  run that ended at the stop key has everything before that press in it, and
  says so -- what is missing is what was done *while waiting* for the recorder
  to catch up to the press, which is exactly what a stop that appears not to
  work tempts someone into doing. That was the note on the 21-second recording
  above, and it read as though the end had been dropped mid-demo. A run
  interrupted some other way still warns that its end may be short.

- **`record_motion` recorded no hovers at all, which is very often what it is
  turned on for.** The whole-path branch returned before the rest bookkeeping
  ran, so somewhere the pointer *stayed* was recorded as a run of positions and
  nothing else: that same session held 572 motion events and not one dwell, and
  the generator's hover wait -- the one thing that opens a submenu at replay,
  its own comment saying a replay that arrives and leaves in the same instant
  gets no submenu, no tooltip, and then clicks a coordinate that only exists
  because of them -- could not appear in the script at all. Rests are tracked
  whichever way motion is recorded now, so a rest is emitted as the hover it
  was, resolved fully and once per rest, ahead of the move that leaves it, while
  the path keeps every position it had.

- **A menu interaction did not replay: `motion = "natural"` replaced the route
  the pointer took with one it invented, straight across the menu.** Seen live
  on MATE, recording the Applications menu. The recording had it exactly right
  -- the pointer rested on the menu's first row (0.60s, which is what opens that
  row's submenu), travelled right along that row, then *down the submenu column*
  to the item at (212, 361) and clicked it. What the script had was a single
  `move_mouse_naturally(212, 352)` from where the pointer rested, because
  `natural` folds a run of positions to its endpoint and hands the rest to
  pyguitest. pyguitest's path bows perpendicular to each leg by a fraction of
  that leg's length (`arc`, 0.15 by default), so one leg from (57, 40) to
  (212, 352) -- 347px long -- bows some 51px across the menu's own rows:
  computed both ways the bow's apex sits at x≈89-180, inside the menu's column,
  where the recorded route at those heights was at x≈212-218, inside the
  submenu. Every row the bowed path crossed opened its own submenu, replacing
  the one the recording depended on, so the click landed on an item that
  recording never chose and the application the demo was opening never opened --
  while the menu itself did open, which is what made it look like a click
  problem rather than a path problem.

  A movement that begins or ends where the pointer came to rest now keeps its
  recorded route whichever shaping is asked for
  (`_MotionRun.touches_a_rest`), rendered as `via=` waypoints thinned to the
  corners it turned on. The rest is the evidence: the pointer had settled
  somewhere, so where it went next was steered rather than travelled, and a
  shaped path is then a *different* path rather than a smoother account of the
  same one. `natural` still folds and shapes every movement that has no rest
  beside it, which is what keeps its scripts short, and `recorded` is unchanged
  -- it keeps every route that is not straight at all. On the recording above,
  the same move now carries the seven points the hand turned on: right along the
  first row, then down inside the submenu column.

- **`natural` motion across a run of positions could ask a non-movement for a
  dwell.** `_group_motion` decides where one movement ends and the next begins,
  and now asks each event in the stream whether the pointer had settled beside
  it -- asked of a `Click`, that is an `AttributeError` and the whole render
  fails. Found by re-rendering the recording above rather than by a test, which
  is why it has one now.

- **A drag's path was not recorded at all, so `record_motion` did not do what it
  says it does.** Motion under a held button was dropped outright by the
  analyzer -- the drag event kept where it began and where it ended and nothing
  between -- and a straight line between those two points is a *different*
  gesture from the one that was made. It showed up as a hole in a real
  recording: 494 positions across 16.68 seconds, with a 1.97-second stretch
  between 11.56s and 13.53s in which no motion event existed at all, and one
  drag sitting in the middle of it. `record_motion`'s own description names
  this exact case ("the route itself -- a drag, a drawing, a gesture"), so the
  setting was promising something the code never did.

  A drag now carries its route (`Drag.route`), collected only when motion is
  being recorded in its own right, each position resolved for its window only
  as a recorded move is -- the element under the pointer is not what a drag is
  drawn from. It is serialized as points, so `--regenerate` keeps it, and it
  renders as pyguitest's own `drag(..., via=[...])`, thinned to the corners the
  hand turned on. Written in screen coordinates -- a drag that moved its own
  window, or a script asking for absolute locators -- the whole route goes in;
  relative to a window, one `window_x`/`window_y` read serves the list, so only
  the points sharing the *end*'s origin can, which is the limit `_group_motion`
  already refused to cross. A recording made before this keeps its drags as
  they were: there is no route in it to render, and none is invented.

  The live capture check's drag now arcs deliberately rather than interpolating
  straight, because a straight route is the one case where the recorded path
  adds nothing -- a check that cannot tell the two apart agrees with any amount
  of dropping it.

- **A configuration block under any heading the loader did not recognise was
  ignored in silence.** Five section names were hard-coded and only those were
  read, so a file grouped any other way -- or one whose heading was simply
  misspelled -- contributed nothing at all: the settings were in the file, the
  file was in force, and none of it applied, with no error and nothing to say
  why. Every table in the file is flattened now, whatever it is called, so a
  section is grouping for the reader and nothing else; an unknown *key* is
  still refused by name, in whichever section it appears. Found by auditing
  every key in the example against the code -- which is also what turned up the
  stale `record_motion` notes above. Two guards came out of it: the example is
  asserted to *be* the defaults, and every setting is asserted to be named in
  it.

- **The wait a chord had earned was thrown away by the pointer move after it,
  and measured from the wrong event when it was not.** `_settle_after_key_action`
  restores the gap a keystroke is followed by where `normalize.py` dropped it
  (below `pause_threshold`), and it is owed to the *chord* -- a `Win+R` opens a
  window the next line addresses. It was tracked as "a keystroke is pending",
  set after every event, so a move in between cleared it and the click that
  actually needed the pause got none. The flag was also answered by each
  event's own `delay`, which is the interval to the event *before* it: a click
  0.2s after a move that was itself 0.7s after the chord read as 0.2s -- under
  the floor -- on an interval the recording had spent 0.9s on. The timestamp of
  the keystroke is kept now, the wait is derived from it, and a move neither
  takes it nor cancels it, since a move is the step towards the line that does.
  An input event or a recorded `Pause`/`WaitForIdle` consumes it, because
  either the line that addressed the new window has already run or the seconds
  are already in the script.

- **A character outside the Basic Multilingual Plane recorded as two unpaired
  surrogates, and the script could not be written.** `SendInput` sends one
  `VK_PACKET` keystroke per UTF-16 code unit, so an emoji arrives as a high
  half and a low half. `chr()` on each half is a lone surrogate -- not the
  character -- and joining the two does not decode them either, because a
  Python string is code points and not UTF-16 units. `normalize.py` carried the
  halves into one `TextInput`, the generator wrote them into
  `gui.type_text(...)`, and the first thing that had to encode that string
  raised `UnicodeEncodeError`: `--regenerate` died writing the file it had just
  built. The high half is held back until its pair arrives now, and an unpaired
  half -- no pair coming, which an ordinary key in between establishes -- is
  dropped rather than passed on, since it has no character to replay.
  Held back means not enqueued either: the first version still queued the
  high half as a text-less press, which the normalizer turns into a
  `KeyStroke`, so the script opened with `gui.tap_key("U+D83D")` -- a key name
  pyguitest rejects -- ahead of the `type_text` for the character it belonged
  to. Found by review, since the live run typed no emoji, and pinned by a test
  that runs a pair through the normalizer rather than stopping at the backend.

- **A recording made through an X server on a Windows machine described itself
  as a native Windows desktop.** Windows can run Xming, VcXsrv or WSLg, and
  `backend = "xrecord"` there records X clients -- but every question about
  which platform a recording was of was answered from `sys.platform`, so the
  context was asked for as `win32` + `uia` (a native desktop none of whose
  windows are in the recording), `$DISPLAY` was cleared out of the header of
  the one recording that display explains, the session was described as "for
  this desktop", and the resolver relaxed its pid corroboration for an X11
  recording. The selected capture backend decides now, through one helper both
  the selection and every one of those questions go through, so `auto` still
  answers for the host and a named backend answers for itself. `probe_context`
  asks the same way, before there is a backend to ask.

- **A hook that installed off the interactive desktop was started anyway.**
  `unavailable_reason` asks whether this process is on the interactive window
  station (see its own entry above), but it is asked during *selection* --
  `Recorder.start` opens the session and only then starts the backend, and
  nothing about `Win32CaptureBackend` requires that anything selected it at
  all. A backend built on a real desktop and started after the session went
  away -- RDP dropping to a disconnected session is enough -- installed its
  hooks successfully and recorded nothing, with no error and no note. `start()`
  now refuses with the same sentence. Same shape as the selector's own fix, in
  the place where the answer can still change under a running process.

- **A recording's saved delays were trusted after the events were reordered,
  and an inserted event kept a delay measured against a predecessor it no
  longer had.** A delay is the interval to the event in front of it, so any
  event the timestamp sort moves is left holding a gap that belongs to another
  neighbour -- visible in a `recorded`/`verbatim` script as a pause in front of
  a line nothing waited for. `Recording.add` recomputed only the delays that
  were empty ("or not event.delay"), so an event arriving with its own delay
  kept it, and `Recording.from_dict` sorted without recalculating anything.
  Both recompute now, the inserted event included, and the first event's delay
  is zero in every case. Found by reading the two paths against each other:
  `add`'s docstring claims both the moved event and the one it displaces are
  recomputed from the neighbour each ends up with, and only one of them was.

- **A hand-edited timestamp could crash `--regenerate` outside the error it
  promises.** `Event.timestamp` defaults to `0.0`, so a missing one read as
  "the recording began here" over the field the model orders events by, a
  `"3.0"` string reached the load-time sort and raised `TypeError` from
  comparing a float to a str -- not the `ValueError` `Recording.from_dict`
  documents and `main()` catches -- and a `NaN` sorted into an order nothing
  can explain, since it compares false to everything including itself.
  `event_from_dict` requires the field to be present, numeric and finite, and
  names which of the three failed. `to_dict` has always written it, so nothing
  this recorder saves is affected: a file that lost it is one a person edited.
  A JSON integer too large for a float (`10**1000` is valid JSON) is the
  fourth: `math.isfinite` converts before it looks, and raised `OverflowError`
  from outside the same contract, so it is reported as not finite too.

## [0.3.0] — 2026-09-13

Two threads. One is the `motion` setting, which decides how a pointer move is
rendered — shaped, recorded, or the teleport every recording has produced until
now. The other is the floor and the prose catching up to what the code already
did, so that a generated script names only what the pyguitest installed beside
it can answer.

### Added

- **A `win32` capture backend: this recorder is no longer X11-only.** Two
  `WH_KEYBOARD_LL`/`WH_MOUSE_LL` hooks stand in for XRecord on native Windows —
  the platform's own counterpart, and the only route available: Windows has no
  interface that lets one process observe another's input the way XRecord
  does, and Microsoft's documented alternative, raw input, needs a
  message-only window this first pass does not build. `choose_backend`
  prefers it automatically on Windows (never falling back to `xrecord`, which
  would silently record only whatever X clients happen to be running under
  Xming, VcXsrv or WSLg — a phantom-desktop recording with no error at all)
  and still honours an explicit `backend = "xrecord"` for whoever wants that
  anyway.

  The honest limit this backend carries and XRecord does not: **a low-level
  hook that misses `LowLevelHooksTimeout` (300ms by default, 1000ms the most
  any recent Windows build will honour) is silently unhooked, with no
  `CallNextHookEx`, no error, and no way for this process to find out.** The
  callback is kept to the least possible work — read the structure, enqueue
  it, return — which is Microsoft's own mitigation, and the rest (keysym
  resolution, `ToUnicodeEx` for typed text) runs off that same thread rather
  than being deferred, so `analyzer/normalize.py` needed no changes at all:
  every `RawEvent` this backend produces is shaped exactly like X11's, down to
  `raw.text` being populated in the callback the same way `_printable()`
  populates it there.

  Two things XRecord cannot do at all come along with the platform:
  `RawEvent` gained an `injected` field, set from `LLKHF_INJECTED`/
  `LLMHF_INJECTED` — a synthetic keystroke or click is recorded and marked
  rather than silently dropped or silently kept indistinguishable from a
  real one, which is the same "never drop what you saw" rule this package
  applies everywhere else. And every keysym this backend names uses X11's
  own spelling (`Control_L`, `BackSpace`, `Super_L`), not pyguitest's own
  lower-cased internal vocabulary — replay would not care either way, but
  `analyzer/normalize.py`'s `MODIFIERS` table matches X11's spelling exactly,
  character for character, and a lower-cased `control_l` would have made
  every Windows-recorded Ctrl-chord invisible to it.

  **Nothing here has run on a Windows machine.** Every structure, flag and
  call shape is transcribed from Microsoft's own documentation, and the
  tests drive a fake `user32` the same shape `tests/test_x11.py` fakes
  `Xlib` — real hook installation, a real background pump thread, real
  `ctypes` structures cast from raw addresses, none of it a live Win32 API.

- **`motion`, deciding how a pointer move is rendered: `teleport` (unchanged,
  and still the default), `natural`, or `recorded`.** `teleport` is
  `gui.move_mouse()`, as every recording has always produced. `natural` emits
  `gui.move_mouse_naturally()` instead — the same call length, the same
  recorded endpoints, and a path pyguitest shapes, so the pointer is genuinely
  on its way rather than only arriving: a hover reveal, a hot corner, or a menu
  that opens on the approach now fires on the approach at replay.
  `recorded` also puts the route back, as `via=` waypoints. The default stays
  `teleport` because `_move` positions the pointer before every coordinate
  click and scroll too, where the path was incidental — shaping those would put
  a derived 0.25-1.5s in front of each one, which is the slowdown
  `_HOVER_WAIT_CAP` exists to avoid. `--natural-motion`, `--recorded-motion`,
  `--teleport-motion` and `--max-waypoints` set it from the command line.

  `recorded` is the only value that can make a script long, which is why it is
  asked for rather than assumed, and it is kept in hand by thinning rather than
  by a budget. The route goes through Douglas-Peucker at `motion_threshold`
  (8px — the line this codebase already draws between "the pointer meant this"
  and "the pointer was passing through"), which is chosen for the property that
  matters here: **every point it drops is within that tolerance of the line it
  keeps.** Measured, on synthetic routes: 500 samples of a 180px curve become
  29 waypoints, a quarter circle becomes 9, a 300-sample line carrying 7px of
  hand jitter becomes none at all, and a route that never turned emits no `via`
  rather than a route-shaped lie. `max_waypoints` (32) sits above all of that as
  a backstop, and it is met by loosening the tolerance rather than by
  subsampling — dropping every N-th point throws away exactly the corners
  thinning just identified, which is how a 12-point zigzag came out a straight
  line under a cap of 8.

  A run of positions is folded into one move only where folding is safe. A
  `MouseMove` that *is* a hover is never collapsed into one — its dwell is the
  whole point of it, and a submenu that opened because the pointer stayed would
  be lost. Nor does a run span two windows at different origins: a relative
  coordinate is an offset into where its window was at the moment of capture,
  and one `via` is rendered against a single `window_x`/`window_y` read, so a
  window that moved partway through the run would have its earlier points
  measured from an origin the script no longer has. That is the same failure
  `_ensure_geometry` already handles within one event, met across a run.

  As with the `Element.double_click` spelling, the generator asks the
  installed pyguitest whether it has the method before emitting it, so an
  install that predates it still gets a script it answers — rendered as
  teleports, with a warning — rather than one naming a method it does not have.

### Changed

- **The pyguitest floor is now 0.10.1**, the release that adds
  `move_mouse_naturally`. The `motion` setting above emits that call under
  "natural" and "recorded", so a floor that still permitted 0.10.0 would let
  pip install a pair whose generated scripts name a method the library does not
  have — which is precisely the failure `validate()` exists to catch, arriving
  as a dependency's problem rather than a generator's. The generator's own
  check for the method is unchanged, and still renders teleports with a warning
  on an install the floor would not have allowed in the first place: a checkout
  of an older pyguitest, or an install made with `--no-deps`.

- **A double click on a named element is asked of the element itself where
  the installed pyguitest can do it, and of the session where it cannot.**
  pyguitest's `Element` gained `double_click` after 0.9.0 — the release the
  floor still names — so the generator asks `pyguitest.Element` what it offers
  before it emits the call, the same way it already asked `Session`, and falls
  back to `gui.double_click_element(element)` on an install that predates it.
  An older pyguitest therefore keeps getting a script it answers instead of
  one naming a method it does not have, and no floor bump is needed to take
  the new spelling when it arrives. Nothing about the gesture changes: the
  element stays the locator, its rectangle is read at replay rather than baked
  in, and an element carrying no rectangle still degrades to two
  `Element.click()` calls. That degradation's comment now says so — it used to
  claim `Element` had no `double_click` at all, which was true of 0.9.0 and is
  about to stop being true.

- **The pyguitest floor is now 0.10.0, and the code that existed to support
  0.9.0 went with it: the generator no longer asks the installed `Element`
  whether it has a `double_click`, and never emits
  `Session.double_click_element` as a fallback spelling.** Under a 0.9.0 floor
  the two spellings named the same gesture, and which one came out depended on
  what was installed — a branch no supported install takes now.
  `Element.double_click` is what generated scripts get, and `_element_methods()`
  is deleted along with the fallback it served. The floor also buys several
  `app_id`s per window lookup, for a window named differently by each protocol,
  which pyguitest 0.10.0 added.

- **`validate()` now checks what a generated script calls on an element against
  the installed `Element`, not only `gui.*` calls against `Session`.**
  `gui.element(...).double_click()` is an attribute read on a *result* rather
  than on the `gui` name, so the older check never saw it — which meant a script
  naming a method its pyguitest did not have passed validation and failed at
  replay with the `AttributeError` naming it. The factories come from the
  generator's own sugar table, so an accessor added there is covered the moment
  it can be emitted; the other ways a script gets an element (`window_element`,
  `root_element`, `element_at`) are covered too, since these files are meant to
  be edited.

### Fixed

- **Three claims about the project were older than its code: the status page
  and the troubleshooting guide both said pyguitest could not look a window up
  by application id — the stated reason a helper function had to be carried
  into each generated script — and `pyproject.toml` gave the floor as 0.5.0 in
  one place and 0.9.0 in another while calling 0.2.0 "never tagged".** None of
  it was true any more. `app_id` lookups landed in pyguitest 0.7.0, which the
  `v0.9.0` tag the floor names is verified to contain; the generator emits
  `gui.expect_window(app_id=...)` for a window whose title drifted; the private
  helpers stopped being written into scripts in 0.2.0; and `v0.2.0` was tagged
  on 2026-09-12 with a CHANGELOG section of its own. The cost was a status page
  inviting somebody to build a feature that already existed. The `atspi`
  extra's floor is gone rather than corrected — the base dependency sets it,
  and a second copy is one more number to go stale. Found by checking whether
  the feature existed before starting on it.

- **Four documentation pages still described the release before 0.2.0,
  including two code samples and the key users are told to press to record a
  check.** The private `expect_*` helpers stopped being written into generated
  scripts in 0.2.0 — they are pyguitest `Session` methods now — but `README.md`,
  `docs/getting-started.md` and `docs/recipes.md` still showed the old
  free-function call `expect_text(gui, ...)` and still explained that the
  helpers were "written into the generated file". The same pages and
  `docs/troubleshooting.md` also told users to press **Ctrl+F1**, which was the
  default until 0.2.0 moved it to **Ctrl+1** — precisely because a laptop's bare
  F1 commonly sends `XF86_AudioMute` rather than `F1`, so the press is never
  matched and is silently recorded as an ordinary keystroke. The instructions
  were therefore describing the exact failure the change existed to remove. Two
  pages also stated a pyguitest floor of 0.5.0 against `pyproject.toml`'s
  0.9.0, and both copies of the sample generated file still opened
  `Profile: pyguitest-0.5` against a generator `PROFILE` of `pyguitest-0.9`.

  `tests/test_docs.py` is the new guard. It compares the prose against the real
  code rather than grepping for words: the stated floor against `pyproject.toml`,
  the sample header against `PROFILE`, the documented check key against
  `Settings.check_key`, and every documented `--flag` against the CLI parser —
  plus that every relative link and every entry in a page's own table of
  contents still resolves. pyguitest's `test_docs.py` and python-libei's
  `test_documentation_shape.py`/`test_documented_examples.py` were already
  doing this; this repo's suite was all code and no prose, which is how a
  release could land with four pages describing the one before it.

## [0.2.0] — 2026-09-12

Everything here came out of recording real applications and replaying what
came back: MATE's Applications menu, a GNOME Text Editor window on
GhostBSD, KDE's Kickoff. Recording was already right in each case — what
the generated script did with it was not, and each fix below is a replay
that did the wrong thing on a real desktop.

### Added

- **Hovering is recorded now**, as a `MouseMove` carrying how long the
  pointer rested (`hover_threshold`, default 0.3s). A hover is an input:
  it opens submenus, shows tooltips, and starts the auto-scroll on a long
  menu — and none of it is a click, so a recording that kept only clicks
  replayed a pointer teleporting to a coordinate that was only valid
  *because* of a hover that never happened. Found live on MATE: the
  Applications menu opens a category's submenu on hover, so a recorded
  click on "Text Editor" at (226, 368) replayed onto bare desktop — the
  submenu holding it was never opened — and dismissed the menu instead.
  The generated script now renders the hover as the move plus the wait
  that makes it one, capped at 2s.

  Deliberately not built as menu detection: menus are override-redirect
  windows that do not appear in the window list at all (confirmed live —
  `windows()` showed nothing while a menu was open, and `window_at()` on a
  menu entry returned the desktop), so recognizing them means dropping to
  raw X11 for something Wayland has no equivalent of, and it would only
  ever solve menus. Resting the pointer is observable everywhere.

  The pointer is tracked for this without paying `record_motion`'s price:
  position and time only, with the resolver consulted once per hover
  rather than once per motion event.

- **`Recorder.on_stop_progress(got, needed)`**, called on a stop-key press
  that registers but does not yet complete the run. Pressing the stop key
  once has no visible effect at all, so the natural response is to pause
  and check before pressing again -- long enough, live, to exceed
  `stop_key_interval` and have the first press discarded as the recorded
  application's own keystroke. The CLI now wires this up to print
  `Escape (1/2) -- press again within 2s to stop.` (using whatever key and
  interval are actually configured), so there is no need to guess whether a
  press was seen.

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

- **Generated scripts no longer carry a private copy of `expect_window`,
  `expect_element`, `expect_text`, `expect_checked`, `expect_showing`, or
  `double_click_element` -- they call the pyguitest `Session` methods of the
  same names instead** (new in pyguitest 0.9; see its changelog). A window
  binding used to read `window = _expect_window(gui, "Title", timeout=10)`
  followed, at the bottom of the file, by a ~30-line function definition
  duplicated into *every* script that needed one; it now reads
  `window = gui.expect_window("Title", timeout=10)` and nothing else, with
  the exact same behavior (settle, retry-focus, raise on timeout) living
  once in pyguitest instead of once per generated script. Raises the
  minimum pyguitest version accordingly (see the dependency comment in
  `pyproject.toml`).

- **`stop_key_interval`'s default is now 2.0s, not 1.0.** 1.0 measured live
  as too tight for how people actually press it: a real capture showed
  1.333s between the release of a first Escape press and the start of a
  second meant as the same deliberate run -- comfortably past the old
  default, so the first press was discarded and a third press was needed to
  actually stop. Paired with the progress feedback above rather than
  instead of it, since a wider window alone does not tell anyone whether
  their first press was seen.

### Fixed

- **Binding a window raised and focused it, which dismissed any menu that
  was open.** Found live on MATE: a script that only wanted the desktop's
  origin to offset a menu click by got `gui.expect_window("Desktop", ...)`,
  which activated the desktop — raising it over the open Applications menu,
  dismissing it, and leaving every click that followed landing on bare
  desktop. The replay opened the menu, closed it, and did nothing else.
  A lookup is now a lookup: `expect_window` finds a window and does not
  touch it. The settle-and-confirm-focus behavior moved to pyguitest's new
  `Session.focus_window()`, which a generated script now calls in the two
  places that actually need it — where the recording carries a
  `WindowActivate`, and immediately before typing into a window — rather
  than on every window it happens to bind. Typing is the case that matters:
  a click lands at its coordinate whether or not the window was ready, but
  a keystroke goes to whatever *does* hold focus, and a freshly-opened
  window can exist before the window manager has focused it. Confirming
  focus on every bind was the overcorrection in the other direction, and
  dropping it entirely was this fix's own first overcorrection: it sent
  `type_text` into whatever had focus, usually the terminal running the
  replay. Generated scripts no longer declare `Capability.WINDOW_ACTIVATE`
  unless they actually focus something.

- **Two clicks the recording made close together but not close enough to
  merge into a double-click could replay fast enough to trigger the target
  app's own double-click gesture -- toggling a window's maximize state
  instead of the plain close it was recorded as.** Root cause in
  `normalize.py`: clicks under `double_click_interval` (0.4s) apart merge
  into one double-click event, and only a gap of `pause_threshold` (1.0s)
  or more becomes an explicit `gui.wait(...)`; a real gap in between --
  long enough to be genuinely separate, too short to be worth a comment --
  fell through both and was silently dropped, so the generated script
  issued the two clicks back to back. Reproduced live on GhostBSD/MATE:
  three real, separately-timed clicks near a GNOME Text Editor window's
  close button replayed as a fast pair that read as a double-click on the
  header bar, toggling maximize instead of closing. Fixed in the
  generator, not `normalize.py` -- the dropped gap's exact duration is not
  worth reconstructing, only that two clicks the recording already decided
  were separate must not collapse back into one on replay -- by inserting
  a fixed 0.5s `gui.wait(...)` between any two coordinate clicks rendered
  with nothing else in between.

- **A generated script's pointer actions had no synchronization at all when
  a click resolved to no window at all -- not even the desktop -- leaving
  it to the user's own recorded pause, which was too short to notice on a
  fast, confident click.** Same live KDE reproduction as the resolver retry
  below: dismissing GNOME Text Editor's own in-window "Discard changes?"
  sheet with two clicks close together in time meant `normalize.py`'s
  1-second `pause_threshold` never saw a gap worth turning into an explicit
  wait, so nothing paced the second click against the sheet still
  animating in. `target.window is None` is the one case a generated script
  has nothing else grounding the point in, so it is now also the one case
  that gets a small (0.3s) unconditional settle wait before the pointer
  moves there, regardless of what the recording user happened to notice.

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
