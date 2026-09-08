# How the recorder works, and why

The reasoning behind the design. None of this is needed to *use* the tool —
[getting-started.md](../getting-started.md) is that — but all of it explains
why a recording came out the way it did.

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

## Generated code is checked before it is offered

`validate()` compiles the file, confirms every `gui.<method>` call exists on
the installed `pyguitest.Session`, checks each `Capability` and `Role`
constant against the same, and reports any name the module reads without ever
binding.

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
