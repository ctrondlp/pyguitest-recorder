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
| Checks (the check key, and what they generate) | run live, with a negative control -- see below |
| Script generator + API validation | tested against the installed pyguitest |
| Configuration (TOML, XDG, precedence) | tested |
| CLI (`--doctor`, `--regenerate`) | tested |
| Window/title-drift resolution | tested; drift fix found by a live recording |
| XRecord decoding, keysyms, teardown | tested against synthetic X events; text in nine scripts run live -- see below |
| The control set (combo, tabs, tree view, menus, radio, check) | run live against `scripts/gtk_probe_window.py` |
| Redaction of a real password field | run live, all three modes |
| XRecord capture of a real application | run live, here and on CI runners |
| win32 capture (low-level hooks, keysyms, `ToUnicodeEx`) | run live on Windows 11 -- see below |
| AT-SPI element resolution | run live against a real accessibility bus; same-process multi-window case run live -- see below |
| Focus-based targeting for typed text | run live; names a GTK4 field |
| Drag, window switching, save/regenerate | run live |
| Record-and-replay round trip on X11, with verification | run live on a private Xvfb -- see below |

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

## Run live on a private Xvfb, agent session (2026-09-23)

Not the developer's own desktop at any point: a private Xvfb (`:97`) and a
private accessibility bus, torn down after, the same pattern
`live-capture-check.py` uses — set up by hand rather than through that
script, specifically to close the gap this file had named since it was
written: **a real record-and-replay round trip, with the end state read
back**, which `live-capture-check.py` itself still does not do (see Known
gaps). `mate-calc` and `gedit` were the applications, both real, both driven
through pyguitest's own `x11`+`atspi` composite the way any caller would.

**A full round trip, with verification.** Typed Chinese text
(`xdotool type` into gedit), recorded, generated, and replayed into a fresh
copy of the application, read back through AT-SPI both times: `你好世界`
both before and after. A second round trip — `7 + 3 =` by button name in
mate-calc, then Help → About and back out of it — recorded, generated, and
replayed clean into a fresh `mate-calc`; see the popup finding below for
what that replay's coordinate fallback did and did not reproduce.

**Three real bugs, each reproduced live before it was fixed, each in code
this file or `docs/validation.md` had exercised before but never with
non-Latin-1 text, a second toplevel, or a display other than the ambient
one all three at once:**

- **Typing anything outside the base X keyboard layout was captured as
  nothing.** `xdotool type` of `你好世界` came back as four `key_press`
  events with keysym `0x0` and no text — `gui.tap_key("0x0")` four times in
  the generated script, not a character typed. Two causes, found together:
  the X11 backend's own keysym cache never picked up the `MappingNotify` a
  remap like this broadcasts (`xdotool` retargets an unused keycode to type
  a character outside the layout, exactly how several input methods do it
  too), and `_printable` never covered ICCCM's direct-Unicode keysym block,
  which is exactly where a character with no legacy X keysym of its own —
  all of CJK included — lands. Fixed in `backends/x11.py`; the same
  recording now produces `gui.type_text("你好世界")`, confirmed replaying
  correctly above. Same shape of bug as the Windows `VK_PACKET` fix, on X11.

- **A click on a dialog resolved to a button in the window underneath it.**
  mate-calc's Help → About opens a second toplevel, same process, almost the
  same size and position as `Calculator`. The window was named correctly
  (real X stacking order), but the accessible hit test has no concept of one
  window of a process being stacked above another window of the *same*
  process, and answered with `Calculator`'s own `=` button — silently: pid
  matched, so the existing cross-process check waved it through. The
  element's own `path` is what catches it — its toplevel ancestor is named
  `'Calculator'`, not the resolved window's `'About MATE Calculator'` — and
  the resolver now checks that too, degrading to a coordinate rather than
  naming the wrong widget. See `resolver.py`'s `_other_toplevel_of`.

- **A recording made on a private Xvfb described the developer's own MATE
  session.** `scoped_environment` strips `WAYLAND_DISPLAY` so an X11
  recording is not called a Wayland one, but never stripped
  `XDG_CURRENT_DESKTOP` and its two cousins — variables scoped to the login
  session, not to any one display. `Recorded on: x11 (other, MATE)` for a
  bare Xvfb with no window manager and nothing resembling MATE running on
  it. Fixed by dropping those three whenever `--display` names a server
  other than the ambient one; left alone recording the desktop you are
  actually sitting in front of.

**A near-miss the harness itself caused, worth recording so the next live
session does not repeat it.** `at-spi2-registryd` injects a synthetic click
(`Atspi.generate_mouse_event`, what `Element.click()` uses on X11) into
whichever display it was *started* on — read from its own environment at
launch, not from anything a caller passes later. An ad hoc test script for
this session started that daemon without first pointing `DISPLAY` at the
private Xvfb, so it inherited the ambient one — the real desktop — and a run
of `Element.click()` calls almost certainly landed real synthetic clicks on
the developer's actual screen, near the top-left, at coordinates sized for a
420×261 calculator window. `live-capture-check.py`'s own `main()` already
gets this right (`os.environ["DISPLAY"] = display` before
`start_a11y_bus()`, with a comment saying exactly why); the ad hoc script
had not copied that ordering. No consequence this time — confirmed with the
developer, who was away from the machine — and every script for the rest of
this session set `DISPLAY` first. Worth a line here because nothing about
"clicked at the wrong coordinate" would have looked wrong from inside the
recording itself: this is a way the *test harness* touches the real desktop
that has nothing to do with what the recorder under test does.

### The control set, against a purpose-built GTK window

`scripts/gtk_probe_window.py` was written for this and closes the gap the
Known-gaps list had named for months: the routine live check drove two bare
GTK windows, so a combo box, a notebook tab, a tree-view row and a real menu
had never been recorded on this platform at all. Recording one click on each
found three separate bugs in an afternoon, which is the argument for the
window existing.

**Two of them are above and in the CHANGELOG** (the drop-down
misattribution, the `NO_AT_BRIDGE` verdict). **The third was upstream:**
every control on a notebook page resolved to the `page tab list` container
and came out as a bare coordinate, because GtkNotebook publishes a page's
contents under the `page tab`, whose own rectangle is just the tab label --
so the accessible tree stops nesting geometrically exactly where pyguitest's
hit test assumed it would. Measured at the `Save` button's own centre
(360, 234): `page tab list.getAccessibleAtPoint` answered `None` and
`page tab.getAccessibleAtPoint` answered the page's content correctly, so
one step through the tab was the whole fix. Fixed in pyguitest; see its
`docs/validation.md`.

**Confirmed working afterwards, one click each, all named:**
`gui.text_field("Name").set_text("Ada")`, `gui.button("Save").click()`,
`gui.checkbox("Enabled").click()`,
`gui.element(role=Role.RADIO_BUTTON, name="Large").click()`,
`gui.dropdown("Size").click()` followed by `gui.menu_item("Gamma").click()`,
`gui.element(role=Role.MENU, name="File").click()` followed by
`gui.menu_item("Open").click()`. Replayed into a fresh copy of the window and
read back through AT-SPI: the entry reads `Ada`, `Enabled` is checked,
`Large` is selected and `Small` is not — none of which was true before.

**Three controls are named in the recording and still emit a coordinate,
correctly.** A spin button, a notebook tab and a tree-view row offer AT-SPI no
click or press action, so the generator falls back to a coordinate and says
which element it means and why: *"'Count' was named, but offered AT-SPI no
click or press action, so this has to stay a coordinate"*. That is the
designed ladder working, not a gap.

### The check key, run live at last

The one row in the table above that still read "tested; not yet run live".
Pointing at a widget and pressing `ctrl+1` recorded three assertions against
the probe window — `gui.expect_text(role=Role.TEXT, name="Name",
equals="Ada")`, `gui.expect_checked(role=Role.CHECK_BOX, name="Enabled",
checked=True)` and `gui.expect_showing(role=Role.PUSH_BUTTON, name="Save")` —
which replayed clean against a fresh window.

Clean is not the same as meaningful, and this project has been caught by that
distinction before, so the assertions were run again with every *action* line
commented out and only the assertions left. Against a fresh window they fail,
as they must: `AssertionError: expected 'Name' to read 'Ada', but it reads
''`. The checks assert rather than merely run.

### Redaction, against a real password field

The probe window publishes a real `password text` widget, which is the one
signal redaction keys off and the one thing no unit test can produce. All
three modes behave as documented, with `hunter2swordfish` typed into it:

- default — the password becomes `os.environ["SECRET_1"]` in the script and
  ordinary text stays literal;
- `--sensitive` — *both* fields are redacted, `SECRET_1` and `SECRET_2`;
- `--no-redact` — the literal is written, which is what it is for.

And the warning in the README and `docs/recipes.md` is **accurate, not
cautious**: the saved `--save-session` JSON carries the plaintext either way,
sitting next to the `"sensitive": true` flag that marks it. Redaction happens
at generation time, exactly as documented. A `.json` from a session that
touched real credentials is a credential file.

### Text beyond Latin-1, across nine scripts

The Chinese failure drove the keysym fix; these are the other shapes the same
path has to survive. Typed into the probe window and read back from the
widget, then compared against what the recorder captured — all nine matched,
and each generated the right `set_text` call:

| | sent | captured |
|---|---|---|
| Chinese | `你好` | ✓ |
| Arabic (RTL) | `مرحبا` | ✓ |
| Hebrew (RTL) | `שלום` | ✓ |
| Emoji (astral plane) | `😀🎉` | ✓ |
| Latin-1 accents | `café` | ✓ |
| Latin Extended | `Ċħŗ` | ✓ |
| Greek | `αβγ` | ✓ |
| Cyrillic | `Привет` | ✓ |
| Mixed | `aA1 你好 é` | ✓ |

The astral-plane emoji is the interesting one: a single keysym above the
BMP, and the case a naive UTF-16-shaped decoder splits. Greek and Cyrillic
are the other: both have legacy X keysyms of their own, and `xdotool`
nevertheless sends them through the direct-Unicode block, so the same branch
handles them.

### A modal dialog: found, reproduced, and **not** fixed

The one finding of this run left open, and the most serious. Recording
"click `Confirm`, then click `OK` in the modal dialog it raises" produces a
script that **validates clean, runs clean, and does the wrong thing** — the
failure mode this file keeps warning about, caught again:

```
press 1 at (360, 318)  -> window 'Probe Window', element None
press 2 at (405, 318)  -> window 'Probe Window', element button 'Confirm'
```

Press 2 landed on the dialog's `OK` button, whose extents were measured live
at (362, 301, 86, 34). It was recorded as a click on **`Confirm`** — the
button of the *parent* window that happens to sit under the same point —
because `Confirm` is at window-relative y 218 and the window origin is
(100, 100), so the two overlap exactly. The generated script therefore opens
the dialog twice and never dismisses it, which the replay confirms: the
window list afterwards still has `Confirm Action` in it.

**The cause is structural, not lag.** A click that dismisses a window is
always consumed after that window has gone: the application processes the
press, `dialog.run()` returns and the dialog is destroyed, all in
milliseconds and all independently of the recorder, which reads a stream from
another process. By the time the press is resolved there is no dialog left in
the tree to resolve it against, so the hit test answers with whatever is
underneath. It is the same shape as the menu problem `_popup_at` exists for —
"the press is consumed after the popup has closed" — but a dialog is an
ordinary toplevel, and nothing remembers those.

**The toplevel check did its half.** Press 1 *was* refused rather than
misnamed: by its consume time the dialog had already opened over it, so
`element_at` answered with a dialog element, and the check added this same
day caught the mismatch and degraded to a coordinate with a note. Without it
press 1 would have been confidently wrong as well.

**Why it is not fixed here.** Detecting "a window that covered this point has
since vanished" needs a window list per click, and window listing is the
expensive question this resolver has already been tuned hard to avoid — the
motion cache and `_recent_window` exist for exactly that reason. A cheaper
route is visible and worth designing properly rather than landing in a hurry:
the resolver *already saw* the dialog, at press 1's resolve, in the very
element it refused. Remembering the toplevels named in elements it has
already looked at, with their rectangles and for a second or two, would cost
no extra round trip and would let press 2 be refused the same way press 1
was. Refusing is enough to fix the replay, too, and that is the part worth
saying: the coordinate the click would fall back to is window-relative to the
parent, the dialog opens in the same place, so a refused-and-coordinate script
dismisses the dialog correctly where today's confidently-named one does not.

**Measured, not chased further.** mate-calc's own numeric display answered
empty through both AT-SPI's `Text` and `Value` interfaces after a real
`7 + 3 =` driven the same way the recording above drove it, on this
`mate-calc` build in this environment — a question about what this
particular build publishes, not about anything in this repository, and not
pursued past confirming it is not this project's own resolution or replay
at fault. Separately, the About dialog's `Close` button has no reliable
accessible action here (the toplevel-mismatch fix above only proves the
*wrong* element is no longer named, not that a right one exists to name
instead), so its click degrades to a coordinate the same as any other
last-resort locator — and a repeat run under otherwise identical conditions
left that particular dialog open, consistent with the coordinate not
tolerating a rendering difference as small as a few pixels, which is a
known, general property of coordinate fallbacks and not a new finding.

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

- **`scripts/live-capture-check.py` itself records and regenerates but still
  never replays**, where the Windows one replays by default. Every replay in
  this file, including the private-Xvfb round trips above, was run by hand
  rather than by that script. A replay alone is not enough either: the worst
  bug of the GhostBSD day produced a script that validated clean *and* ran
  clean, and only reading an end state back -- the tab count, the
  calculator's display -- showed it doing the wrong thing, which is why every
  hand-run replay above reads a real end state back rather than stopping at
  a clean exit.
- **A click that dismisses a modal dialog is recorded as a click on whatever
  is underneath it.** Found, reproduced and characterised on 2026-09-23 (see
  the modal-dialog section above), with a replay that leaves the dialog open
  to prove the consequence. Not fixed: detecting it cheaply needs the
  resolver to remember toplevels it has already seen in resolved elements,
  rather than taking a window list per click.
- **No UI yet.** The design calls for a timeline, inspector and source preview;
  this is the CLI and the engine underneath it.
- **Two recordings have been made of a real desktop application** — a file
  manager, a text editor and a terminal on GhostBSD, and Windows 11's own
  Notepad (both above). The first found three bugs in one pass, all now fixed
  (see the CHANGELOG), the worst of which made any recording of an editor fail
  at replay; the second is what turned up Notepad restoring its previous draft
  session, which is a trap for testing that application rather than a defect
  here. ~~The routine live check still uses two GTK windows on a private
  server, so this remains the thinnest-covered part of the tool.~~
  **Narrowed 2026-09-23:** `scripts/gtk_probe_window.py` now puts a real
  control set on that server -- combo box, notebook, tree view, menu bar,
  radio pair, check box, spin button, password field -- and recording against
  it found three bugs in an afternoon. What is still thin is *real
  applications*: a purpose-built window cannot surprise you the way a shipped
  one does, which is the whole point of the two recordings above.
- ~~The CI `live` job has never run on a GitHub runner.~~ **Closed:** it now
  runs green on `ubuntu-latest` on every push, so the Ubuntu package names and
  daemon paths are observed rather than reasoned.
- ~~**A double click on a named element is emitted on the element itself only
  where the installed pyguitest has `Element.double_click`.**~~ **Closed:** the
  floor is 0.11.0, and `Element.double_click` has been in since 0.10.0, so the
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


## The win32 live check's verification, corrected (2026-09-23)

Two consecutive runs of `scripts/win32-live-capture-check.py` on the Windows
box failed at *different* points -- `role='entry'` one run, `role='list item',
name='Gamma'` the next -- with capture, generation and `validate` clean both
times. The cause was the check's own verification, in three parts, all fixed:

- **A tab's controls are not in the UI Automation tree at all while another
  tab is selected.** Measured on the probe window: with List showing, the
  entry is absent even from a search that asks for invisible elements, and it
  reappears when General is selected again. The verifier read the entry
  *before* selecting any tab, so it depended on whichever page the replayed
  script happened to leave showing. Every control is now read with its own
  tab selected first.
- **A flat 0.6s after a tab click is a guess.** The driver and the verifier
  now wait for the page's own controls to appear, which is the only signal
  that the click landed -- `_select_tab` in the check.
- **A failure now says what the window was publishing.** That wait first
  reported "the 'List' tab never showed its 'list item'" and nothing else;
  with the tree in the message the state named itself -- General still
  showing, `push button:'Restore'` (the replay leaves the window maximized) --
  which is what pointed at the click and the page rather than at the recorder.

**Still open, in two parts.** First, pyguitest's `element()` intermittently
misses an element that is present: the same query that fails one moment
answers the next, and `find_elements()` on the same window finds what the
lookup missed (measured on this box and on Windows 11; see pyguitest's
`docs/validation.md`). The check retries every lookup now, which is enough for
that. Second, and not explained by it: with the retries in place the
verifier's click on the List tab still does nothing *in the check's own
process*, repeatably, while the identical click from a separate process works
every time -- including with the window maximized, with the pointer parked
first, on the already-selected tab first, with two sessions in the process, and
after a Recorder has been started and stopped in that process. Each of those
was measured and eliminated. The check now reports the state it finds when this
happens (`showing: page tab 'General', entry:'', ... push button:'Restore'`),
which is what stopped it looking like a recorder failure -- but the cause is
still open, and instrumentation inside the check is where the next attempt
should start.

**Also found on the X11 side while doing this, upstream:** replaying a
generated script against the GTK control-set window, the entry and the check
box landed correctly and then `gui.menu_item("Gamma").click()` -- an item in an
open combo-box popup -- died with
`ValueError: Attempting to generate a mouse event at negative coordinates:
(-2147483647, -2147483647)` from dogtail, via `atspi.py`'s `self.node.click()`.
AT-SPI's INT_MIN sentinel for "not showing", handed to the injection layer
instead of being refused. See pyguitest's `docs/validation.md`.

**Also worth knowing for these harnesses:** `move_window` and
`resize_window` are EWMH client messages, so on the bare Xvfb the live check
starts they do nothing at all rather than failing. Nothing in the generator
emits them today, so no recording is affected -- see pyguitest's
`docs/validation.md` for the measurement.



## Radio groups and a nested tree, and what they found (2026-09-23)

Both probe windows grew the two controls whose *structure* is the point: two
radio groups that must not see each other, and a tree with a branch that
starts collapsed. `scripts/gtk_probe_window.py` gained a second radio group
(Compact/Comfortable, separate from Small/Large) and a `Tree` page, and
`scripts/win32_probe_window.py` a `Groups` page with two radio groups and a
`SysTreeView32` (Documents -> Reports -> Q1/Q2).

**The Windows tree worked end to end**, by name and by mouse: a double-click
opened a branch (`page tab 'Groups'` then `tree item 'Documents'`, `'Reports'`,
`'Q1'` all resolved), `element.do_action("expand")` opened one without any
input at all, and the nested `Q1` came back `selected=True` after a replay
that only ever sent clicks. GTK's did not: `Gtk.TreeView` publishes a row as a
`table cell`, and neither a double-click (which only activates the row), nor
`Right`, nor a click on the expander left the child rows in the accessible
tree -- so the GTK nested-tree *drive* is still to do, while the Win32 one is
covered. Note also that a hidden `Gtk.Notebook` page keeps its rows in the
accessible tree, where a hidden `SysTabControl32` page's controls vanish from
it; the two platforms disagree about what "not showing" means.

**A `BS_GROUPBOX` swallowed the radios**, which is why the first version of
this found nothing: with one, a click at a radio's *own* reported centre
resolved to the group box (`push button 'Priority'` for a click aimed at
`High`) and the radio never moved -- measured, and the reason the probe now
uses plain `STATIC` captions with `WS_GROUP` for the separation. Both groups
are independent: `select()` on `High` moved the priority group and left
Safe/Fast alone, and `select()` on `Fast` moved the mode group with `High`
still selected.

**The generator now prefers a name for a selection as well as for a click.**
A radio button and a tree item publish `select` and no `click`, so a recorded
click on one used to fall all the way to a coordinate -- the one locator that
breaks when the window moves. `ElementRef.selectable` mirrors `clickable`, and
the click ladder now tries `click()`, then `select()`, then a coordinate; the
`locators` setting still says to favour coordinates when that is what is
wanted, and `actions is None` (a recording made before the field existed)
keeps the old rendering. Six unit tests in `tests/test_generator.py` cover it,
including the repeated-click and unknown-actions cases.

**Still open from the same run.** The label the replayed tour ends on reads
`'clicked 3'` where a clean replay must read `'menu 5'`, and `'menu 4'` in
another run: the recorded clicks that come *after* the replay drags and
maximizes the window do not all land, which is exactly the shape of failure
this check exists to catch. And the replay of a generated script that clicks
the Groups tab by coordinate failed to switch it, so the tree items were not
there to find -- the same "click lands nowhere" family the focus fix narrowed
but did not close.

## The GTK nested-tree gap, closed (2026-09-24)

The previous run left "the GTK nested-tree *drive* is still to do" open.
pyguitest gained `Element.expand()`/`collapse()`/`expanded`/`expandable`
(both backends; see that repository's changelog) specifically to close it,
and this generator now prefers them over `double_click()` for any element
that reports one.

**Confirmed live, both directions of the bug.** Recording a real
double-click on the GTK probe window's `Documents` row (private Xvfb,
`gtk_probe_window.py`) produced `gui.element(role=Role.TABLE_CELL,
name="Documents").expand()` -- and a screenshot of the raw double-click
itself showed the row merely *selected* (highlighted, still collapsed),
confirming again that `double_click()` was never going to work here.
Replaying the generated script against a fresh window actually opened the
row: `Reports` and `Notes` appeared as children, screenshotted after
switching back to the Tree tab. `validate()` on the generated script: `[]`.

**Two bugs found and fixed on the way, both regressions the new field would
otherwise have caused silently:**

- `ElementRef`'s dict round-trip (`_rebuild` in `model/events.py`) lists
  fields by hand rather than reading them off the dataclass, so the new
  `expanded` field was reconstructed as `None` on every `--regenerate` and
  every `--save-session` reload, regardless of what was recorded --
  `to_dict` (which uses `dataclasses.asdict`) carried it out fine, only the
  read-back silently dropped it. `test_expanded_round_trips_through_to_dict_and_back`
  in `tests/test_model.py` exists because `save_button`, the fixture the
  existing round-trip test used, carries no `expanded` state either way and
  would not have caught this.
- `test_resolver.py`'s `FakeElement` had no `.expanded` attribute at all;
  `_describe_element` now reads it unconditionally, so every resolver test
  using the default fixture would have raised `AttributeError` the moment
  this landed. Given a default of `None`, same as pyguitest's own answer for
  "not expandable."

23 new tests total, across `pyguitest/tests/test_atspi.py`,
`pyguitest/tests/test_uia_backend.py`, and this repository's
`tests/test_generator.py`, `tests/test_model.py` and `tests/test_resolver.py`.

## `selectable` was blind on GTK the whole time it claimed to be fixed (2026-09-24)

Found doing exactly what was asked for after the above: record a session,
replay the generated script while recording *that*, and see how closely the
two agree. A varied sequence (type text, click a button, click a radio,
click a page tab, expand a tree row) recorded against the live GTK probe
window surfaced it before the second recording was even needed -- the raw
JSON from the first recording showed the `Tree` page tab's `actions` as `[]`,
and the "Radio groups and a nested tree" entry above's own `select()`
feature reads `selectable` from exactly that field. It was never wrong on
Windows because Windows 11, the only platform it was measured against, was
the one platform where it happened to be right.

`ElementRef.selectable` now reads pyguitest's own `Element.selectable`
(captured in `_describe_element`, carried through the JSON round trip, both
new gaps of the same shape the `expanded` field opened two sections up and
caught the same way: a fixture or round-trip test with nothing to catch a
silently-dropped field). Confirmed live: the `Tree` tab recorded as
`gui.element(role=Role.PAGE_TAB, name="Tree").select()` where it used to
fall to a coordinate, and replaying it against a fresh window actually
switched the tab.

**Worth doing again.** The record/replay/record comparison did not even need
its second half here -- reading the first recording's own JSON was enough
once the question was "does the actions list actually say what I assumed
it says." The second half (replay while recording, diff the two) is still
worth running as its own pass; nothing in this session got to it.

## Ran the second half, found a pyguitest bug (2026-09-24)

Recorded session A (click a field, type, click Save, click a radio, click a
page tab, double-click a tree row) against a live GTK window, replayed the
generated script against a *fresh* copy of the same window while a second
recording watched, then compared. The raw event counts diverged exactly as
expected -- session B saw `move_mouse_naturally`'s real pointer motion and
nothing else, since `Element.click()`/`select()`/`expand()`/`set_text()` all
act through the accessibility bus with no X input to see, by design. That
part is not a bug.

Checking whether B's replay actually *reproduced* A's end state was: the
Name field and the tree both matched, but the `Large` radio and `Enabled`
checkbox stayed unchecked, though the script ran with no error. Isolated
outside either recorder: `element.click()` on that radio returned normally
having changed nothing; `element.do_action("click")` on the identical
element toggled it every time. This is a pyguitest bug, not this
repository's -- `AtspiBackend.Element.click()` tried dogtail's own
coordinate click first and the action interface only as a fallback from an
exception, and GTK reports a checkbox/radio's extents as its *whole row*
(494px, to the window edge) rather than the small toggle the row reacts to,
so the coordinate click lands in dead space and dogtail raises nothing.
Fixed there: the action interface is now tried first wherever one is
published, coordinates only where none is. See that repository's changelog;
nothing in this repository needed to change for it.

This is what the technique was for -- a bug neither recorder's own test
suite would ever catch, since both assert what a script *says*, not what it
*does* once replayed. Worth keeping as a standing check, not a one-off.

## Taken to the Windows box, live (2026-09-24)

Same techniques, against a real Windows 11 desktop over SSH -- reached
through a scheduled task run with `/it` (only when logged on), since plain
SSH lands in Session 0 and cannot see, let alone drive, the interactive
desktop's UI Automation tree at all; every script below ran that way.

**`clickable` was checking the wrong vocabulary on the whole platform.**
Recording a session against the win32 probe window and reading the script
it generated: `gui.checkbox("Enable feature")` was named and correctly
resolved, and its click still rendered as a bare coordinate. Isolated
directly: every interactive UIA control's own `actions` -- a push button, a
checkbox, a page tab -- includes `invoke` and/or `do default action`, never
`click` or `press`, so `ElementRef.clickable`'s AT-SPI-shaped check
(`"click"`/`"press"` only) answered False for everything on this platform.
Fixed; see the changelog. Confirmed live afterward: the same checkbox now
records as `gui.checkbox("Enable feature").click()`.

**Still open: a double click on a *collapsed* tree row recorded as
`collapse()`.** Isolated to a single double-click event, nothing else: read
`Documents.expanded` directly beforehand (`False`), recorded one
double-click on it, and the JSON `_describe_element` wrote for that same
event carries `"expanded": true`. Win32's native `SysTreeView32` toggles a
row synchronously as part of handling the click, and `_describe_element` is
called from `_element()`, which -- deliberately, per the GTK dropdown-popup
entry above ("a press is resolved when it is *consumed*, which is after the
application has reacted to it") -- resolves after the application has
already reacted, which is *why* popup attribution works at all. That same
deliberate lateness means `expanded` is read after Windows has already
toggled the row, not before, so `_expand_click` reads the row's state
*after* the click it is trying to describe and picks the opposite of the
right verb every time on this platform. GTK's `"expand or contract"` is a
toggle with no OS-side pre-emptive state change, so the equivalent read
there is not stale the same way -- this looks Windows-specific, though
nothing here has tried to break it on GTK on purpose either.

Not fixed this pass: resolving earlier would need to not disturb the
popup-attribution timing the rest of `_element`/`_describe_element` depends
on, which needs more than the scope of a live-testing session to get right.

Two mitigations considered and ruled out without touching anything: reading
`expanded` inside the `WH_MOUSE_LL` hook callback directly contradicts that
file's own documented design (the callback must stay minimal or risk being
silently unhooked at the 300ms mark -- see its module docstring); boosting
the resolver thread's OS priority would not have helped at all, since the
delay is COM/UI-Automation round-trip latency, not scheduling contention.

One mitigation tried and measured live: pyguitest's `uia.Element` was making
several *redundant* `GetCurrentPattern` round trips per description --
`expanded`/`expandable`, `checked`/`checkable` and `selected`/`selectable`
each separately re-asking about a pattern `actions` had already asked about
once each, for all seven patterns, to build its own list. Caching the
pattern *lookup* per Element (not any value read through it, which still
goes back to UIA every time) is a real fix in its own right -- see
pyguitest's changelog -- and cuts real round trips from one `_describe_
element` call. Retested live afterward, isolated exactly as above: still
`"expanded": true` for a row that read `False` immediately beforehand.
Removing a few redundant calls was not enough to win a race against a
synchronous, same-process native toggle; the redundant calls were not
where most of the time was going. The bigger fix -- UI Automation's own
`IUIAutomationCacheRequest`, batching every property `_describe_element`
wants into the *original* hit-test round trip, so there is no second
round trip to lose the race during at all -- is real engineering (new COM
interface bindings, no way to test it without a live Windows box) and
stays out of scope for a live-testing pass. A pre-click snapshot of just
`expanded`, taken at raw-event time rather than at describe time, remains
the other untried shape of a fix.
