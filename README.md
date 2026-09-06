# pyguitest-recorder

Record desktop GUI activity and generate [pyguitest](https://github.com/ctrondlp/pyguitest)
scripts from it.

The point is not to replay a macro. It is to turn what you did into test code
you would have been willing to write by hand — so a recorded click on a button
becomes

```python
gui.button("Save").click()
```

rather than `gui.move_mouse(180, 90); gui.click()`, which stops working the
moment the window moves, the theme changes, or someone adds a toolbar item.

## Status

**Early, but the engine is complete and every path has now been run.**
Following pyguitest's own convention of saying which parts are verified
and how:

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
| AT-SPI element resolution | run live against a real accessibility bus |
| Focus-based targeting for typed text | run live; names a GTK4 field |
| Drag, window switching, save/regenerate | run live |

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
same wall this recorder describes below, met from the other side.

## Why recording is X11 only

This is a property of the platform, not a missing feature.

pyguitest classifies reading global keyboard or pointer state as
`Capability.INPUT_STATE_QUERY`, tier 6 — *"deliberately prevented"* — and
describes it as what a keylogger reads. That is exactly what a recorder must
do. No ordinary Wayland client can observe another client's input on any
compositor, and no backend added later changes that: injection on Wayland took
portals, libei and per-compositor IPC, but observation is the thing compositors
exist to prevent.

X11's RECORD extension is the exception, so it is the first and only capture
backend. Under a Wayland session it reaches XWayland clients and nothing else —
and says so in the recording and in the generated script's header, rather than
producing a file with silent gaps.

The one part that *is* portable is element resolution: AT-SPI answers "what is
under this point" identically under X11 and Wayland. An AT-SPI event-based
acquisition layer is the plausible route to a Wayland recorder, and the event
model here is deliberately free of X11 vocabulary so that layer can feed it.

### Recording and replaying are different questions

**Recording** needs X11 or XWayland, for the reason above. Under a Wayland
session that means XWayland clients and nothing else, which the recording and
the generated script's header both say.

**Replaying** is pyguitest's problem, not this tool's, and it goes further —
pyguitest injects on Wayland through portals, libei and per-compositor IPC.
But how far a *particular* script gets depends on what is in it, and that is
decided when it is recorded:

| What the script contains | How it replays on pure Wayland |
|---|---|
| `gui.button("Save").click()` and other named elements | The portable case. AT-SPI answers the same under X11 and Wayland |
| `gui.move_mouse(x, y)` and window-relative coordinates | Needs pointer and window-geometry capabilities the compositor may not grant |
| `gui.activate_window(...)`, `wait_for_idle(win.pid)` | Compositor-tier: available on some desktops, absent on others |

So a recording that resolved to elements is close to portable, and one that
came out as coordinates is close to X11-only. That is the same reason element
resolution is worth the trouble, stated from the replay end — and it is why
the notes explaining *why* a script came out as coordinates are worth reading
before assuming it will run somewhere else.

Nothing here fails silently: the generated `gui.require(...)` preamble names
the capabilities the script actually uses, so replaying it somewhere weaker
raises a typed exception on the first line rather than clicking into empty
space halfway through.

## Install

**Not on PyPI yet** — nothing has been released. Until it is, use the clone
below. Once it is:

```sh
pip install 'pyguitest-recorder[x11,atspi]'

pyguitest-recorder --doctor      # start here: can this machine record?
```

`x11` brings `python-xlib`, which capture needs. `atspi` is what lets a click
be recorded as a name instead of a coordinate — see the caveat below, because
that one is not pip's to satisfy alone.

### From a clone

```sh
git clone https://github.com/ctrondlp/pyguitest-recorder
cd pyguitest-recorder
pip install -e '.[x11,atspi,dev]'

pyguitest-recorder --doctor
```

The editable install is what puts the `pyguitest-recorder` command on your
path. **To run without installing anything** — trying a branch, or keeping
the command off your path — this package uses a `src/` layout, so the source
directory has to be on the import path:

```sh
PYTHONPATH=src python -m pyguitest_recorder --doctor
```

Every flag below works identically that way. It is not a way to skip the
dependencies, though: `pyguitest` and `python-xlib` still have to be
importable. `scripts/live-capture-check.py` needs neither form — it puts
`src/` on the path itself and runs straight from a checkout.

### The part pip cannot do for you

`dogtail`, which element resolution goes through, **declares no dependencies
of its own**: PyGObject and pyatspi have to come from your distribution. Miss
them and nothing errors — `--doctor` reports element resolution off and every
click in every recording comes out as a coordinate, which looks like the
recorder being bad at its job rather than a missing package.

On Fedora `python3-gobject python3-pyatspi at-spi2-core`; on Debian and Ubuntu
`python3-gi python3-pyatspi gir1.2-atspi-2.0`. pyguitest's
[install guide](https://github.com/ctrondlp/pyguitest/blob/main/docs/install.md)
carries the full table, including Arch, openSUSE and FreeBSD.

**pyguitest 0.5.0 or newer is required outright.** Generated scripts call
`gui.button(...)`, which finds nothing on a current at-spi2 before 0.5.0 —
that release is where role lookups learned to accept both spellings of a
renamed role. 0.5.0 is also where `Window.app_id` starts being populated on
X11, which is what lets a window whose title drifts still be found. Earlier
versions lack `element_at`/`extents` and `double_click` as well.

## Use

```sh
pyguitest-recorder --doctor              # can this machine record? why not?
pyguitest-recorder -o login_test.py      # record until Escape, Escape
pyguitest-recorder -o login_test.py --save-session rec.json   # keep both
pyguitest-recorder --regenerate rec.json -o out.py   # re-render, no recording
```

Without `-o` the script goes to stdout and the summary to stderr, so a
redirect works too. Keeping the `.json` is worth it: `--regenerate` re-renders
an old recording under whatever inference rules exist now, without recording
it again.

Uninstalled, from a checkout, put `src/` on the import path and call the
module — every flag is the same:

```sh
PYTHONPATH=src python -m pyguitest_recorder --doctor
PYTHONPATH=src python -m pyguitest_recorder -o login_test.py
PYTHONPATH=src python -m pyguitest_recorder -o login_test.py --save-session rec.json
```

Recording stops on **Escape pressed twice**, not only Ctrl-C — a recorder you
can only stop from its own terminal is one you cannot stop while driving a
full-screen application. **Ctrl+F1** records a check on whatever the pointer is
over; see [Checks](#checks-what-makes-it-a-test).

Both are configurable (`--stop-key`, `--stop-presses`, `--check-key`, or the
config file) and both take the same chord syntax — `Escape`, `ctrl+Escape`,
`ctrl+shift+F1`. They match *exactly*, so binding `ctrl+F1` leaves
`ctrl+shift+F1` to the application being recorded.

The defaults are chosen around what laptops actually have. Pause is not on
many keyboards any more, so Escape stands in — twice, because a single Escape
belongs to the application, and a press that does not complete the run is
passed through and recorded like any other key. That keeps "press Escape to
close the dialog" recordable. A key nothing else wants can drop the repeat:
`--stop-key Pause --stop-presses 1`. The check key carries a modifier for the
same reason — a bare F9 is a screenshot key on some laptops, and a bare F1 is
help nearly everywhere.

The generated file opens with a docstring naming the recorder and pyguitest
versions, the desktop it was recorded on, and a pointer to the notes at the
end. `--no-header` drops it; `--header TEXT` (or a multi-line `header` in the
config file) puts your own text above it, for a licence or a ticket number —
the provenance block still follows, since setting a header should not
silently drop the record of what made the file.

### What comes out

```python
"""Generated by pyguitest-recorder. Edit freely.

Profile:     pyguitest-0.5
Recorded on: x11 (mutter)
"""

import pyguitest
from pyguitest import Capability, Role


def main() -> None:
    """Replay the recorded interaction."""
    with pyguitest.connect() as gui:
        gui.require(
            Capability.ELEMENT_ACTION,
            Capability.ELEMENT_TREE,
            Capability.WINDOW_ACTIVATE,
        )

        editor = gui.wait_for_window("Example", timeout=10)
        gui.activate_window(editor)
        gui.text_field("Name").set_text("Ada")
        gui.button("Save").click()


if __name__ == "__main__":
    main()
```

Three things about that output are deliberate.

**Elements lead, coordinates follow.** Each click is resolved at record time
against AT-SPI. A named element becomes a named call; failing that, a
window-relative coordinate; failing that, an absolute one. `--absolute-coordinates`
forces the bottom of that ladder.

**Scripts declare what they need.** Every file opens with `gui.require(...)`
naming the capabilities it uses, so a recording made on X11 and replayed
somewhere weaker fails on the first line with a typed exception instead of
halfway through with a click that went nowhere.

### When it refuses to name an element

Element resolution is the reason this tool exists, so it is worth saying when
it declines. An accessible element is dropped, and the click falls back to a
coordinate, whenever the answer cannot be corroborated:

- its process is not the process owning the window under the same point;
- no window on the recorded display accounts for it at all;
- its rectangle does not fit inside that window;
- **its own extents do not contain the point it was looked up at;**
- the answer is the *toplevel itself* rather than anything inside it, which
  is what a toolkit placing its widgets in window coordinates looks like
  from outside — the frame's rectangle checks out and none of its widgets'
  do. `gui.element(role=Role.FRAME, …)` is never the recorded click.

The last two are not hypothetical. A GTK4 dialog (zenity, Fedora 45) reports
*every* widget at the origin — `Cancel` and `OK` both at `(0, 0, 120, 44)` —
so `get_accessible_at_point` returns the same `label` for every point in the
window. Nothing errors; the recorder is simply told something false, and
without this check it emits `gui.element(role=Role.LABEL, name=…).click()` for
a click that was nowhere near the label. A coordinate that works beats a
named element that does not.

pyguitest now applies the same containment rule inside `element_at`, so what
comes back from a toolkit like that is the *frame* rather than the label —
which is why the toplevel check exists here as well. Both are needed: one
catches the widget that lies, the other catches what is left when every
widget has been caught.

Every one of these is recorded as a note, printed when the script is
generated, and written into the generated file's docstring — because "why is
this script all coordinates?" is the first thing its reader asks. The
rectangle checks are skipped where any screen is scaled, since AT-SPI extents
and window geometry are then not reliably in the same units.

If the answer to "why is this script all coordinates?" is your own
application, [docs/testable-guis.md](docs/testable-guis.md) is the document to
hand its developers: what to publish so a click can be recorded by name, and
how to assert it in their own suite.

### Typing goes where focus is, not where the pointer is

Hit-testing is not the only question worth asking, and on GTK4 it is not a
question that can be answered at all — every widget there reports its size at
the origin with no position, so `element_at` returns the frame for essentially
every point (measured across gnome-calculator, baobab and gnome-text-editor).

Keyboard focus is unaffected by that, because it involves no geometry, and it
is measurably reliable on a bare X server: `focus_tracking_works()` is true
and Tab walks real widgets. So a run of typed text asks the toolkit what has
focus, and a recording gets `gui.text_field("Name").set_text("Ada")` where it
would otherwise have got `gui.type_text("Ada")` — including for a field
reached by Tab, by an accelerator, or focused by the application itself, none
of which the pointer sees.

It is asked once per run rather than per keystroke (it costs a walk of the
accessible tree), and only believed when it survives the same scrutiny
everything else here gets:

- a *toplevel* holding focus means the desktop does not publish per-widget
  focus at all — GNOME Shell carries it on its own window for the whole
  session — so that reads as "no answer" rather than as the frame;
- the focused element's process must own a window on the recorded display.
  Focus carries no coordinate to corroborate it against, so unlike a click
  there is no second opinion available, and an element from another session
  is refused rather than guessed at;
- among that process's windows, the one the element's own accessible ancestry
  names. Taking the first is how a recording announces a window nothing was
  done in: zenity owns both its dialog and a window called "zenity", and the
  live run that found this generated a stray `wait_for_window("zenity")` for
  typing that went into the dialog.

The last clicked text field remains the fallback wherever focus cannot be
had, which is every desktop running a shell that holds FOCUSED itself.

**Generated code is checked before it is offered.** `validate()` compiles the
file, confirms every `gui.<method>` call exists on the installed
`pyguitest.Session`, checks each `Capability` and `Role` constant against the
same, and reports any name the module reads without ever binding. A recorder
that emits a plausible script naming a function the library does not have is
worse than no recorder — and a script that compiles and then raises
`NameError` on its first run is not much better.

### Checks: what makes it a test

A recording of actions alone is not a test. It passes as long as nothing
raises, whatever the application actually did — click Save, and a script that
never looks at the result passes just as happily against a build where saving
silently fails.

Point at what should have changed and press **Ctrl+F1**. The recorder reads what is
under the pointer *and what it currently says*, and generates a check against
that value:

```python
gui.button("Save").click()
# the recording waited 2.4s here for 'Save As' to open
saveas = gui.wait_for_window("Save As", timeout=10)

# Check: 'Status' reads 'Saved'
expect_text(gui, role=Role.LABEL, name="Status", equals="Saved")
# Check: 'Read only' is checked
expect_checked(gui, role=Role.CHECK_BOX, name="Read only", checked=True)
# Check: 'Undo' is showing
expect_showing(gui, role=Role.PUSH_BUTTON, name="Undo")
```

What gets checked depends on what was under the pointer, most specific first,
because the value of a check is exactly how much it would notice:

| What the pointer was over | What comes out |
|---------------------------|----------------|
| A checkbox, radio button or toggle | `expect_checked(...)`, against its state |
| A text field, or a label with something to say | `expect_text(...)`, against what it read |
| Any other named element | `expect_showing(...)` — the floor |
| No element, but a window | `expect_window(...)` |
| Neither | nothing, and the script's header says so |

**The `expect_` functions are written into the generated file**, not imported
from this package: a generated script is plain pyguitest source and depends on
nothing but pyguitest. They exist rather than bare `assert` statements for two
reasons. A failing `assert gui.element(...).text == "Saved"` reports an
`AssertionError` and a line number, where these say which element was wrong,
what it should have read and what it actually reads. And each one retries
until its timeout — a check recorded the instant an action returns would
otherwise race an application that has not finished redrawing.

Text from a password field is redacted exactly as typed input is, and a check
that could not be resolved to anything is reported in the header rather than
dropped, so a script never looks like it verifies something it does not.
`--no-checks` turns the key off and lets it through to the application, and
`--check-key` rebinds it.

### Waits, not sleeps

The difference between a recorder and a macro player is what happens to the
three seconds you spent waiting for a dialog. A macro player sleeps for three
seconds: slow when the machine is fast, broken when it is slow.

Each pause is instead asked what it was waiting for, and answered from what
the recorded events themselves saw:

| What the recording shows | What comes out |
|--------------------------|----------------|
| The next action is in a window nothing had seen before | `wait_for_window` |
| The next action is on a new element, in a window already open | `wait_for_element` |
| Nothing observable changed | `wait_for_idle(win.pid)` |
| None of the above | `gui.wait(...)`, and a comment saying why |

So the earlier example's four-second gap becomes

```python
# the recording waited 3.8s here for 'Save As' to open
saveas = gui.wait_for_window("Save As", timeout=11.4)
```

with the timeout scaled to what was actually observed rather than guessed.
`--no-sync-inference` turns the whole thing off; `--no-idle-inference` drops
only the third rule, which is the one that needs `WINDOW_PID`.

Inference runs when a script is generated, not when a recording is made. What
is saved is what was *observed*, so `--regenerate` re-analyzes an old
recording under whatever rules exist now, and rendering the same file twice
cannot compound.

## Privacy

Keyboard capture through XRecord sees **every application's keystrokes**, not
only the one you are recording — including your password manager.

- Text typed into an AT-SPI password field is detected and never written into
  the generated script; it gets `os.environ["SECRET_1"]` instead. A check
  recorded against a password field is redacted the same way.
- `--sensitive` treats *all* text that way.
- Raw event logs are off unless `--record-raw` is passed.
- A saved recording is a credential-bearing artifact. Treat it like one.

## Configuration

`$XDG_CONFIG_HOME/pyguitest-recorder/config.toml`, falling back to
`~/.pyguitest-recorder.toml`. Precedence is defaults → file → command line.
See [config.example.toml](config.example.toml).

## Known gaps

- **No UI yet.** The design calls for a timeline, inspector and source preview;
  this is the CLI and the engine underneath it.
- **Only one recording has been made of a real desktop application** — a file
  manager, a text editor and a terminal on GhostBSD. It found three bugs in
  one pass, all now fixed (see the CHANGELOG), the worst of which made any
  recording of an editor fail at replay. The routine live check still uses two
  GTK windows on a private server, so this remains the thinnest-covered part
  of the tool.
- ~~The CI `live` job has never run on a GitHub runner.~~ **Closed:** it now
  runs green on `ubuntu-latest` on every push, so the Ubuntu package names and
  daemon paths are observed rather than reasoned.
- **pyguitest's `Element` has no `double_click`**, only `Session` does. Worked
  around rather than lived with: a double click on a named element emits a
  `double_click_element` helper that looks the element up, reads its rectangle
  *at replay*, and double-clicks there — so the element stays the locator and
  the gesture stays one gesture. It needs the element to have a trustworthy
  rectangle, and falls back to two `Element.click()` calls where it does not,
  which no toolkit is obliged to read as a double click. A triple click has no
  primitive on either path and emits three clicks.
- **Element resolution needs `Capability.ELEMENT_GEOMETRY`**, added upstream
  for this and released in pyguitest 0.4.0. A pyguitest without it declares
  the capability nowhere, so the recorder degrades to coordinates and says so
  rather than failing. (The floor is 0.5.0 for other reasons — see Install.)
- **pyguitest cannot look a window up by application id**, only by title
  regex, so a recording whose title drifted carries a small helper function
  into the generated file to do it.
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
  geometry — see above.

## License

GPL-2.0-or-later, the same as [pyguitest](https://github.com/ctrondlp/pyguitest)
— which this imports at run time and generates source for. See [LICENSE](LICENSE).
