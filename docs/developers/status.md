# Status and known gaps

What has actually been run, and what is still missing. Following pyguitest's
own convention of saying which parts are verified and how, rather than
claiming everything works.

**Early, but the engine is complete and every path has now been run.**

| Part | State |
|------|-------|
| Canonical event model, JSON round-trip | tested, on CI |
| Semantic analyzer (clicks, drags, text, hotkeys, pauses) | tested |
| Synchronization inference | tested |
| Checks (the check key, and what they generate) | tested; not yet run live |
| Script generator + API validation | tested against the installed pyguitest |
| Configuration (TOML, XDG, precedence) | tested |
| CLI (`--doctor`, `--regenerate`) | tested |
| Window/title-drift resolution | tested; drift fix found by a live recording |
| XRecord decoding, keysyms, teardown | tested against synthetic X events |
| XRecord capture of a real application | run live, here and on CI runners |
| win32 capture (low-level hooks, keysyms, `ToUnicodeEx`) | run live on Windows 11 -- see below |
| AT-SPI element resolution | run live against a real accessibility bus |
| Focus-based targeting for typed text | run live; names a GTK4 field |
| Drag, window switching, save/regenerate | run live |

## The live capture check

`scripts/live-capture-check.py` is what closed the last two rows. It starts a
private Xvfb and a private accessibility bus, puts an application on them,
synthesises real input, and runs the whole pipeline over what comes back —
capture, resolve, normalize, infer, generate, validate. It needs
`xorg-x11-server-Xvfb` (Fedora) or `xvfb` (Debian), and it will not run
against your own session, because XRecord captures every application's
keystrokes and not only the one being recorded. CI runs it on every push, so
the capture backend is not a thing that only works on one developer's machine.

It earns its keep. Running it the first time found a fatal bug no unit test
could have: the RECORD context was created on one display connection and
enabled on the other, which the server refuses with `BadContext`, so capture
had never worked at all. Running it the second time found that the window
context came from the *compositor* rather than the recorded display, so a
recording made on a private X server resolved its clicks onto the editor this
was written in.

**A rootless XWayland is not a substitute for Xvfb.** Verified against a
private headless GNOME session: the RECORD context comes up and disables
cleanly, but neither XTEST nor kernel-level `uinput` can inject into it —
the compositor owns the pointer — so there is no input to record. It is the
same wall described in
[architecture.md](architecture.md#why-wayland-has-no-capture-backend),
met from the other side.

## Run live on Windows 11 (build 26200), interactive desktop

2026-09-19, the console session itself (not SSH) — the exact gap the row
above used to name. `scripts/win32-live-capture-check.py` is the Windows
counterpart of `live-capture-check.py`, closing it the same way: a real
`SetWindowsHookExW` hook, a real window (`scripts/win32_probe_window.py`, a
hand-rolled `EDIT`/`BUTTON`/`STATIC`/`SysTabControl32`/`COMBOBOX`/
`SysListView32`/menu-bar window — not an OS application, for the reason in
its own module docstring), and the whole pipeline run over what came back.

**A real bug, found on the first live run and fixed before this line was
written.** Typing `"Ada"` produced three raw `key_press` events named
`0xe7` with no text at all, and the generated script replayed
`gui.tap_key("0xe7")` three times instead of typing anything. `0xE7` is
`VK_PACKET` — the virtual key `SendInput`'s `KEYEVENTF_UNICODE` arrives as,
which is not only how a synthetic probe types but how IMEs, on-screen
keyboards, and other remote-input tools produce text too — and
`ToUnicodeEx` cannot translate it: it maps a virtual key through the active
keyboard *layout*, and no layout defines `VK_PACKET`. The character was
never missing, only unread: `KBDLLHOOKSTRUCT.scanCode` carries it verbatim
for this one virtual key. Fixed in `Win32CaptureBackend._record_key` — see
the CHANGELOG.

**Confirmed working, full pipeline, one purpose-built window:** typed text
landing correctly (`gui.type_text`, read back through `uia`'s own
`Element.text` rather than a raw `GetWindowText` — see pyguitest's own
`docs/validation.md` for why that distinction mattered), a plain button
click, a real menu bar (`Actions` → `Do Thing`), a `SysTabControl32` tab
switch, a `COMBOBOX` selection, a checkbox toggle, a right click dismissed
with Escape, a scroll, a double click, and — the two deliberately adversarial
cases — a window **drag** mid-recording and a **maximize/restore** cycle,
neither preceded by a `WindowActivate` event. Both correctly produced "the
window moved, so its origin is read again" in the generated script
(`generator/python.py`'s `_ensure_geometry`, previously fixed for exactly
this shape of bug per its own docstring, now confirmed live on Windows too)
— three re-reads for three real geometry changes, all landing at the right
place on replay against a fresh window.

A **real `SysListView32`** (report view, a real header, real rows) offered
`Element.click()`/`.selected` a working `SelectionItem` action — confirmed
in isolation (`False` before a click, `True` after) — unlike this window's
tabs, menu items, and checkbox, none of which offer UI Automation any
click/press action at all, so the generator correctly falls back to a
coordinate for those and says so in a comment. A list selection did not
reproduce in the first full run, and was first put down to resolver lag; it
was the probe window. A plain report-view list only selects when the click
lands on the item's *label*, and the recorded click was at the centre of the
row's UI Automation rectangle -- blank space to the right of the word -- so
nothing was selected at record time either, and the replay faithfully
reproduced that. Setting `LVS_EX_FULLROWSELECT`, as real list views nearly
always do, made the same recording replay with the row selected.

**A real application, not only the purpose-built window:** recording and
replaying typed text, Ctrl+A/Ctrl+B (bold), and opening/closing the File and
Edit menus against Windows 11's own Notepad. The generated script correctly
recorded and replayed the click and keystrokes; what made the round-trip
*look* like a replay failure was Notepad itself, not this package —
**Windows 11 Notepad restores its previous draft session even after the
process is killed outright** (`taskkill /F`, no graceful shutdown, no save
prompt possible), so "kill the process" does not mean "next launch starts
blank" the way it does for a hand-rolled window or most classic Win32 apps.
A second run's typing landed in the *same* restored, previously-typed
document rather than a fresh one, reading back doubled. Confirmed by
listing the window's own elements before touching anything: a freshly
"killed and relaunched" Notepad came back with the exact draft tabs open
before the kill, unmodified. Not a recorder bug and not chased further as
one; recorded here because it is a real trap for testing Notepad
specifically; the earlier, purpose-built-window checks are unaffected and
are what this project leans on for anything needing a clean slate.

**Two more things this run settled, briefly:** `services.msc`,
`certmgr.msc`, and `regedit.exe` all auto-elevate via UAC on at least this
machine (confirmed by a non-elevated process being refused even
`TerminateProcess` on them) — out of reach for a non-elevated recorder
session by UIPI before capture even enters into it, which is why the
"real application" checks above use Notepad and a hand-rolled window rather
than one of those. `--doctor` now says so up front on Windows: whether this
process is elevated, and whether the window in front is.

**A second pass, on what a recording depends on but a probe window does not
exercise,** found four more bugs, each reproduced before it was fixed and
described in the CHANGELOG: a script redirected to a file was written in the
ANSI code page rather than UTF-8; Ctrl-C did nothing while recording; the
recorder recorded the Windows Terminal it was started from; and a fall-back to
a smaller UI Automation context said nothing about why. `--doctor` now reports
whether the recorder and the window in front are elevated.

**Not verified, and why.** AltGr (the fake left Control Windows sends with it,
scan code `0x21D`) is handled and unit-tested, but this machine has a US
layout, which has no AltGr, so it has not been seen on a real keyboard; dead
keys are in the same position. The `LowLevelHooksTimeout` limit itself has not
been provoked. What has been measured is the callback's cost: 0.04 ms at the
median and 3 ms at the worst over 20,000 key-downs, against the 300 ms limit.

## Run live on GhostBSD 26.1 / MATE, recorded and replayed (2026-09-20)

A private Xvfb with the real MATE window manager (`marco`) on it, a private
session bus and a private accessibility bus -- never the desktop the machine was
being used from -- with real applications on it: mate-calc and pluma. Each was
recorded with the CLI, the generated script replayed into a fresh copy of the
application, and then replayed again into a window moved across the screen.

**Confirmed working:** `7 + 3 =` in mate-calc, by button name, replayed into the
same window and into a moved one, the display reading `10` each time; typed text,
a Ctrl+Home chord and a menu in pluma, replayed into both, the document reading
back exactly; `File → New` in pluma by menu-item name, two document tabs after
replay with teleport motion.

**Found and fixed, all described in the CHANGELOG:** a click on an open menu item
recorded as the widget under the menu (`gui.button("Open")` for `New`); a click
near a window's corner recorded against its one-pixel GTK leader window; the
header printing enum reprs; a misleading "another session" note; and the live
check itself, which silently tested nothing here and leaked a daemon per run. The
popup bug took three attempts to fix live because each passed its tests and
changed nothing -- the press is consumed after the popup has closed, and a closed
item's size is not always a sign of it -- and the second and third were found by
tracing the real recorder rather than by reading it.

**Measured, not fixed:** `Session.focused()` is a walk of every node on the
desktop, ~1.5s per call on pluma's 461 nodes (~3ms a node), and the recorder calls
it once per run of typed text -- that is the "fell 1.7s behind live input" note in
a light recording, and it grows with the application. A prototype that reads only
the active frame, skips what is not showing and stops at the first hit took 0.3s
on the same tree. It belongs in pyguitest, so it is recorded here rather than
worked around. **Not run:** the user's own live desktop, where the same fixes are
unconfirmed.

## Run live on the developer's own GNOME Shell 51.rc desktop (2026-09-21)

The line the section above ended on — "**Not run:** the user's own live
desktop" — closed. Fedora 45, GNOME Shell 51.rc, Wayland, recording through
the session's own XWayland on `:0`, with `gedit` (GTK3) as an XWayland client
and again as a native Wayland one. Recorded with the `Recorder` API, driven
by pyguitest through in-process uinput, and each generated script replayed
into a fresh copy of the application with the document read back through
AT-SPI afterwards — because a script that runs clean and does the wrong thing
is the failure this project keeps meeting.

**Confirmed working:** typed text, a click on a named `Open` button, the file
chooser it raised recorded as a second window, and the Escape that dismissed
it — generated as `gui.expect_window(...)`, `gui.focus_window(...)`,
`gui.type_text(...)`, `gui.button("Open").click()`,
`gui.wait_for_idle(pid)`, and window-relative coordinates for the move. The
script replayed into a fresh XWayland gedit with the document reading back
`Ada Lovelace`, and into a *native Wayland* gedit after one edit described
below.

**Four bugs, each reproduced before it was fixed.**

- **`stop()` from another thread silently stripped every window and element
  off the backlog.** It closed the resolver's pyguitest session the moment it
  was called, while `run()` was still consuming what capture had already
  delivered — and resolution happens at consume time. The recording kept its
  coordinates and lost its names: bare `gui.move_mouse(...)`/`gui.click()`
  with no `expect_window` at all, validating clean, with nothing saying what
  had been lost. Proved by control rather than by reading: the *identical*
  interaction generated `gui.button("Open").click()` when the consumer was
  given 20s to catch up first and a bare coordinate when it was not. `stop()`
  now stops capture immediately and leaves the context to `run()`, which
  closes it once it has finished with it. This is the documented way to end a
  run from another thread, so every watchdog and check script was exposed to
  it.

- **A recording made on a Wayland desktop said it was made on X11.**
  `scoped_environment` removes `WAYLAND_DISPLAY` on purpose, so that a
  recording of X clients is not described as a Wayland session — and the same
  stripped environment was then handed to the detection whose answer decides
  `environment.xwayland`. `_classify` needs that variable to say XWayland at
  all, so every recording ever made on a real Wayland session came back
  `x11`: no note, no header block, a script indistinguishable from one
  recorded on Xorg while native Wayland clients had been invisible to it
  throughout. `--doctor` said `xwayland` about the same session in the same
  minute, because it detects against the ambient environment — two paths
  disagreeing, with the wrong one going into the file. It contradicted
  [architecture.md](architecture.md#why-wayland-has-no-capture-backend)'s own
  claim that this "says so in the recording and in the generated script's
  header". Now asked of the server being recorded: XWayland advertises an
  `XWAYLAND` X extension, which is the only thing that can tell a session's
  own XWayland from a private Xvfb started on that same session — both have
  the two variables set. Checked both ways on one machine: `:0` lists it and
  an Xvfb on `:77` does not.

- **Window identity was fixed from a window that had already reacted.** The
  resolver establishes a window's identity the first time an event resolves
  to it, which is consume time. With the consumer behind, gedit was first
  seen as `*Untitled Document 1 - gedit` — the modified-marker title that
  does not exist until the recorded typing has happened. Nothing had seen it
  drift, so the generator judged it stable and matched on it, and the replay
  raised `WindowNotFound` on its first line against a freshly opened copy.
  The resolver is now primed at `start()` with every window already open —
  one window list, 2ms measured — so a title that moves during a recording is
  seen to have moved and the generator reaches for the app id instead.

- **`environment.display` recorded the ambient `DISPLAY`,** not the one being
  recorded, so a recording made with `--display :99` put the developer's own
  `:0` in its header — the one fact that header exists to carry. It reads the
  environment it is handed now, which is also what the XWayland probe above
  asks about.

**Found here, fixed upstream in pyguitest, and the reason this run was worth
making:** `Session.focused()` cost **4.49s** a call on this desktop and
returned the **wrong element** — GNOME Shell's own `Main stage` toplevel,
because both the shell and the focused widget publish `FOCUSED` and a
root-first walk reaches the shell's first. That single call is the whole of
the "fell 6.2s behind live input" note in these recordings, and this tool's
own rule (see
[architecture.md](architecture.md#typing-goes-where-focus-is-not-where-the-pointer-is))
treats a toplevel answer as "no answer" — so the recorder paid 4.5s per run
of typed text to be told nothing. Scoped to the active window's application
it is 0.33s and correct; the lag in a comparable recording fell from 6.2s to
1.6s. See pyguitest's `docs/validation.md`.

**A gap this run measured rather than closed:** an app id is
protocol-specific, and a recording only ever sees the one it was made
through. gedit is `Gedit` through XWayland (the `WM_CLASS` class) and `gedit`
natively; gnome-calculator is `gnome-calculator` and `org.gnome.Calculator`.
Replayed against the same application running natively, the generated script
raised `WindowNotFound: no window matching app_id='Gedit'` on its first line
— loud, which is the design, but silent about *why*. pyguitest's
`expect_window` has always accepted several ids for exactly this, so the
generator now writes that advice into the script whenever the recording was
an XWayland one. Editing the line to `app_id=("Gedit", "gedit")` made the
same recording replay into a native Wayland gedit, document reading back.

**Not fixed, and known:** typed text still comes out as `gui.type_text(...)`
rather than a named field here, and correctly so — gedit's document area
publishes no accessible name, so there is nothing to address it by. Element
*geometry* for native Wayland clients is wrong rather than withheld upstream;
this tool is insulated from it by its own containment checks, which is why
clicks there degrade to coordinates rather than to wrong elements.

## Known gaps

- **The X11 live check records and regenerates but never replays**, where the
  Windows one replays by default. Every replay above was run by hand. A replay
  alone is not enough either: the worst bug of that day produced a script that
  validated clean *and* ran clean, and only reading an end state back -- the tab
  count, the calculator's display -- showed it doing the wrong thing.
- **No UI yet.** The design calls for a timeline, inspector and source preview;
  this is the CLI and the engine underneath it.
- **Two recordings have been made of a real desktop application** — a file
  manager, a text editor and a terminal on GhostBSD, and Windows 11's own
  Notepad (both above). The first found three bugs in one pass, all now fixed
  (see the CHANGELOG), the worst of which made any recording of an editor fail
  at replay; the second is what turned up Notepad restoring its previous draft
  session, which is a trap for testing that application rather than a defect
  here. The routine live check still uses two GTK windows on a private server,
  so this remains the thinnest-covered part of the tool.
- ~~The CI `live` job has never run on a GitHub runner.~~ **Closed:** it now
  runs green on `ubuntu-latest` on every push, so the Ubuntu package names and
  daemon paths are observed rather than reasoned.
- ~~**A double click on a named element is emitted on the element itself only
  where the installed pyguitest has `Element.double_click`.**~~ **Closed:** the
  floor is 0.10.1, and `Element.double_click` has been in since 0.10.0, so the
  `double_click_element` fallback and the probe that chose between the two
  spellings are gone. The element is looked
  up, its rectangle read *at replay*, and `element.double_click()` called
  there — the element stays the locator and the gesture stays one gesture. It
  still needs the element to have a trustworthy rectangle, and falls back to
  two `Element.click()` calls where it does not, which no toolkit is obliged
  to read as a double click. A triple click has no primitive and emits three
  clicks.
- **Element resolution needs `Capability.ELEMENT_GEOMETRY`**, added upstream
  for this and released in pyguitest 0.4.0. A pyguitest without it declares
  the capability nowhere, so the recorder degrades to coordinates and says so
  rather than failing. (The floor is higher than that, and set by
  `pyproject.toml` rather than by this capability — see the README's install
  section.)
- ~~**pyguitest cannot look a window up by application id**, only by title
  regex.~~ **Closed:** `find_windows`/`find_window`/`wait_for_window`/
  `expect_window` have taken an `app_id` since pyguitest 0.7.0, well inside
  the floor, and the generator uses it — a window whose title drifted comes
  out as `gui.expect_window(app_id=...)`. No helper is written into the script
  to do it, and none has been since the private helpers stopped being emitted
  in 0.2.0.
- **pyguitest's X11 backend could not be pointed at a display by argument.**
  Fixed upstream; the recorder still sets `DISPLAY` around the call, which
  works on any version.
- **GTK4 applications cannot be located by AT-SPI hit-testing.** Their
  widgets report a size at the origin and no position, so `element_at`
  returns the frame for essentially every point — measured across
  gnome-calculator (48 of 49 sampled points), baobab (42/49) and
  gnome-text-editor (48/49). Nothing above AT-SPI can recover a position
  that was never published, so **clicks** there come out as coordinates.
  Typed text is the exception and does get named, through focus rather than
  geometry — see
  [architecture.md](architecture.md#typing-goes-where-focus-is-not-where-the-pointer-is).
