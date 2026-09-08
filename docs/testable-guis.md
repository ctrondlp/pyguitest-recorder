# How to build a GUI that can be tested

**Who this is for:** application developers. It is written to be handed to you
by whoever has to automate your UI, and it asks for about a day of work spread
across a codebase.

**The short version:** accessibility is the *foundation* of robust GUI
automation. A test can find your widgets the same way a screen reader does —
through the accessibility tree — and when it can, it clicks a button by name
and survives redesigns. When it can't, it falls back to pixel coordinates,
and those break the next time anything moves.

Two qualifications, so the rest of this document is read accurately.
Accessibility and automation are not the same thing: a test can also match a
window title, compare an image, or click a coordinate, so a badly-labelled
application is harder to test rather than impossible. And not everything the
accessibility tree exposes is worth automating against — a name that changes
with state is published, visible, and still useless to a test.

None of this is test-only scaffolding. Every item below is the accessibility
work the application already owed. Testability is what you get for free once
it's done.

---

## The one-page version

| Do this | So that |
|---|---|
| Give every button, field, checkbox, menu item and tab an **accessible name** | Tests can say `click the "Save" button` instead of `click pixel (412, 380)` |
| Keep those names **unique within a window** | The test clicks the Remove button you meant, not a different one |
| Keep names **stable when state changes** | `"Save"` doesn't become `"Save (3 unsaved)"` and break every test |
| Use the **right widget type** for the job | A button made from a clickable box isn't a button to anyone outside your process |
| Give the window a **stable app id** (the class half of `WM_CLASS` on X11) | Tests survive a title that changes when the document does |
| Report **which widget has keyboard focus** | Typed text can be attributed to the field it went into |
| Report widget positions in **screen coordinates** | "What's under the mouse here?" gets the right answer |
| Make long operations **visible** — a status message, a disabled button | Tests can wait for your app to be ready instead of sleeping for two seconds |

If you only do the first row, you have already fixed most of it.

---

## The mental model: five things every widget publishes

Everything below is one of these five. Knowing which one a problem belongs to
is most of knowing how to fix it.

| | What it is | A test uses it to | Gets it wrong when |
|---|---|---|---|
| **Role** | What kind of control this is — button, check box, text field, list item | Narrow a search: "the *button* named Save", not any node reading "Save" | You build a button out of a clickable box, so its role is "container" |
| **Name** | The short label identifying it — usually the visible text | Find it at all | An icon-only button has no name, or a name is duplicated within the window |
| **State** | Checked, selected, enabled, visible, focused | Assert on the outcome of an action, and wait for readiness | Enabled stays true while the action is unavailable |
| **Value** | The number behind a slider, spinner or progress bar | Assert on a quantity without parsing a label | Progress lives only in the name, as `"Connecting… 40%"` |
| **Relationships** | How widgets connect — which label names which field, what contains what | Disambiguate two identically-named controls, and attribute a label to a field | A field is left with no `labelled-by`, so its visible label is not its name |

Roles and names are what a test searches on. States and values are what it
asserts on. Relationships are what save it when a name alone is ambiguous.

---

## Why it matters: the failure is silent

The frustrating part of accessibility metadata is that nothing complains when
it's wrong. There is no error, no warning in the console, no failing build. A
test suite just quietly gets worse:

| What the app publishes | What the test can do | How long it keeps working |
|---|---|---|
| Right type, stable unique name | `gui.button("Save").click()` | Through themes, resizes, layout changes |
| Right type, name changes with state | `gui.button("Save (2 changes)")` | Until the count changes |
| Right type, duplicate name | Clicks whichever comes first internally | Until someone reorders the layout |
| No name | Clicks a pixel coordinate | Until anything moves |
| Wrong position reported | Clicks a **different widget** and passes | Forever — nobody finds out |
| Not in the tree at all | Nothing | Nothing |

The second-to-last row is the dangerous one. A coordinate click that fails
becomes a bug report. A named click on the wrong widget becomes a green test
that isn't testing anything.

### The same recording, before and after

This is what the difference looks like in a generated test. Both files came
from doing the identical thing — click the toolbar's save button, type a
filename, confirm — against two versions of the same application.

**Before**, with an unnamed icon-only toolbar button and an entry whose label
is not associated with it:

```python
# note: no accessible element at (412, 88); recorded as a coordinate
gui.move_mouse(412, 88)
gui.click()
gui.wait(1.5)
gui.type_text("report.txt")
gui.move_mouse(690, 512)
gui.click()
```

Nothing in that file says what it does. It breaks when the window moves, when
a toolbar item is added, and when the dialog opens a little slower than it did
on the day it was recorded.

**After**, with `Save` labelled, the filename entry `labelled-by` its label,
and a status message that appears while writing:

```python
gui.button("Save").click()
saveas = gui.wait_for_window("Save As", timeout=10)
gui.text_field("Name").set_text("report.txt")
gui.button("Save").click()
gui.wait_until_gone(name="Saving…", timeout=10)
```

Same interaction, same recorder. The second one reads like a test someone
wrote on purpose, and it survives everything the first one does not — and the
only thing that changed was the metadata the application publishes.

---

## 1. Name every control a user acts on

These widget types need a name, because they're the ones a person operates by
identifying them:

> buttons · toggle buttons · checkboxes · radio buttons · links · text fields ·
> password fields · spin buttons · dropdowns · menu items · tabs · sliders

Labels, images and layout containers don't need one.

Usually the visible text *is* the accessible name and you get this for free. It
breaks for **icon-only buttons** — the toolbar, the close button on a chip, the
row action that's just a trash can. Those are the ones to go looking for.

```python
# GTK 4 (PyGObject)
button.update_property([Gtk.AccessibleProperty.LABEL], ["Save"])

# GTK 3 (PyGObject)
button.get_accessible().set_name("Save")

# Qt (PyQt / PySide)
button.setAccessibleName("Save")
button.setAccessibleDescription("Write the document to disk")
```

```xml
<!-- GTK 4 .ui file -->
<object class="GtkButton" id="save_button">
  <property name="icon-name">document-save-symbolic</property>
  <accessibility>
    <property name="label">Save</property>
  </accessibility>
</object>

<!-- GTK 3 .ui file -->
<object class="GtkButton" id="save_button">
  <child internal-child="accessible">
    <object class="AtkObject">
      <property name="AtkObject::accessible-name">Save</property>
    </object>
  </child>
</object>
```

### Form fields: connect the label to the field

A text field almost never carries its own name. The name is sitting next to
it, in a separate label widget, and unless you say the two are related the
field is published as an unnamed entry — so `gui.text_field("Email")` finds
nothing even though the word "Email" is plainly on screen.

The relationship is what carries it:

```python
# GTK 4 (PyGObject) -- the label names the entry
entry.update_relation([Gtk.AccessibleRelation.LABELLED_BY], [label])

# GTK 3 (PyGObject) -- a mnemonic label does this for you
label.set_mnemonic_widget(entry)

# Qt (PyQt / PySide)
label.setBuddy(line_edit)
```

```xml
<!-- GTK 4 .ui file -->
<object class="GtkEntry" id="email_entry">
  <accessibility>
    <relation name="labelled-by">email_label</relation>
  </accessibility>
</object>
```

Setting an explicit accessible name on the field works too, and is the right
answer where there is no visible label at all — a search box whose only cue is
a magnifying-glass icon, say. Prefer the relation when a visible label exists:
one source of truth, and it stays correct when the label is translated.

Same for a group of radio buttons or a set of related fields: label the group
(`Gtk.AccessibleProperty.LABEL` on the box, `QGroupBox` in Qt) so a test can
scope a search to it rather than relying on the whole window having unique
names.

### Three things that trip people up

- **A tooltip is not a name.** Don't assume tooltip text reaches the
  accessibility tree. Set the label explicitly.
- **The name includes the ellipsis.** A menu item shown as "Save As…" is named
  `"Save As…"` — one ellipsis character, not three dots. Worth knowing when a
  test mysteriously can't find it.
- **Don't put shortcuts in the label** to make things findable. `Ctrl+S`
  belongs in the accelerator, not the name. (Mnemonics are fine — `_Save` is
  named `Save`.)

## 2. Make names unique within a window

Names only need to be unique inside one window, not across the whole app. But
two "Remove" buttons in one dialog are genuinely ambiguous: the test gets
whichever one the toolkit lists first, which is stable right up until someone
reorders the layout, and then it silently starts removing the wrong thing.

In order of preference:

1. **Make them distinct** — "Remove account", "Remove device".
2. **Name the container instead.** For a per-row Delete button, name the row.
   A test can then say "the Delete button inside the row named X", and you
   don't have to invent awkward labels.
3. If neither is possible, at least know it — the test will have to find the
   parent first.

## 3. Use the widget type that matches the behaviour

A "button" built from a clickable box or a bare drawing area shows up outside
your process as a generic container. It's not findable as a button, it isn't
announced as one, and no automated check flags it — containers aren't expected
to have names.

The same goes for anything custom-drawn. A canvas, a chart, a virtualized list,
a custom text view: if you draw your own rows, publish them as list items with
names, or the whole list is one opaque rectangle and every test against it is
back to pixel coordinates.

### Lists, trees and tables

These are where most real applications keep the data a test actually cares
about, and where a custom implementation most often publishes nothing.

- **Publish the structure, not just the pixels.** A list should be a list with
  list items inside it; a table should be a table whose rows contain cells. A
  test then says "the row named *ada@example.com*", which survives sorting,
  scrolling and re-styling. One opaque rectangle survives nothing.
- **Name each row by what identifies it to a user** — the filename, the
  account, the message subject — not by its index. Row 4 changes meaning the
  moment anything is sorted or inserted.
- **Expose selection as state, not as styling.** A test asks "is this row
  selected?" through the selected state. If selection exists only as a
  background colour, it is invisible to everything outside your process.
- **Name the columns.** A cell whose column is anonymous can only be found
  positionally, which is the table equivalent of a pixel coordinate.
- **Virtualized lists are a real constraint, not a bug** — a list that
  publishes only its realized rows is normal, and a test that needs row 900
  has to scroll to it first. What matters is that scrolling *does* make the
  row appear in the tree. If realized rows are never published, or stale rows
  linger after scrolling, the list is worse than untestable: it is
  misleadingly wrong.
- **Per-row action buttons need the row to be findable.** Twenty rows each
  with a "Delete" button is the duplicate-name problem from section 2 at
  scale; naming the row is what makes "the Delete button inside the row named
  X" expressible.

If you implement your own widget from a drawing primitive, the toolkit cannot
help you here — the accessible object and its children are yours to publish.
That is real work, and it is the price of a custom widget; the alternative is
that every test touching it is a coordinate click.

## 4. Keep names stable when state changes

A name that carries state can't be written into a test.

| Instead of | Do this |
|---|---|
| `"Save (3 unsaved)"` | Name it `"Save"`; put the count in the description |
| A toggle whose name flips `"Play"` ⇄ `"Pause"` | One toggle named `"Play/Pause"`; expose the state as checked/unchecked |
| `"Connecting… 40%"` | Name it `"Connection status"`; put the progress in the value |

State has its own fields — checked, selected, enabled, visible, value,
description — and tests can read and assert on all of them. A test asking "is
the *Remember me* checkbox checked?" survives every state change. A test
hunting for a differently-named widget per state does not.

## 5. Give the window a stable identity

Window titles drift. GNOME Text Editor renames its window the moment the
document has content, and a test pinned to the old title matches nothing.

- **Set a stable app id** and don't vary it per document. Tools prefer it over
  the title when they can, precisely because it does not drift. See below for
  which value that actually is on X11 — it is not obvious.
- Keep the constant part of the title predictable — "Untitled — MyApp" is
  easier to match than "MyApp — Untitled".
- **Give dialogs real titles.** An untitled modal can only be found as
  "whatever window just appeared".

### Which value is the app id?

It depends on which display server your window is actually on — and on a
modern desktop, two windows side by side may not agree.

| Your window is | The app id comes from | Set it with |
|---|---|---|
| A **native Wayland** client | The `xdg_toplevel` app id — one string, conventionally your reverse-DNS application id | `Gtk.Application(application_id=...)`, `QGuiApplication::setDesktopFileName`, or your toolkit's equivalent |
| An **X11** client, on a real X session | `WM_CLASS`, specifically the **class** half | The toolkit sets it from your program/application name; `xprop WM_CLASS` shows what you actually shipped |
| An **X11 client under XWayland** (an X11 app in a Wayland session) | `WM_CLASS` again — XWayland clients are X11 clients, and the compositor reports them that way | Same as above |

The XWayland row is the one that surprises people: in a single Wayland
session, native clients are identified by their Wayland app id and XWayland
clients by their `WM_CLASS`, so a tool listing windows sees both conventions
at once. If you ship both a Wayland and an X11 build, set both, and do not
assume the strings match — conventionally they do not.

The catch on the X11 side is that `WM_CLASS` is a **pair** — an instance name
and a class. **Tools read the class.** That is what sway, Hyprland and
pyguitest all report as an X11 window's app id, so it is the half worth
getting right. Check yours with `xprop WM_CLASS`, then click the window:

```
WM_CLASS(STRING) = "myapp", "MyApp"
                    ^        ^
                    instance class — this is the one
```

The class is conventionally a capitalised program name rather than a
reverse-DNS id — `"Gedit"`, `"Firefox"` — so don't be surprised when yours
looks nothing like your Wayland app id. Either is fine; what matters is that
it is set and identical on every run.

Set neither and the property is simply absent — it is optional in ICCCM — so
tools report an empty app id and your window can only be found by the title
you were just told not to rely on.

## 6. Report positions in screen coordinates

This is the one that produces tests that pass while doing the wrong thing, so
it's worth a direct check.

When something asks your widget where it is, the answer must be where it
actually is *on the screen* — not relative to the window, and not `(0, 0)`.

This isn't hypothetical, and it is not one application's bug. Measured on
**Fedora 45, GTK 4.23.3 and at-spi2-core 2.61.1 (2026-09-06)** across three
unrelated GTK 4 applications — gnome-calculator, baobab and gnome-text-editor
— **every widget reported its correct size at position `(0, 0)`**.
gnome-calculator's `C`, `↑n` and `7` buttons all claimed `(0, 0, 64, 44)`.

The size is right and the position is missing, which has a specific
consequence: "what is at this point?" cannot distinguish two widgets, so it
walks back up and answers with the *window*. It did that for 48 of 49 sampled
points in gnome-calculator, 42 of 49 in baobab, 48 of 49 in gnome-text-editor.
Nothing errors. A tool that trusts it clicks confidently on the wrong widget;
a tool that checks, like this one, falls back to raw coordinates and your
application becomes untestable by name.

Mostly this is your toolkit's job. On the GTK 4 versions measured above it was
*not* being done, and there is nothing a tool higher up can recover — a
position that was never published cannot be inferred. Treat that as a finding
about those versions rather than a permanent property of GTK 4: it is the kind
of thing that gets fixed upstream without an announcement, so **measure your
own stack before concluding anything**:

```python
import pyguitest

gui = pyguitest.connect()
window = gui.window_element("MyApp")
for element in gui.elements(within=window):
    print(element.role, element.name, gui.extents(element))
```

Every widget reporting `x` and `y` of `0` while the sizes look right is the
signature. A window that genuinely sits at the top-left corner of the screen
is the one false positive — move it first.

Check it directly if you maintain custom widgets, and check it **on a second
monitor and at a non-100% scale factor**, which is where the remaining bugs
live.

## 7. Make "busy" and "ready" visible

Every automated test eventually has to wait for your app. It can do that well
or badly, and which one it gets is up to you:

| If your app... | The test can |
|---|---|
| Opens a window | Wait for that window |
| Shows a new element (a dialog fills in, a list loads) | Wait for that element |
| Shows nothing at all while it works | Sleep for a guessed number of seconds |

That last row is where flaky GUI tests come from — too slow on a fast machine,
broken on a slow one.

So make progress observable:

- A **"Saving…" status message** that appears and then disappears gives a test
  a precise start and end for free.
- **Disabling the Save button** while the write is in flight gives another.
- Keep **enabled and visible honest**. A button that stays clickable while its
  action is unavailable turns "wait until Save is ready" back into "sleep and
  hope".

## 8. Support keyboard focus and actions

- **Report which widget has keyboard focus.** This carries more weight than it
  looks. Tab-order tests depend on it, and so does knowing which field typed
  text went into — a recorder cannot tell from the pointer, which the user
  moves away the moment the field has focus. It is also the *only* thing that
  still identifies a widget when hit-testing cannot (section 6), because focus
  involves no geometry: a GTK 4 application whose clicks all degrade to
  coordinates still gets `gui.text_field("Name").set_text(...)` for its
  typing, purely because focus is reported correctly. Verified on a bare X
  server, where Tab walks real widgets and each one reports itself focused.
- **Expose the actions a widget supports** (click, press, toggle) rather than
  only reacting to raw mouse events. Tests can then invoke the action directly,
  which is faster and doesn't depend on the pointer being anywhere in
  particular.
- **Make text fields programmatically settable.** Otherwise a test has to
  synthesise forty keystrokes to fill a field — forty chances to lose a
  character.

---

## Environment gotchas that aren't your fault

Your application can be doing everything right and still be invisible, because
the accessibility bridge is switched off. Worth knowing before you go looking
for a bug in your own code:

- **GTK apps need `toolkit-accessibility` on.** GNOME sessions set it; KDE
  sessions don't. With it off, nothing reports a problem — queries just come
  back empty, exactly as if your app had no widgets at all.
  ```console
  $ gsettings set org.gnome.desktop.interface toolkit-accessibility true
  ```
- **Chromium and Electron apps publish nothing until an assistive technology is
  announced.** Until then there is no accessibility tree at all — not a partial
  one. If you ship Electron, launch it with
  `--force-renderer-accessibility` in test environments rather than asking
  everyone to flip a system-wide setting.
- **Qt** activates its bridge from the same system-wide signal;
  `QT_ACCESSIBILITY=1` and `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1` are the usual
  overrides.

## How to check your own work

**Look at the tree.** `pyguitest inspect` prints every widget on the desktop
with its type, name and state (`--json` for a diffable version). If you can't
find your button in that output by name, no test will either.

**Add one assertion per screen to your test suite.** This turns the whole
review into something CI does for you:

```python
import pyguitest

gui = pyguitest.connect()
window = gui.window_element("MyApp")

gui.assert_accessible(within=window)
```

**What it actually checks**, since the name under-promises. Two things, in
this order:

1. **Every visible control that should carry a name has one.** "Should" is a
   fixed list of roles — push button, toggle button, check box, radio button,
   link, entry, password field, spin button, combo box, menu item, check and
   radio menu item, page tab, slider — and `roles=` overrides it. Invisible
   controls are skipped deliberately: an off-screen widget nobody can reach is
   not a labelling problem, and a hidden dialog's worth of them would drown
   the findings that matter.
2. **No two controls of the same role in scope share a name.** Same roles,
   same scope. This is the section 2 problem, caught automatically.

The failure names the counts and the roles, so it tells you *what* to go
fix — "4 visible control(s) have no accessible name: 3 x push button, 1 x
entry".

**What it does not check**, so nobody reads a green build as more than it is:
colour contrast, keyboard operability, tab order, focus visibility, whether a
name is *meaningful* rather than merely present (`"Button1"` passes), whether
labels are associated with the right fields, screen-reader announcement
quality, or anything about states and values being honest. It is a floor, not
a WCAG audit — but it is a floor that fails the build, which is more than most
applications have.

Add it once per screen and the *next* unnamed toolbar button fails the build
on the day it's added, instead of six months later when someone tries to test
it.

**Record a session against your app.** Every click that comes out as
`gui.button("Save").click()` is a control you got right; every click that comes
out as a raw coordinate is one to fix. The generated script says at the top
which ones it couldn't name, and why.

---

## Checklist

- [ ] Every button, field, checkbox, menu item and tab has a name
- [ ] Icon-only buttons have explicit labels, not just tooltips
- [ ] Text fields are associated with their visible label (`labelled-by`)
- [ ] No duplicate names within a single window
- [ ] Names don't change when state changes
- [ ] Widget types match behaviour — no buttons made of boxes
- [ ] Custom widgets publish their contents, not one opaque rectangle
- [ ] Lists and tables publish rows, cells and selection — not just pixels
- [ ] Window app id is stable — on X11, the *class* half of `WM_CLASS`
- [ ] Dialogs have real titles
- [ ] Keyboard focus is reported per widget, not just per window
- [ ] Positions are correct on a second monitor and at non-100% scaling
- [ ] Enabled / visible / checked reflect reality
- [ ] Long operations show something that appears and disappears
- [ ] One `assert_accessible` per screen, running in CI

---

<details>
<summary>Where these claims come from</summary>

Measured on live desktops by pyguitest or this recorder: the GTK 4 widget
position bug — first in zenity, then across gnome-calculator, baobab and
gnome-text-editor with the per-point hit rates quoted above (Fedora 45,
at-spi2-core 2.61.1, gtk4 4.23.3, 2026-09-06); `toolkit-accessibility` off by
default on KDE and the silent empty results that follow (2026-09-01); Chromium
and Electron absent from the accessibility tree while the system-wide flag is
false (GNOME Shell 51, 2026-09-05); window titles drifting under GNOME Text
Editor.

Focus reporting has been measured twice, with opposite results, and both are
true: **no** per-widget focus on GNOME Shell 50.4 Wayland across three toolkits
(the shell holds it session-wide), and **working** per-widget focus for GTK 4
on a bare X server with no shell running (2026-09-06). So section 8 is worth
doing even though a tool cannot always benefit from it — what breaks it is the
desktop, not your application.
Details in pyguitest's
[validation.md](https://github.com/ctrondlp/pyguitest/blob/main/docs/validation.md).

Taken from toolkit documentation rather than verified here: the GTK 3/4 and Qt
API calls and UI-file syntax, and the Qt environment variables. They're the
standard forms, but check them against your toolkit version before treating a
failure as your application's fault.

</details>
