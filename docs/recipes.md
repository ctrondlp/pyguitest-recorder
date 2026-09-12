# Recipes

Task-shaped answers. For a first recording see
[getting-started.md](getting-started.md); for a recording that came out wrong
see [troubleshooting.md](troubleshooting.md).

- [Record once, regenerate forever](#record-once-regenerate-forever)
- [Checks: what makes it a test](#checks-what-makes-it-a-test)
- [Rebinding the stop and check keys](#rebinding-the-stop-and-check-keys)
- [Recording hovers and menus](#recording-hovers-and-menus)
- [Keeping secrets out of the script](#keeping-secrets-out-of-the-script)
- [Forcing coordinates, or forcing elements](#forcing-coordinates-or-forcing-elements)
- [Turning off inference](#turning-off-inference)
- [Putting your own header on the file](#putting-your-own-header-on-the-file)
- [Recording a specific display](#recording-a-specific-display)
- [Configuration file](#configuration-file)
- [Running without installing](#running-without-installing)

## Record once, regenerate forever

Always pass `--save-session`. The `.json` holds what was *observed* — events,
the windows and elements they resolved to, the pauses between them — and it
outlives the script generated from it:

```sh
pyguitest-recorder -o login_test.py --save-session login.json
```

Later, re-render it under whatever rules exist now, without recording
anything again:

```sh
pyguitest-recorder --regenerate login.json -o login_test.py
```

This is worth doing habitually for two reasons. Inference improves — a pause
that rendered as `gui.wait(3.8)` last month may render as
`wait_for_window(...)` today, and regenerating gets you that for free across
every recording you ever made. And rendering is deterministic from the
observations, never from the previous script, so regenerating the same file
twice cannot compound artifacts.

It is also the fastest way to try a different rendering: the same recording
with `--absolute-coordinates`, or with `--no-sync-inference`, tells you what
those flags actually change without re-performing the interaction.

## Checks: what makes it a test

A recording of actions alone passes as long as nothing raises. Press
**Ctrl+F1** while recording, pointing at whatever should have changed, and the
recorder reads what is under the pointer *and what it currently says*:

```python
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

A check that could not be resolved to anything is reported in the header
rather than dropped, so a script never looks like it verifies something it
does not. `--no-checks` turns the key off and lets it through to the
application instead.

## Rebinding the stop and check keys

Both take the same chord syntax — `Escape`, `ctrl+Escape`, `ctrl+shift+F1` —
and both match *exactly*, so binding `ctrl+F1` leaves `ctrl+shift+F1` to the
application being recorded.

```sh
pyguitest-recorder --stop-key Pause --stop-presses 1
pyguitest-recorder --check-key ctrl+shift+F1
```

The defaults are chosen around what laptops actually have. Pause is not on
many keyboards any more, so Escape stands in — twice, because a single Escape
belongs to the application. A key nothing else wants can drop the repeat, as
above. The check key carries a modifier for the same reason: a bare F9 is a
screenshot key on some laptops, and a bare F1 is help nearly everywhere.

## Recording hovers and menus

By default pointer movement is recorded only as part of a drag — a recording
full of every intermediate position is noise, not a test.

Anything driven by *hovering* needs the movement itself:

```sh
pyguitest-recorder --record-motion
```

Use it for a menu that opens on hover, a tooltip you want to assert on, or a
toolbar that reveals controls under the pointer.

## Keeping secrets out of the script

Keyboard capture sees every application's keystrokes, so this matters more
than it would in a tool that only watched one window.

```sh
pyguitest-recorder --sensitive        # treat ALL typed text as secret
```

By default, text typed into an AT-SPI password field is detected and replaced
with `os.environ["SECRET_1"]` in the generated script, and a check recorded
against a password field is redacted the same way. `--sensitive` extends that
to everything typed, which is the right default when recording anything that
touches a login.

`--no-redact` does the opposite and writes password-field text into the script
as a literal. There is no good reason to use it outside debugging the redactor
itself.

**A saved `--save-session` recording is a credential-bearing artifact** even
with redaction on, because redaction happens at generation time. Treat the
`.json` like a password file, or do not keep one for a session that touched
real credentials.

## Forcing coordinates, or forcing elements

The ladder is element → window-relative coordinate → absolute coordinate, and
the recorder walks down it only as far as it must. Two flags override that:

```sh
pyguitest-recorder --absolute-coordinates    # always screen coordinates
pyguitest-recorder --relative-coordinates    # prefer window-relative over elements
```

`--absolute-coordinates` is a diagnostic more than a mode: it answers "is the
element resolution the thing making this recording weird?" and gives you a
script that depends on nothing but the pointer. Both make the result more
brittle by construction — a named element is the only rung that survives the
window moving.

You can also drop a whole resolution layer:

```sh
pyguitest-recorder --no-element-context     # don't resolve clicks via AT-SPI
pyguitest-recorder --no-window-context      # don't open a pyguitest session at all
```

## Turning off inference

```sh
pyguitest-recorder --no-sync-inference      # keep every pause as a sleep
pyguitest-recorder --no-idle-inference      # drop only the wait_for_idle rule
```

`--no-sync-inference` renders every pause as `gui.wait(...)`, which is what a
macro player would do. It is occasionally the right answer for an application
whose readiness genuinely is not observable, and it is a useful comparison
when a waiting rule guessed wrong. `--no-idle-inference` drops only the third
rule, the one that needs `WINDOW_PID`.

Since inference runs at generation time, `--regenerate` lets you try these
against an existing recording rather than re-performing it.

## Putting your own header on the file

```sh
pyguitest-recorder --header "SPDX-License-Identifier: MIT"
pyguitest-recorder --no-header        # drop the provenance docstring
pyguitest-recorder --no-comments      # drop the explanatory comments
```

`--header` puts your text *above* the provenance block rather than instead of
it — a licence line or a ticket number should not silently drop the record of
which versions and which desktop produced the file. A multi-line `header` in
the config file works the same way.

## Quieting warnings you already know about

```sh
pyguitest-recorder --suppress-keymap-warning
pyguitest-recorder --suppress-atspi-chatter
```

Both default off, and both are about noise on a *replay* you already trust,
not about recording. `--suppress-keymap-warning` silences pyguitest's own
`KeymapWarning` (uinput injects raw scancodes, so replaying on a machine with
a different keyboard layout than the one recorded on can type the wrong
characters entirely silently otherwise) — real signal worth seeing at least
once, so leave it on until you have actually checked layouts match. If your
terminal is full of `dbind-WARNING **: AT-SPI: Error in GetItems ...` lines
that have nothing to do with your script, `--suppress-atspi-chatter` silences
GLib's own "dbind" log domain instead; this is native library chatter, not
something pyguitest itself emits, so silencing it installs a process-wide
GLib log handler for the whole replay -- harmless in practice, but the reason
it is not the default.

## Recording a specific display

```sh
pyguitest-recorder --display :99 --screen 0
```

Both the capture and the window/element context follow the display given, so a
recording made against a private X server resolves its clicks against *that*
server's windows rather than the compositor you happen to be sitting in front
of. That was a real bug once; see
[developers/status.md](developers/status.md#the-live-capture-check).

## Configuration file

`$XDG_CONFIG_HOME/pyguitest-recorder/config.toml`, falling back to
`~/.pyguitest-recorder.toml`; `--config FILE` picks a specific one.
Precedence is defaults → file → command line, so a flag always wins over the
file.

See [config.example.toml](../config.example.toml) for every key with its
default.

## Running without installing

The package uses a `src/` layout, so the source directory has to be on the
import path. Every flag works identically this way:

```sh
PYTHONPATH=src python3 -m pyguitest_recorder --doctor
PYTHONPATH=src python3 -m pyguitest_recorder -o login_test.py --save-session rec.json
```

It is not a way to skip the dependencies — `pyguitest` and `python-xlib` still
have to be importable. `scripts/live-capture-check.py` needs neither form: it
puts `src/` on the path itself and runs straight from a checkout.
