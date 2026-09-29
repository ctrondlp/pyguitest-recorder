# Troubleshooting

Symptom first. The generated script's own header is worth reading before any
of this — every refusal to name an element is recorded there as a note, for
that specific recording.

- [Why is my script all coordinates?](#why-is-my-script-all-coordinates)
- [`--doctor` says element resolution is off](#doctor-says-element-resolution-is-off)
- [Nothing was captured at all](#nothing-was-captured-at-all)
- [On Windows, one window records nothing](#on-windows-one-window-records-nothing)
- [The recording stopped when I did not mean it to](#the-recording-stopped-when-i-did-not-mean-it-to)
- [The check key typed into the application instead of recording a check](#the-check-key-typed-into-the-application-instead-of-recording-a-check)
- [The script clicked the wrong thing](#the-script-clicked-the-wrong-thing)
- [The script fails at replay](#the-script-fails-at-replay)
- [The script waits too long, or not long enough](#the-script-waits-too-long-or-not-long-enough)
- [A window is found by title and the title changed](#a-window-is-found-by-title-and-the-title-changed)

## Why is my script all coordinates?

This is the most common question, and there are only two families of answer:
the recorder could not *reach* the accessibility layer, or it reached it and
did not *believe* what it was told.

**Start with `--doctor`.** If element resolution is off, that is the whole
answer — see [the next section](#doctor-says-element-resolution-is-off).

If resolution is on, the recorder looked and refused. It drops an accessible
element, and falls back to a coordinate, whenever the answer cannot be
corroborated:

| It refused because | Which usually means |
|---|---|
| The element's process does not own the window under the same point | Two applications overlapping, or a stale accessibility node |
| No window on the recorded display accounts for it | You are recording `--display :99` but the element came from your real session |
| The element's rectangle does not fit inside that window | The toolkit is reporting geometry in window coordinates, not screen coordinates |
| The element's own extents do not contain the point it was looked up at | The toolkit reported a position that is not where the widget is |
| The answer was the *toplevel itself*, not anything inside it | Every widget in that window reports the same rectangle, so hit-testing cannot distinguish them |

The last two are the GTK 4 case, and they are not your mistake or the
recorder's. Measured on Fedora 45 (GTK 4.23.3, at-spi2-core 2.61.1), every
widget in gnome-calculator, baobab and gnome-text-editor reported its correct
*size* at position `(0, 0)` — so "what is at this point?" cannot tell two
widgets apart and answers with the window. A coordinate that works beats a
named element that does not, so the recorder takes the coordinate.

**One thing that is not a refusal, and still comes out as a coordinate: a click
on a popup that has already closed.** A press is resolved once the application
has consumed it, and choosing a combo box's own item, or a submenu entry, closes
the popup it was in — so by then there is nothing there to name. On Linux the
recorder looks at the popup while it is open and remembers its layout, which is
what `_popup_at` exists for; on Windows it relies on UI Automation hit-testing
popups itself, and that cannot see one that has closed. Measured on Windows 11
(2026-09-26) against a real combo box and a real submenu: both clicks recorded as
coordinates, with none of the `'X' was named, but ...` comments that accompany an
element the recorder *did* name and could not act on — there was nothing there to
name by then. Both replayed correctly, because the script's coordinates are
relative to the window's own origin. If that matters for a particular control, a
hand-written `gui.menu_item("Save As").click()` or
`gui.dropdown("Size").choose("Large")` is the upgrade — those go through the
accessibility layer instead.

**Typed text is the exception** and still gets named, because focus involves
no geometry: you will see `gui.text_field("Name").set_text("Ada")` in a
recording whose clicks are all coordinates. That is expected, not
inconsistent.

**Rectangle checks are skipped entirely where any screen is scaled**, since
AT-SPI extents and window geometry are then not reliably in the same units.

If the application is yours, this is fixable at the source:
[testable-guis.md](testable-guis.md) is written to be handed to its
developers.

## `--doctor` says element resolution is off

`dogtail`, which element resolution goes through, **declares no dependencies
of its own** — PyGObject and pyatspi have to come from your distribution, and
`pip install 'pyguitest-recorder[atspi]'` cannot supply them. Miss them and nothing errors;
every click just comes out as a coordinate.

```sh
# Fedora
sudo dnf install python3-gobject python3-pyatspi at-spi2-core
# Debian / Ubuntu
sudo apt install python3-gi python3-pyatspi gir1.2-atspi-2.0
```

pyguitest's [install guide](https://github.com/ctrondlp/pyguitest/blob/main/docs/install.md)
carries the full table, including Arch, openSUSE and FreeBSD.

Two further causes worth knowing, both of which look identical from here:

- **The accessibility bridge is switched off.** GTK applications need
  `toolkit-accessibility`; GNOME sessions set it, KDE sessions do not.
  `gsettings set org.gnome.desktop.interface toolkit-accessibility true`.
- **The application is Chromium or Electron.** Those publish *nothing* to the
  accessibility tree until an assistive technology is announced — not a
  partial tree, none. Launch it with `--force-renderer-accessibility`.

## Nothing was captured at all

**Are you on Wayland?** Recording needs X11 or XWayland on a Linux desktop,
native Windows, or macOS — and under a Wayland session the recorder reaches
XWayland clients and nothing else, so recording a native Wayland application
produces nothing —
[developers/architecture.md](developers/architecture.md#why-wayland-has-no-capture-backend)
explains why this is permanent rather than a gap.

Check which one the application under test actually is: in a GNOME Wayland
session, some applications are native Wayland and some are XWayland, and they
look identical on screen.

**Is `python-xlib` installed?** On Linux and the BSDs, capture needs the `x11`
extra: `pip install 'pyguitest-recorder[x11]'`.

**Is this a rootless XWayland?** A private headless GNOME session is not a
substitute for Xvfb — the RECORD context comes up cleanly, but the compositor
owns the pointer and neither XTEST nor `uinput` can inject into it, so there
is nothing to record. Use a real Xvfb for automated capture testing.

## On Windows, one window records nothing

Windows does not show an unelevated process the input going to an elevated
window (UIPI), so a recorder started from an ordinary prompt sees nothing
typed into Task Manager, `regedit`, an MMC snap-in such as `services.msc`, or
anything else that runs as administrator. Nothing fails; the recording just
has no events for it.

`pyguitest-recorder --doctor` says whether the recorder is elevated and, if it
is not, whether the window in front is. To record an administrator window,
start the recorder from an elevated prompt too.

The other Windows-only limit is a keyboard layout with dead keys or AltGr,
which has been unit-tested but not tried on a real keyboard; if `é` or `@`
comes out wrong, that is worth a report.

## The recording stopped when I did not mean it to

The stop key is **Escape pressed twice in a row** by default. An application
that uses Escape heavily — a dialog you open and close repeatedly — can hit
that by accident.

```sh
pyguitest-recorder --stop-key Pause --stop-presses 1
```

Any chord works (`ctrl+Escape`, `ctrl+shift+F12`), and matching is exact, so a
bound `ctrl+F1` leaves `ctrl+shift+F1` to the application.

## Pressing the stop key does not seem to stop it

Pressing it once has no visible effect at all — by design, a single press
belongs to the application being recorded — so the natural response is to
pause and check before pressing again. That pause has to land within
`stop_key_interval` (2.0s by default) for the two presses to count as one
run; slower than that, the first press is handed on as a keystroke and the
count restarts. Recording now prints `Escape (1/2) — press again within
2s to stop.` the moment the first press registers, precisely so there is no
need to guess whether it was seen. If it is still happening with presses
close together in time, check what has focus: on Linux capture is
XRecord/X11-only, so a press landing on a genuinely native-Wayland surface (no
XWayland presence at all) never reaches the recorder in the first place — see
`docs/developers/architecture.md#why-wayland-has-no-capture-backend`.

## The check key typed into the application instead of recording a check

The check key matches *exactly*, so a held Shift silently turns a check into a
keystroke: `ctrl+1` is not matched by `ctrl+shift+1`. That is deliberate — the
application can still have the shifted chord.

A key that is *remapped by the hardware* fails the same silent way, which is why
the default is a plain digit. A laptop's bare F1 commonly sends
`XF86_AudioMute` rather than the `F1` X11 calls it, and the matcher never sees
that — so every press is recorded as an ordinary keystroke and nothing says so.
`ctrl+F1` was the default until that was measured live.

`--no-checks` turns the key off entirely and passes it through, and
`--check-key` rebinds it.

## The script clicked the wrong thing

**If it clicked a coordinate**, the window moved, resized, or opened at a
different position than when it was recorded. That is the failure mode
coordinates have; the fix is making the target findable by name — see
[Why is my script all coordinates?](#why-is-my-script-all-coordinates).

**If it clicked a named element and got the wrong one**, two elements shared a
role and a name — two "Remove" buttons, say — and the generator could not
tell them apart. It tries to on its own first: when the elements' recorded
ancestry differs by a *named* container anywhere above them, it scopes each
one to that container automatically, emitting `within=<ancestor>` with no
edit needed.

That only fails when nothing in either element's path is both named and
unique to it — two identically structured, identically named panes, say. In
that case the script header carries a warning naming the collision, and the
generated calls are left unscoped, matching whichever pyguitest's search
finds first. Disambiguate by hand:

```python
gui.element(
    role=Role.PUSH_BUTTON, name="Remove", within=gui.window_element("Accounts")
).click()
```

Generated scripts are meant to be edited; this is one of the places where a
user adds something the recording could not know.

## The script fails at replay

**On the first line, with a typed exception**, that is `gui.require(...)`
working: the session you are replaying on lacks a capability the recording
used. The message names it. A recording made on X11 that used window
placement, pointer coordinates or `wait_for_idle` will do this on a Wayland
desktop that does not grant them.

**With `ElementNotFound`**, the application published different names this
time — often because it opened in a different state, or a dialog had not
appeared yet. Adding a wait is usually the fix, and the recorder's inference
did not add one because nothing observable changed at that moment.

**With `ElementNotActionable`, or a `CapabilityUnsupported` naming a pattern**,
the element the recorder named has no action to run — and the two shapes now call
for different things. A `CapabilityUnsupported` naming a pattern — *"the do
default action pattern failed on this element"* with a hex HRESULT — is a
**refusal**: the element advertises a `LegacyIAccessible` default action and its
own provider throws when asked for it, which is what a WinForms control that
declares no default action looks like from UI Automation: the MSAA bridge
exposes `accDoDefaultAction` for everything, so `actions` carries `do default
action` (which is why the generator named it at all) and the call then raises
.NET's `InvalidOperationException` (`0x80131509`). Measured live on Windows 11
(2026-09-26) on this repository's probe window: a `SysListView32` *cell* refused
while the window's `EDIT` accepted the identical call, so the shim's fidelity
varies by control and cannot be asked in advance. The fix is to act on the
*control* rather than the piece under the cursor — `gui.list_item("Gamma").click()`,
or `.select()` where the row offers it — and `docs/developers/status.md` records
what the recorder itself will do about the naming.

A plain `ElementNotActionable` is the other shape: the element publishes
*nothing at all*, which is what that same cell does on a platform whose shim
declares none, and **pyguitest's `Element.click()` now clicks such an element by
coordinate rather than raising** — the element stays the locator and only the
gesture falls back to the pointer, so a recording that names one keeps working
with no change to what the recorder generated. Seeing this error there now means
the element had no *rectangle* to aim at either — it is not showing, or the
backend reports none — and the message says so; `Session.click_element(element)`
is the same call spelled for an element taken straight from a backend. Both are
in pyguitest 0.14.0, with a live pass still to come. On a pyguitest older than
that, a no-action element fails at replay, so edit that one line of
the generated script to the coordinate pair the recording carries —
`gui.move_mouse(x, y)` then `gui.click()`, which is what the generator renders
for a click that has no element at all — or act on the control as above.

**With an `AttributeError` on `gui.something`**, your installed pyguitest is
older than the recording expects. **pyguitest 0.15.1 or newer is required
outright** — the floor the generated code is verified against. Generated
scripts may call `Element.expand()`/`.collapse()` or read `.selectable`
directly (0.12.0), call the `expect_` family as `Session` methods (0.9.0 and
later), double-click named elements with `Element.double_click` (0.10.0), and
under `motion = "natural"` or `"recorded"` move the pointer with
`Session.move_mouse_naturally` (0.10.1). 0.11.0 is the first release that
imports on Windows at all, and 0.14.0 is the one a macOS recording needs:
before it there is no `macos` backend for the recording's windows and
elements to be resolved through, and no `macquartz` key vocabulary for its
key names to be translated through. 0.15.0 was where the floor stood with
nothing generated here actually needing what it added -- simply the release
the output was last verified against. 0.15.1 is a real need: a script under
`locators = "element"` (the default) scopes its search with
`gui.window_element(title)`, and an older pyguitest there could resolve that
call to a shell-owned decoration proxy instead of the real window on a real
GNOME/Mutter desktop, found live. Older floors matter as well:
`gui.button(...)` finds nothing on a current at-spi2 before 0.5.0.

## The script waits too long, or not long enough

Timeouts come from what was actually observed during the recording, not guessed
— a 12.4 second wait becomes `timeout=13`, a pause under ten seconds still gets
the ten-second floor, and five minutes is the ceiling on any one wait. Earlier
versions multiplied each wait up instead — three times it, floor and ceiling
included — which left the timeout unreadable beside the comment above it and
quietly absorbed a replay machine slower than the one that recorded. Nothing is
absorbed now: if that machine needs longer, the number to edit says how long the
wait took. It is a plain literal in a plain script.

That is the fix for one script. For a whole suite that consistently needs more
than a plain reading gives — CI hardware slower across the board, or a wait
you would rather fail fast than sit out — `max_timeout` (`--max-timeout`) and
`timeout_factor` (`--timeout-factor`) change the rule generation-wide instead
of every file by hand: `max_timeout` moves the five-minute ceiling, and
`timeout_factor` above 1 buys the same blanket tolerance the old default did,
at the same price — a wait's two numbers stop agreeing, and the generated
header says so, naming the multiplier in force rather than claiming a plain
rounding it is no longer doing. See
[config.example.toml](../config.example.toml) to set either from a config
file, or `pyguitest-recorder --help` for the flags.

If a pause became `gui.wait(...)` with a comment saying why, that means
nothing observable changed while it waited — no new window, no new element.
That is the application not making its progress visible, and
[testable-guis.md](testable-guis.md#7-make-busy-and-ready-visible) is the fix
at the source.

To try different inference rules without re-recording, keep the `.json` and
[regenerate](recipes.md#record-once-regenerate-forever).

## A window is found by title and the title changed

Window titles drift — GNOME Text Editor renames its window the moment the
document has content. Where the recorder can tell that happened, the script
does not match on the title at all: it matches the application id instead, as
`gui.expect_window(app_id="org.gnome.TextEditor", timeout=10)`, and no helper
is written into the file to do it. A window whose title never changed still
matches by title, which is the thing a reader recognises.

If a replay then fails to find a window that is plainly on screen, check the
app id the script asks for before anything else. A window has two possible
identities and they do not always agree — the `xdg_toplevel` app id on
Wayland, the class half of `WM_CLASS` on X11 — so a recording made one way can
name a window the other way never shows; see
[testable-guis.md](testable-guis.md#which-value-is-the-app-id). Failing that,
the call takes an ordinary title or pattern, and these files are meant to be
edited — that is what they are for.
