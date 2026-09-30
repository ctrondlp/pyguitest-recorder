# How to build a GUI that can be tested

**Who this is for:** application developers. Someone who automates or tests
your UI is probably handing you this page, and the request is modest: give
your controls accurate names, roles and states. Most of the value is in the
first half hour, and the rest can be done a screen at a time.

**Why it works:** automation tools such as
[pyguitest](https://github.com/ctrondlp/pyguitest) and its recorder find your
widgets the way a screen reader does, through the platform's accessibility
tree. When the tree is accurate, a test clicks "the button named Save" and
survives redesigns, theme changes and window moves. When it is not, the test
falls back to pixel coordinates, which break the next time anything moves.

The same work makes your application usable with a screen reader, so this is
not test-only scaffolding: what a test needs is mostly what a blind or
low-vision user needs. You do not need pyguitest to do any of it; the
[last section](#how-to-check-your-own-work) lists free inspectors for every
platform.

Two caveats, so the rest is read accurately. Accessibility and automation are
not the same thing: a test can also match a window title, compare an image or
click a coordinate, so a badly labelled application is harder to test, not
impossible. And not everything the tree exposes is worth testing against: a
name that changes with state is published, visible and still useless to a
test.

**Contents:**
[Start here](#start-here) ·
[The mental model](#the-mental-model-five-things-every-widget-publishes) ·
[Why it matters](#why-it-matters-the-failure-is-silent) ·
[1. Names](#1-name-every-control-a-user-acts-on) ·
[2. Unique names](#2-make-names-unique-within-a-window) ·
[3. Widget types](#3-use-the-widget-type-that-matches-the-behaviour) ·
[4. Stable names](#4-keep-names-stable-when-state-changes) ·
[5. Window identity](#5-give-the-window-a-stable-identity) ·
[6. Positions](#6-report-widget-positions-where-your-toolkit-allows) ·
[7. Busy and ready](#7-make-busy-and-ready-visible) ·
[8. Focus and actions](#8-support-keyboard-focus-and-actions) ·
[An empty tree](#when-it-isnt-your-code-an-empty-tree) ·
[Checking your work](#how-to-check-your-own-work) ·
[Checklist](#checklist)

---

## Start here

Three tiers, by how much time you have. Each stands on its own.

### If you have 30 minutes

The two that buy the most, in this order:

1. **Give every control a name** —
   [section 1](#1-name-every-control-a-user-acts-on). Usually the visible text
   already is the name and you get this for free. The ones that need work are
   the icon-only buttons: the toolbar, the close button on a chip, the row
   action that is only a trash can.
2. **Connect every field to its visible label** —
   [also section 1](#form-fields-connect-the-label-to-the-field). A text field
   almost never carries its own name; the name is sitting next to it in a
   separate label widget, and unless you say the two are related the field is
   published as an unnamed entry.

Then look at what you actually published, rather than assuming it worked,
with `pyguitest inspect --window "MyApp"` or any of the inspectors under
[How to check your own work](#how-to-check-your-own-work). If your Save
button turns up there under the name you expect, a test can find it.

### If you have an afternoon

Sections [1](#1-name-every-control-a-user-acts-on) and
[2](#2-make-names-unique-within-a-window), plus one assertion per screen in
your test suite:

```python
gui.assert_accessible(within=window)
```

[How to check your own work](#how-to-check-your-own-work) has the full call,
what it does and does not check, and the shape to paste it into a suite.

### If you are choosing a toolkit, or writing a custom widget

[Section 3](#3-use-the-widget-type-that-matches-the-behaviour) decides whether
any of the rest is reachable. A "button" built from a clickable box, a canvas
you draw yourself, a virtualized list that publishes nothing — none of those
can be fixed by labelling, because there is nothing there to label.

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

Accessibility metadata has a frustrating property: nothing complains when it
is wrong. There is no error, no console warning and no failing build. A test
suite just quietly gets worse:

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
from `pyguitest-recorder -o test.py`, doing the identical thing — click the
toolbar's save button, type a filename, confirm — against two versions of the
same application.

**Before**, with an unnamed icon-only toolbar button and an entry whose label
is not associated with it:

```python
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
gui.wait_for_window("Save As", timeout=10)
gui.text_field("Name").set_text("report.txt")
gui.button("Save").click()
gui.wait_until_gone(name="Saving…", timeout=10)
```

Same interaction, same recorder. The second one reads like a test someone
wrote on purpose, and it survives everything the first one does not — and the
only thing that changed was the metadata the application publishes.

---

## 1. Name every control a user acts on

These widget types need a name, because they are the ones a person operates by
identifying them:

> push buttons · toggle buttons · check boxes · radio buttons · links · text
> entries · password fields · spin buttons · combo boxes · menu items · check
> and radio menu items · page tabs · sliders

That is exactly the role list the automated check in
[How to check your own work](#how-to-check-your-own-work) uses, so an unnamed
one of these fails a build rather than only a screen reader. Labels, images and
layout containers don't need a name.

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

The same naming, on Windows and macOS:

```xml
<!-- WPF (XAML) -->
<Button AutomationProperties.Name="Save" Content="&#xE74E;" />
```

```csharp
// WPF (code-behind)
AutomationProperties.SetName(saveButton, "Save");

// WinForms
saveButton.AccessibleName = "Save";
saveButton.AccessibleDescription = "Write the document to disk";
```

```swift
// AppKit (Cocoa)
saveButton.setAccessibilityLabel("Save")
saveButton.setAccessibilityHelp("Write the document to disk")

// SwiftUI
Button("Save", action: save)
    .accessibilityLabel("Save")
```

```html
<!-- Web content, including Electron -->
<button aria-label="Save"><svg aria-hidden="true">…</svg></button>

<label for="email">Email</label>
<input id="email" type="email">
```

Standard WinForms and WPF controls, and standard AppKit controls
(`NSButton`, `NSTextField`, and the rest), are automation-visible with no
extra step at all — the properties above are for the same icon-only case
GTK and Qt need them for, where there is no visible text to fall back on.

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

On Windows and macOS there is no per-application id to choose. A Windows
window is identified by its **class name** (`GetClassName`), which the toolkit
generates (`Notepad`, `Chrome_WidgetWin_1`); a custom Win32 class name comes
from `RegisterClassEx`. A macOS window is identified by its owning
application's **display name** (`kCGWindowOwnerName`, from `CFBundleName` or
`CFBundleDisplayName` in `Info.plist`), which every window the process owns
shares. Both are coarser than a Wayland app id or an X11 class: they do not say
*which* window of an application you are looking at, so there the title, or the
element tree scoped by `pid`, tells two windows of the same application apart.

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

## 6. Report widget positions, where your toolkit allows

Mostly this one is your toolkit's job. It is listed because of how badly it
fails, not because you are likely to cause it. A test that asks "what widget
is at this point?" relies on each widget reporting where it is. When
positions are missing or in the wrong coordinate space, a tool that trusts
them clicks the wrong widget and the test still passes; a tool that checks
falls back to raw coordinates, and your application is testable only by
position.

What has been measured, so you know what to expect:

- **GTK 4 applications reported every widget at `(0, 0)`**, with the correct
  size: gnome-calculator's `C`, `↑n` and `7` buttons all claimed
  `(0, 0, 64, 44)`. "What is at this point?" then answered with the window
  for most sampled points (48 of 49 in gnome-calculator, 42 of 49 in baobab,
  48 of 49 in gnome-text-editor). That was one Fedora 45 stack (details at the
  end); other toolkit releases may differ.
- **Native Wayland clients cannot know where their window is on the screen**,
  so the positions they publish are relative to the window. The same GTK 3
  application reported a button at `(516, 183)` through XWayland and at
  `(32, 23)` natively, with the window at `(510, 183)`.

Neither is something an application sets wrong, and a tool cannot recover a
position that was never published. What you can do:

- **Prefer names and actions to geometry in your own tests.**
  `gui.button("Save").click()` uses no coordinates, so none of the above
  touches it.
- **If you draw your own widgets**, publish their bounds through your
  toolkit's accessibility API, in the coordinate space it asks for.
- **Check your own stack**, on a second monitor and at a non-100% scale
  factor, where the remaining bugs live:

```python
import pyguitest

with pyguitest.connect() as gui:
    # A plain string matches a window title as a literal substring; pass a
    # compiled re.Pattern for a regex.
    window = gui.window_element("MyApp")
    for element in gui.elements(within=window):
        print(element.role, element.name, gui.extents(element))
```

Every widget reporting `x` and `y` of `0` with plausible sizes is the
signature. A window that genuinely sits at the top-left corner of the screen
is the one false positive, so move it first.

If `window_element` raises `WindowNotFound` for a window that is plainly on
screen, look it up by app id instead of by title (see
[section 5](#which-value-is-the-app-id)):

```python
window = gui.find_window(app_id="org.example.MyApp")
```

If the toolkit publishes no name for the window at all (a native-Wayland GTK 4
frame has been seen with an empty name), scope by process instead, since every
element still reports its own `pid`:

```python
for element in gui.elements(predicate=lambda e: e.pid == window.pid):
    print(element.role, element.name, gui.extents(element))
```

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
  typing, purely because focus is reported correctly. Checked on a bare X
  server and on GNOME, where Tab walks real widgets and each one reports
  itself focused.
- **Expose the actions a widget supports** (click, press, toggle) rather than
  only reacting to raw mouse events. Tests can then invoke the action directly,
  which is faster and doesn't depend on the pointer being anywhere in
  particular.
- **Make text fields programmatically settable.** Otherwise a test has to
  synthesise forty keystrokes to fill a field — forty chances to lose a
  character.

---

## When it isn't your code: an empty tree

Your application can be doing everything right and still be invisible, because
the accessibility bridge is switched off. Worth knowing before you go looking
for a bug in your own code.

### If the tree comes back empty, on Linux

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

Each of those looks exactly like an application with no accessible widgets, so
`pyguitest inspect` coming back empty is worth reading as a question about the
desktop before it is read as one about your code.

### If the tree comes back empty, on Windows or macOS

Neither platform has a system-wide bridge to switch on — UI Automation and
Accessibility are always live, so an empty tree there points somewhere more
specific:

- **Windows: a custom-drawn or raw Win32 control has no automation peer.**
  Standard WinForms and WPF controls are UI-Automation-visible by
  construction; an owner-drawn Win32 control, or a WPF `FrameworkElement`
  that only overrides rendering, publishes nothing until it implements one
  (`AutomationPeer` in WPF; `IAccessible`/UIA provider interfaces for raw
  Win32) — the same "container with no children" gap section 3 describes
  for GTK and Qt.
- **macOS: a bare `NSView` subclass is invisible the same way.** Standard
  AppKit controls (`NSButton`, `NSTextField`, and the rest) implement
  `NSAccessibilityProtocol` automatically; a view that only overrides
  `drawRect:` does not, and has to adopt the protocol itself — or set
  `isAccessibilityElement = true` plus a label and role — before it shows up
  at all. Electron and Chromium's `--force-renderer-accessibility` applies
  here too; the flag is not Linux-specific, only the *default* of publishing
  nothing until asked is shared with the GNOME/KDE case above.

## How to check your own work

**Look at the tree.** If you cannot find your button in an inspector by
name, no test will either. Use whichever fits your platform:

| Platform | Inspector |
|---|---|
| Linux | `pyguitest inspect`, or Accerciser |
| Windows | Accessibility Insights for Windows, or Inspect.exe from the Windows SDK |
| macOS | Accessibility Inspector, in Xcode's developer tools |

`pyguitest inspect` prints every widget on the desktop with its type, name and
state (`--json` for a diffable version). `--window` takes a regex and narrows
the listing to windows whose **title** matches it:

```sh
pyguitest inspect --window "MyApp"
```

**Add one assertion per screen to your test suite.** This turns the whole
review into something CI does for you:

```python
import pyguitest

with pyguitest.connect() as gui:
    # a window title -- see section 6 if this raises WindowNotFound
    window = gui.window_element("MyApp")
    gui.assert_accessible(within=window)
```

`assert_accessible` needs pyguitest 0.3.0 or later, the release that added it
and the two assertions underneath it; `pyguitest-recorder --doctor` prints the
installed version so you can check.

**What it actually checks**, since the name under-promises — two things, in
this order:

1. **Every visible control that should carry a name has one.** "Should" is a
   fixed list of roles — push button, toggle button, check box, radio button,
   link, entry, password text, spin button, combo box, menu item, check menu
   item, radio menu item, page tab, slider — and `roles=` overrides it.
   Invisible controls are skipped deliberately: an off-screen widget nobody can
   reach is not a labelling problem, and a hidden dialog's worth of them would
   drown the findings that matter.
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

**Record a session against your app.** Recording your own application and
reading what came out is the fastest review there is:

```sh
pyguitest-recorder -o test.py
```

Every click that comes out as `gui.button("Save").click()` is a control you got
right; every click that comes out as a raw coordinate is one to fix. A name
that exists but couldn't be *acted on* gets a comment right on that line;
anything systemic — element resolution off entirely, a Chromium window that
never joined the tree — lands in a note block at the end, pointed to from the
top of the file. So the list of things to fix writes itself, and it is
written by the same rules a test would be held to.

## Checklist

A version to paste into a pull-request template:

- [ ] Every icon-only button has an accessible name.
- [ ] Every text field is connected to its visible label (or named directly).
- [ ] Names are unique within each window; per-row actions have a named row.
- [ ] Names do not change with state; state is exposed as state or value.
- [ ] Custom widgets use the platform's widget type (or publish role, name
      and bounds themselves).
- [ ] The window has a stable app id and real dialog titles.
- [ ] Busy work is visible: a status message, a disabled button, or both.
- [ ] Keyboard focus is reported, and text fields can be set programmatically.
- [ ] A screen of your app passes `gui.assert_accessible(within=window)`.

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
Editor; that same application's native-Wayland frame publishing an empty
AT-SPI name while every element belonging to its process stayed enumerable
and correctly placed, confirmed on KDE Plasma 6 / KWin (2026-09-09).

Per-widget keyboard focus is published on GNOME (GTK 3 through XWayland and
GTK 4 natively) and on a bare X server with no shell running; an earlier
version of this document said GNOME did not, which was a bug in the tool
reading it, not in GNOME. Details in pyguitest's
[validation.md](https://github.com/ctrondlp/pyguitest/blob/main/docs/validation.md).

Taken from toolkit documentation rather than verified here: the GTK 3/4 and Qt
API calls and UI-file syntax, and the Qt environment variables. They're the
standard forms, but check them against your toolkit version before treating a
failure as your application's fault.

**The Windows, macOS and web advice is taken from platform documentation in
the same way, and has not had the live check the GNOME, KDE and X11 claims
did.** That covers the WPF, WinForms, AppKit, SwiftUI and ARIA snippets, the
`GetClassName` and `kCGWindowOwnerName` app-id mechanisms, and the
custom-control causes of an empty tree. Treat it as a starting point rather
than a measurement; pyguitest's
[validation.md](https://github.com/ctrondlp/pyguitest/blob/main/docs/validation.md)
records what real Windows and macOS sessions have actually served.

</details>
