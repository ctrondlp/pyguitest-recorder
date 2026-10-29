# Getting started

From nothing to a test you can run, in about five minutes. The short version
is four commands; the rest of this page explains what each one did.

```sh
pip install 'pyguitest-recorder[x11,atspi]'          # Linux, XWayland
pip install pyguitest-recorder 'pyguitest[windows]'  # Windows
pip install 'pyguitest-recorder[macos]'              # macOS

pyguitest-recorder --doctor              # can this machine record?
pyguitest-recorder -o login_test.py      # record; press Escape twice to stop
python3 login_test.py                    # replay it (python on Windows)
```

The first line is the only one that changes by platform; the command itself
is the same on all three. `python3` on the last line is the interpreter you
installed into — `python` on Windows, and `py -3` from a Command Prompt if
`python` is not on your `PATH`.

> **Before your first recording:** keyboard capture goes through the platform's
> own mechanism — X11's RECORD extension, a low-level input hook on Windows, an
> event tap on macOS — and sees **every application's keystrokes**, not only
> the one you are recording. Close your password manager and anything else you
> would not want in a file.
> [Privacy](#privacy-read-this-before-the-first-recording) below is the full
> picture.

## 1. Can this machine record?

```sh
pyguitest-recorder --doctor
```

Recording needs X11 or XWayland on a Linux desktop, native Windows, or macOS.
No ordinary Wayland *client* can observe another client's input *through* the
compositor — a property of the platform, not a missing feature; see
[developers/architecture.md](developers/architecture.md#why-wayland-has-no-capture-backend)
for why. Under a Wayland session the default backend reaches XWayland clients
and nothing else, and says so rather than producing a file with silent gaps.
There is one experimental exception, which reads input *below* the compositor
rather than through it — see
[Recording on Wayland (experimental)](#recording-on-wayland-experimental).

`--doctor` answers three questions: can input be captured at all, can clicks
be resolved to named elements, and is the installed pyguitest new enough.

**The line to look for is element resolution.** If it is off, every click in
every recording comes out as a coordinate — which looks like the recorder
being bad at its job rather than a missing package. The cause is almost always
that `dogtail` declares no dependencies of its own, so PyGObject and pyatspi
have to come from your distribution:

```sh
# Fedora
sudo dnf install python3-gobject python3-pyatspi at-spi2-core
# Debian / Ubuntu
sudo apt install python3-gi python3-pyatspi gir1.2-atspi-2.0
```

**On macOS there is one grant, it has no package, and it is not the pane the
name suggests.** `CGEventTapCreate` answers only when the application
responsible for this process is listed under **System Settings → Privacy &
Security → Accessibility** — that is Terminal, your IDE, or the `sshd`
identity for a session reached over SSH, not the virtualenv path. TCC
composes its Input Monitoring row from that grant rather than keeping a
second one, so **Input Monitoring says "No Items" on a machine where
recording works**, which is measured on macOS 26 rather than inferred; go
looking there and you will be told to grant something that is already
granted. A grant is recorded against a binary and applies to processes
started after it was made, so quit the recorder and start it again rather
than recording into the same terminal. `--doctor` reports the refusal
verbatim when a tap cannot be created, and element resolution is off until
the grant is in place. Nothing on Linux or Windows needs any of this.

## Recording on Wayland (experimental)

The default backends above record X11, XWayland, native Windows and macOS.
There is also an **experimental** `wayland` backend that records a native
Wayland session by reading the kernel's input devices (`/dev/input/event*`)
*below* the compositor — the one capture path that works on every compositor,
because it does not go through any of them. It does not weaken the platform's
guarantee: it is not a Wayland client observing another client, it is a
privileged process reading device nodes.

**Understand what that access is before you grant it.** Reading
`/dev/input/event*` needs the `input` group or a udev ACL — the same *tier*
pyguitest's uinput replay needs, but a **stronger capability pointed the other
way**: replay *writes* synthesised events to `/dev/uinput`, while this *reads*
every real event from `/dev/input`. And reading input is **machine-wide** — it
sees keystrokes and pointer activity from *every* application, not just the one
you mean to record, your password manager and terminals included. So grant it
deliberately, prefer the narrowest form that fits (a per-boot ACL over
permanent group membership), and drop it when you are done recording. Never run
the recorder as root to get around inaccessible devices.

Install the extras and give the user read access to the input devices:

```sh
pip install 'pyguitest-recorder[wayland,atspi]'

# Read access to /dev/input, one of:
sudo setfacl -m "u:$USER:r" /dev/input/event*   # this boot only; narrowest
sudo usermod -aG input "$USER"        # persistent; log out and back in
# revoke when done: `sudo setfacl -x "u:$USER" /dev/input/event*` for the ACL,
# or `sudo gpasswd -d "$USER" input` (then re-login) for the group.
```

Capture only ever *reads* the devices, so the ACL grants `r` alone — write
access on `/dev/input/event*` would let any process running as this user
inject input, a capability recording never needs. And `[atspi]` is not
optional garnish here: **password redaction depends on element resolution.**
The recorder recognises a password field through the accessibility tree; with
no `[atspi]` extra (or a session where resolution is off) it cannot, and text
typed into one goes into the generated script in clear unless you pass
`--sensitive`, which redacts all typed text unconditionally.

One behavioural difference between the two: the ACL is stamped on the device
nodes that exist when the command runs, while group membership covers nodes
udev creates later (they come up `root:input`). The backend rescans
`/dev/input` during a recording precisely so a device plugged in mid-session
is captured — under the ACL route that new node is not readable and is
skipped, so if your session involves hotplugged input devices, the group is
the grant that actually covers them.

With those in place no configuration is needed: `backend = "auto"` prefers
`xrecord` wherever an X server is reachable, exactly as before, and falls back
to this backend only on a session that is **actually Wayland** — never on an
X11 session whose XRecord merely failed, which keeps `auto` from quietly
turning into a machine-wide input reader there. So one configuration serves an
X11 machine and a pure-Wayland one alike. Naming `backend = "wayland"` selects
it outright; setting `display` disables the fallback, because that asks for an
X server by name. A setting the backend cannot honour is refused with an
explanation *before* recording starts, not silently dropped.

Three limits to know going in, all documented in the backend itself:

- **Linux only.** Keys are decoded through `libxkbcommon` under the layout the
  `XKB_DEFAULT_LAYOUT`/`_VARIANT`/… variables name — **not** the compositor's
  active keymap. A desktop session normally exports them; a bare SSH shell or
  a login manager that does not leaves `libxkbcommon` on its built-in default
  (US) even with the library installed, so a German QWERTZ session recorded
  that way comes out decoded as US. Export `XKB_DEFAULT_LAYOUT` (and
  `_VARIANT`) to match the session before recording in that case. A layout
  switched *during* a recording is not tracked; where `libxkbcommon` cannot
  be loaded at all, a fixed US table is the fallback.
- Pointer coordinates are exact for an **absolute device** (a drawing tablet,
  a VM's integration pointer), scaled from the device's own range to the
  screen the DRM connector reports. A **relative mouse** only reports deltas,
  so its position is tracked from the screen's centre and clamped — and that
  starting point is a guess: the offset between the tracked position and the
  real cursor never corrects on its own, so on a session with no absolute
  device a click can be attributed to the wrong window entirely. A session
  recorded with a tablet or inside a VM has the exact kind. For a bare-metal
  relative-only mouse, setting `screencast_cursor = true` reads the true
  position from the ScreenCast portal (a consented screen-share — it raises a
  one-time "Share your screen?" dialog) and stamps every event with it
  instead, which is the supported answer there.
- It captures the **whole machine's input**, so the
  [privacy](#privacy-read-this-before-the-first-recording) note below applies
  with full force — every keystroke in every window reaches the device node.

`--doctor` probes this backend like any other — on a live KWin session it
reports `capture: wayland-evdev (available)` and whether clicks will be named
— and reports the permission error with the fix when `/dev/input` is not
readable.

## 2. Record something

```sh
pyguitest-recorder -o login_test.py
```

Do the thing you want tested — click, type, open a dialog. Then:

- **Escape, twice** ends the recording. Not once: a single Escape belongs to
  the application under test, and a press that does not complete the run is
  passed through and recorded like any other key, so "press Escape to close
  the dialog" stays recordable.
- **Ctrl+1** records a *check* on whatever the pointer is over — see
  [step 4](#4-make-it-an-actual-test).

Without `-o` the script goes to stdout and the summary to stderr, so a
redirect works too.

## 3. Read what came out

```python
"""Generated by pyguitest-recorder. Edit freely.

Profile:     pyguitest-0.19
Recorded on: x11 (mutter)
Timeouts:    seconds; the wait the recording observed, rounded up (floor 10s, cap 300s)
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

It is plain pyguitest source with no dependency on this package. Edit it
freely — that is the point. Three things about it are deliberate:

**Elements lead, coordinates follow.** Each click is resolved at record time
against the accessibility tree, and falls down a ladder only as far as it has
to:

```
    gui.button("Save").click()          a named element  — survives redesigns
        ↓  nothing accessible under the pointer?
    window-relative coordinates         survives the window moving
        ↓  no window accounts for the point?
    gui.move_mouse(842, 612)            absolute — breaks when anything moves
```

`--absolute-coordinates` forces the bottom of that ladder; `--relative-coordinates`
forces the middle one.

**Scripts declare what they need.** The `gui.require(...)` preamble names the
capabilities the script actually uses, so a recording made on X11 and replayed
somewhere weaker fails on the first line with a typed exception instead of
halfway through with a click that went nowhere.

**Waits, not sleeps.** A three-second pause while a dialog opened does not
become `sleep(3)`. Each pause is asked what it was waiting for and answered
from what the events themselves saw:

```python
# the recording waited 12.4s here for 'Save As' to open
saveas = gui.wait_for_window("Save As", timeout=13)
```

`timeout` is in seconds, and it is the wait the comment reports, rounded up to
the next whole second — 12.4 becomes 13. A pause under ten seconds still becomes
`timeout=10`, because ten seconds is the floor, and five minutes is the ceiling
on any one wait. The comment is what happened; the timeout is what the script
allows, and the two now say the same thing.

If a script came out as coordinates and you expected names,
[troubleshooting.md](troubleshooting.md#why-is-my-script-all-coordinates) is
the page — and the generated file's own notes, at the end, already list the
reasons for that particular recording.

## 4. Make it an actual test

A recording of actions alone is not a test. It passes as long as nothing
raises, whatever the application actually did — click Save, and a script that
never looks at the result passes just as happily against a build where saving
silently fails.

So while recording, point at what should have changed and press **Ctrl+1**.
The recorder reads what is under the pointer *and what it currently says*, and
generates a check against that value:

```python
gui.button("Save").click()
saveas = gui.wait_for_window("Save As", timeout=10)

# Check: 'Status' reads 'Saved'
gui.expect_text(role=Role.LABEL, name="Status", equals="Saved")
```

What you get depends on what was under the pointer — a checkbox gives
`expect_checked`, a label with something to say gives `expect_text`, anything
else named gives `expect_showing`. [recipes.md](recipes.md#checks-what-makes-it-a-test)
has the full table.

## 5. Keep the recording, not just the script

```sh
pyguitest-recorder -o login_test.py --save-session login.json
```

That `.json` is the *observations* — what happened, not the code. It is worth
more than the script over time, because inference improves:

```sh
pyguitest-recorder --regenerate login.json -o login_test.py
```

Record once, regenerate forever. See
[recipes.md](recipes.md#record-once-regenerate-forever).

## Privacy: read this before the first recording

Keyboard capture through XRecord sees **every application's keystrokes**, not
only the one you are recording — including your password manager.

- Text typed into an AT-SPI password field is detected and never written into
  the generated script; it gets `os.environ["SECRET_1"]` instead. A check
  recorded against a password field is redacted the same way.
- `--sensitive` treats *all* typed text that way.
- `--exclude-window '*Bitwarden*'` keeps everything done in a matching window
  out of the recording entirely — see
  [recipes.md](recipes.md#keeping-secrets-out-of-the-script).
- Raw event logs are off unless `--record-raw` is passed.
- **A saved recording is a credential-bearing artifact.** Treat it like one.

## Where to go next

| If you want… | Read |
|---|---|
| Flags, checks, config, regeneration | [recipes.md](recipes.md) |
| A recording that came out wrong | [troubleshooting.md](troubleshooting.md) |
| Your own app to be recordable by name | [testable-guis.md](testable-guis.md) |
| Why it works this way | [developers/architecture.md](developers/architecture.md) |
| What has actually been run | [developers/status.md](developers/status.md) |
