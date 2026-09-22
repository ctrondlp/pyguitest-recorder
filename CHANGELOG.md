# Changelog

Notable changes, newest first. Dates are when the work landed, not when it
was released.

## [Unreleased]

### Fixed

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
