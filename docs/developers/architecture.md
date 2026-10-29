# How the recorder works, and why

The reasoning behind the design. None of this is needed to *use* the tool —
[getting-started.md](../getting-started.md) is that — but all of it explains
why a recording came out the way it did.

## Why Wayland has no capture backend

Passive observation — the thing XRecord does — is a property of the platform,
not a missing feature.

pyguitest classifies reading global keyboard or pointer state as
`Capability.INPUT_STATE_QUERY`, tier 6 — *"deliberately prevented"* — and
describes it as what a keylogger reads. That is exactly what a recorder must
do. No ordinary Wayland client can observe another client's input on any
compositor, and no backend added later changes that: injection on Wayland took
portals, libei and per-compositor IPC, but observation is the thing compositors
exist to prevent.

The consent-shaped route was the one candidate left open here, and it was
investigated and closed on 2026-10-18. The Input Capture portal
(`org.freedesktop.portal.InputCapture`, spoken by python-libei's
`InputCaptureSession`) lets a user deliberately hand their keyboard and
pointer to an application — but it *diverts* input rather than copying it: it
was built for input-leap-style pointer crossing, so while a capture is active
the desktop being recorded does not receive the input being captured. The
investigation confirmed the portal is current and available (GNOME 51 answers
with interface version 2, `CreateSession2`, all capabilities) — availability
is not the blocker; the diversion model is. A recorder on top of it would be
a whole-desktop input proxy: activation is barrier-triggered, so capturing an
ordinary in-session interaction means keeping the capture active for the
entire recording and re-injecting every event through libei, with the proxy's
latency sitting in the hot path of every keystroke of the very session being
recorded — and whether the compositor re-captures the re-injected synthetic
events (an infinite loop) is undefined by the spec. That is judged not viable
as a recorder design, on the compositors that implement the portal at all
(COSMIC, for one, does not). The accurate statement remains that "Wayland
recording is impossible" was only ever true of passive capture *through the
display server*.

**The experimental exception, below the compositor.** A Phase-0 spike
(2026-10-27) settled the two questions this section left open. The AT-SPI
event idea is *not* an input-acquisition path: measured on GNOME Wayland, a
passive AT-SPI listener sees effects (focus, state, text-changed, dialogs
appearing) but never the click or keypress that caused them — it can say which
widget reacted, not what the user did. The input itself is reachable the one
way the platform does not mediate: the kernel's `/dev/input` device nodes,
*below* the compositor, which a process in the `input` group reads directly.
That is not the tier-6 violation this section rules out — it is not a Wayland
client observing another client through the display server, it is the same
privileged device access pyguitest's own `uinput` *replay* already requires,
one tier, read instead of write. So there is now a fourth, Linux-only capture
backend (`backends/wayland.py`, the `wayland` name): `auto` still prefers
`xrecord` wherever an X server is reachable and falls back to this one only
where none is, so the pure-Wayland trap `choose_backend` describes now ends in
a recording instead of a refusal. Targets are supplied separately: on native
Wayland, where `element_at` cannot be trusted (AT-SPI extents there are
window-relative), the resolver names a click from keyboard focus and from a
live AT-SPI effect log (`windows/atspi_effects.py`) -- both strictly additive,
turning a coordinate into a name and never the reverse -- while XWayland
clients keep the ordinary `element_at` path. (`analyzer/atspi_fusion.py` is
the offline spike that measured this; the resolver is the production path.)
It records every compositor identically because it goes through none of them
-- run live on all five in the fleet: Mutter, KWin, COSMIC, Hyprland and niri.
Keys decode through `libxkbcommon` under the layout `XKB_DEFAULT_*` names (not
the compositor's active keymap; no variables exported means the library's US
default), with a fixed US table only where that library will not load; an
absolute pointer (a tablet, every VM) is scaled exactly, and a relative-only
mouse is tracked from the screen centre -- a guessed seed whose offset never
self-corrects -- unless the opt-in `screencast_cursor` setting reads the true
position from the ScreenCast portal's cursor metadata. It stays marked
experimental for its remaining edges: coordinate scaling assumes a single
connected output, and a plain push button on a native-Wayland client --
publishing no focus or selection event -- records as a coordinate. The
principle is intact: no ordinary Wayland client gained the ability to watch
another.

X11's RECORD extension, Windows' low-level input hooks and macOS's
`CGEventTap` are the exceptions — one per platform, which is why they are the
three capture backends this tool has. Neither of the first two is subject to
the permission model its own platform otherwise enforces for input: X11 has
none to speak of, and Windows has no equivalent of a Wayland compositor's
refusal or of a Mac's grant — a low-level hook simply works, silently, for any
process on the desktop. The Mac's is the one that asks, and it is the reason
`macos.py` asks the window server by creating a probe tap rather than a
preflight: `CGEventTapCreate` answers with a live tap when the grant is there
and `NULL` when it is not, and on macOS 26.7 the grant that answer composes
from is Accessibility. Under a Wayland session
`xrecord` reaches XWayland clients and nothing else — and says so in the
recording and in the generated script's header, rather than producing a file
with silent gaps. `win32` carries its own honest limit instead: a hook that
misses Windows' own `LowLevelHooksTimeout` is unhooked without any error
reaching this process, so a Windows recording can in principle have a gap
nothing here can detect — see `pyguitest_recorder/backends/win32.py`'s own
module docstring for what that means and why raw input, Microsoft's documented
alternative, is not what this backend uses yet.

The one part that *is* portable across all three is element resolution, which is
a *read* rather than a subscription: AT-SPI answers "what is under this point"
identically under X11 and Wayland, pyguitest's UI Automation backend answers the
equivalent question on Windows, and its Accessibility backend answers it on
macOS — where it is the only backend that answers both halves,
`WINDOW_AT_POINT` and `ELEMENT_GEOMETRY`, out of one session, which is why a Mac
recording resolves its windows and elements through a single backend rather than
a capture/resolution pair. (AT-SPI events were once the candidate acquisition
layer for a Wayland recorder; the Phase-0 spike measured that they carry
effects, never the input itself, and the `wayland` backend reads `/dev/input`
below the compositor instead — see the exception above.) The event model here
is deliberately free of X11 vocabulary — `RawEvent.keysym` is spelled the way
X11 itself spells it, which `win32.py`, `macos.py` and `wayland.py` all
reproduce by hand rather than borrowing pyguitest's own lower-cased internal
vocabulary, precisely so this layer stays platform-neutral to whatever reads
it next.

### Recording and replaying are different questions

**Recording** needs X11 or XWayland, native Windows, macOS — or, with the
experimental `wayland` backend, read access to the kernel's input devices
below a Wayland compositor. Under a Wayland session the *default* backend
reaches XWayland clients and nothing else, which the recording and the
generated script's header both say; the `wayland` backend records the whole
session, native clients included.

**Replaying** is pyguitest's problem, not this tool's, and it goes further —
pyguitest injects on Wayland through portals, libei and per-compositor IPC.
But how far a *particular* script gets depends on what is in it, and that is
decided when it is recorded:

| What the script contains | How it replays on pure Wayland |
|---|---|
| `gui.button("Save").click()` and other named elements | The portable case. AT-SPI answers the same under X11 and Wayland |
| `gui.move_mouse(x, y)` and window-relative coordinates | Needs pointer and window-geometry capabilities the compositor may not grant |
| `gui.activate_window(...)`, `wait_for_idle(win.pid)` | Compositor-tier: available on some desktops, absent on others |
| `gui.expect_window(app_id=...)` | **Protocol-specific.** An id recorded through XWayland is the `WM_CLASS` class; the same application running natively publishes a different one, and the match is exact |

That last row is the one that bites in practice, because it is the case where
a script is *closest* to portable and still fails. gedit is `Gedit` through
XWayland and `gedit` natively; gnome-calculator is `gnome-calculator` and
`org.gnome.Calculator`; neither is derivable from the other. pyguitest's
`expect_window` accepts several ids for exactly this reason, and a recording
can only ever have seen the one it was made through — so the generator writes
a comment saying so whenever the recording was an XWayland one, and naming
both ids by hand is the fix:

```python
gui.expect_window(app_id=("Gedit", "gedit"), timeout=10)
```

Confirmed live on GNOME Shell 51.rc, both the failure and the fix: see
[status.md](status.md).

So a recording that resolved to elements is close to portable, and one that
came out as coordinates is close to X11-only. That is the same reason element
resolution is worth the trouble, stated from the replay end — and it is why
the notes explaining *why* a script came out as coordinates are worth reading
before assuming it will run somewhere else.

Nothing here fails silently: the generated `gui.require(...)` preamble names
the capabilities the script actually uses, so replaying it somewhere weaker
raises a typed exception on the first line rather than clicking into empty
space halfway through.

## When it refuses to name an element

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

## Naming an element correctly is only half of it

Everything above happens while recording, and all of it is about not naming
the *wrong* element. A generated script has the same problem again at replay
time, against a desktop that has moved on, and for a long time it had none of
the same protection: the lookups it emitted were unscoped.

That is not a small difference. The accessible tree spans every running
process, so `gui.menu_item("Help")` asks the whole desktop. Measured on
GhostBSD while replaying: it resolved to pid 1663, `mate-panel`, whose own
`Help` entry is not even on screen, while the application under test had one
at `(245, 61, 56, 31)`. The recorder had been careful about pid at record
time and then written a script that threw that care away.

Every lookup a script emits is now scoped to the window-ish ancestor the
recording saw -- `dialog`, `frame`, `window` or `application`, nearest
first, so an element inside a dialog is scoped to the dialog rather than the
frame behind it. Clicks, recorded checks and inferred waits all take the
same scope from one place (`_scope_for`), because the first two promised that
parity in their docstrings and quietly lost it when scoping arrived.

Two rules keep it honest:

- **A container whose name is its own selection is never the anchor.** Qt
  publishes a tab bar's accessible name as its *selected tab's* label and a
  combo box's as its current item, with nothing stable in the description.
  Anchoring there produced `gui.element(role="page tab list",
  name="Displays")`, which stopped resolving the moment anything selected
  another tab -- including at replay start, where the application opens on
  its first tab. Measured live: three tab bars answered `Alpha`, `One` and
  `Buttons & Toggles`, and selecting the Displays tab renamed the third.
- **An unresolvable collision gets no scope at all.** Where two same-named
  elements cannot be told apart by ancestry, giving both the same window
  scope would read as though something had distinguished them. They stay
  unscoped and the existing warning is the whole story.

## Typing goes where focus is, not where the pointer is

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
  focus at all — so that reads as "no answer" rather than as the frame.
  **GNOME used to be the standing example of such a desktop, and was not
  one.** Both the shell's own window and the application's widget publish
  `FOCUSED` there; `Session.focused()` returned whichever a walk from the
  tree root reached first, which is always the shell's, so this rule fired on
  every GNOME recording and typed text was never named. Fixed upstream in
  pyguitest 2026-09-21 — it searches the active window's application first
  and prefers a widget over a toplevel — which also took the call from 4.5s
  to 0.3s here, the single largest cost in consuming a recording. The rule
  itself stays, for the desktops that really are like that;
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
had — which, since the fix above, is fewer desktops than this used to say.

### Which window a keystroke went to

The same reasoning applies to the *window* a key event is attributed to. A key
event still carries a coordinate, but nothing about a keystroke depends on it:
the window that received it is the one with keyboard focus. Asking the pointer
question for it works on X11, where the pointer is real, and fails on native
Wayland, where the coordinate is a tracked guess: the full resolve lands on
the shell under it (plasmashell on KWin) and then pays for a focus read of
about 1.2s per event. Measured on KWin over the torture test's 300 keystrokes,
that put the recorder 225s behind live input, so the focus-lag gate failed
closed and withheld all seven text runs, while every keystroke was attributed
to plasmashell rather than the application being typed into.

So `Normalizer` asks the resolver `resolve_keyboard` for key and hotkey events.
On Wayland it answers with the active window alone, about 0.01s, and reads no
element and no focus; anywhere else it is the pointer question as before. It is
optional on a resolver, like `resolve_hover`, and a resolver without it is
asked the pointer question.

The text-run fallback keeps the pointer question, deliberately. What made the
keystroke paths expensive was asking per key; a run asks once. And it is the
only one of the two answers that carries an *element* — which is what decides
whether a run is a password and has to be withheld. Asking the keyboard
question there returned a window and no element, `_is_secret` could not then
tell a password field from an ordinary one, and a password typed into a field
`focused()` could not name went into the script in clear. Caught in review of
this change, before it shipped; the regression test is
`test_a_password_the_keyboard_question_cannot_see_is_still_withheld`.

One more limit, because the answer is focus *now*, read when the event is
consumed: a key consumed late can be attributed to whatever focus moved on to,
and the key itself is one of the things that moves it. The stop key makes that
routine — `stop_key_presses` is 2, so every single Escape is held back waiting
for a second one — and an Escape that closed a dialog would otherwise name the
dialog's parent, leaving the generated script to focus the parent and replay an
Escape that does nothing there. Past `focus_max_lag` the window is therefore
left unnamed, the same threshold the typed-text gate uses for the same reason. The same run twice
after: the recorder finished 6.3s and 8.4s behind, 300 of the 302 keystrokes
were attributed to the torture test's window, 3 of the 7 text runs were still
withheld (the lag that remains is the clicks, which still resolve in full),
and the replay exited 0.

What the pointer question costs is not the same on every compositor, which is
why this was confirmed on a second one. On GNOME Shell 51.1 the same resolve
names *no* window rather than the shell, and takes 124ms rather than 1.2s --
so the backlog that made this visible on KWin would never have formed there,
while the attribution was wrong just as silently. The GNOME run is in
[status.md](status.md).

## Generated code is checked before it is offered

`validate()` compiles the file, confirms every `gui.<method>` call exists on
the installed `pyguitest.Session`, checks each `Capability` and `Role`
constant against the same, checks what is called on an element —
`gui.element(...).double_click()`, an attribute read on a *result* rather than
on a `gui` name — against the installed `Element`, and reports any name the
module reads without ever binding.

A recorder that emits a plausible script naming a function the library does
not have is worse than no recorder — and a script that compiles and then
raises `NameError` on its first run is not much better.

## Inference runs at generation time, not at capture time

What is saved in a `--save-session` recording is what was *observed*: events,
the windows and elements they resolved to, and the pauses between them. The
rules that turn a pause into `wait_for_window` rather than `gui.wait(...)` run
when a script is generated.

Two things follow. `--regenerate` re-analyzes an old recording under whatever
rules exist now, so an improvement to inference improves every recording ever
made. And rendering the same file twice cannot compound — the analysis always
starts from the observations, never from the last script.
