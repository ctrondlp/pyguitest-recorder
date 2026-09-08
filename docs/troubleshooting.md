# Troubleshooting

Symptom first. The generated script's own header is worth reading before any
of this — every refusal to name an element is recorded there as a note, for
that specific recording.

- [Why is my script all coordinates?](#why-is-my-script-all-coordinates)
- [`--doctor` says element resolution is off](#doctor-says-element-resolution-is-off)
- [Nothing was captured at all](#nothing-was-captured-at-all)
- [The recording stopped when I did not mean it to](#the-recording-stopped-when-i-did-not-mean-it-to)
- [Ctrl+F1 typed into the application instead of recording a check](#ctrlf1-typed-into-the-application-instead-of-recording-a-check)
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
`pip install '.[atspi]'` cannot supply them. Miss them and nothing errors;
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

**Are you on Wayland?** Recording needs X11 or XWayland. Under a Wayland
session the recorder reaches XWayland clients and nothing else, so recording a
native Wayland application produces nothing —
[developers/architecture.md](developers/architecture.md#why-recording-is-x11-only)
explains why this is permanent rather than a gap.

Check which one the application under test actually is: in a GNOME Wayland
session, some applications are native Wayland and some are XWayland, and they
look identical on screen.

**Is `python-xlib` installed?** Capture needs the `x11` extra:
`pip install '.[x11]'`.

**Is this a rootless XWayland?** A private headless GNOME session is not a
substitute for Xvfb — the RECORD context comes up cleanly, but the compositor
owns the pointer and neither XTEST nor `uinput` can inject into it, so there
is nothing to record. Use a real Xvfb for automated capture testing.

## The recording stopped when I did not mean it to

The stop key is **Escape pressed twice in a row** by default. An application
that uses Escape heavily — a dialog you open and close repeatedly — can hit
that by accident.

```sh
pyguitest-recorder --stop-key Pause --stop-presses 1
```

Any chord works (`ctrl+Escape`, `ctrl+shift+F12`), and matching is exact, so a
bound `ctrl+F1` leaves `ctrl+shift+F1` to the application.

## Ctrl+F1 typed into the application instead of recording a check

The check key matches *exactly*. `ctrl+F1` is not matched by `ctrl+shift+F1`
— which is deliberate, so the application can still have the shifted chord —
but it does mean a held Shift silently turns a check into a keystroke.

`--no-checks` turns the key off entirely and passes it through, and
`--check-key` rebinds it.

## The script clicked the wrong thing

**If it clicked a coordinate**, the window moved, resized, or opened at a
different position than when it was recorded. That is the failure mode
coordinates have; the fix is making the target findable by name — see
[Why is my script all coordinates?](#why-is-my-script-all-coordinates).

**If it clicked a named element and got the wrong one**, the name is ambiguous
within that window — two "Remove" buttons, say. The recorder emits what it
resolved, and matching picks the first. Disambiguate the generated call by
hand:

```python
gui.element(role=Role.PUSH_BUTTON, name="Remove",
            within=gui.window_element("Accounts")).click()
```

Generated scripts are meant to be edited; this is one of the places where a
human adds something the recording could not know.

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

**With an `AttributeError` on `gui.something`**, your installed pyguitest is
older than the recording expects. **pyguitest 0.5.0 or newer is required
outright** — generated scripts call `gui.button(...)`, which finds nothing on
a current at-spi2 before that release.

## The script waits too long, or not long enough

Timeouts are scaled from what was actually observed during the recording, not
guessed — a 3.8 second wait becomes `timeout=11.4`. If a machine is much
slower than the one that recorded it, edit the number; it is a plain literal
in a plain script.

If a pause became `gui.wait(...)` with a comment saying why, that means
nothing observable changed while it waited — no new window, no new element.
That is the application not making its progress visible, and
[testable-guis.md](testable-guis.md#7-make-busy-and-ready-visible) is the fix
at the source.

To try different inference rules without re-recording, keep the `.json` and
[regenerate](recipes.md#record-once-regenerate-forever).

## A window is found by title and the title changed

Window titles drift — GNOME Text Editor renames its window the moment the
document has content. Where the recorder can tell that happened, it carries a
small helper into the generated file that looks the window up by application
id instead, because pyguitest itself can only look a window up by title regex.

If a replay fails to find a window that is plainly on screen, loosen the regex
in the generated script — that is exactly the kind of edit these files are
meant to receive.
