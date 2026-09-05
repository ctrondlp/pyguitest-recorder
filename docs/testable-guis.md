# How to build a GUI that can be tested

**Who this is for:** application developers. It is written to be handed to you
by whoever has to automate your UI, and it asks for about a day of work spread
across a codebase.

**The short version:** automated GUI tests find your widgets the same way a
screen reader does — through the accessibility tree. If a button has a name
there, a test can click it by name and the test survives redesigns. If it
doesn't, the test has to click pixel coordinates, and it breaks the next time
anything moves.

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
| Give the window a **stable app id** | Tests survive a title that changes when the document does |
| Report widget positions in **screen coordinates** | "What's under the mouse here?" gets the right answer |
| Make long operations **visible** — a status message, a disabled button | Tests can wait for your app to be ready instead of sleeping for two seconds |

If you only do the first row, you have already fixed most of it.

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

Three things that trip people up:

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

- Set the app id (`WM_CLASS` on X11, the Wayland app id) to your reverse-DNS
  application id, and don't vary it per document. Tools prefer it over the
  title when they can.
- Keep the constant part of the title predictable — "Untitled — MyApp" is
  easier to match than "MyApp — Untitled".
- **Give dialogs real titles.** An untitled modal can only be found as
  "whatever window just appeared".

## 6. Report positions in screen coordinates

This is the one that produces tests that pass while doing the wrong thing, so
it's worth a direct check.

When something asks your widget where it is, the answer must be where it
actually is *on the screen* — not relative to the window, and not `(0, 0)`.

This isn't hypothetical. A GTK 4 dialog on Fedora 45 reported *every* widget at
`(0, 0, 120, 44)`, so "what is at this point?" returned the same answer for
every point in the window. Nothing errored. Tools that trusted it clicked
confidently on entirely the wrong widget.

Mostly this is your toolkit's job and it just works. Check it directly if you
maintain custom widgets, and check it **on a second monitor and at a non-100%
scale factor**, which is where the bugs live.

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

- **Report which widget has keyboard focus.** Tab-order tests depend on it.
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

It fails with a list of every unnamed or ambiguously-named control. Add it once
per screen and the *next* unnamed toolbar button fails the build on the day
it's added, instead of six months later when someone tries to test it.

**Record a session against your app.** Every click that comes out as
`gui.button("Save").click()` is a control you got right; every click that comes
out as a raw coordinate is one to fix. The generated script says at the top
which ones it couldn't name, and why.

---

## Checklist

- [ ] Every button, field, checkbox, menu item and tab has a name
- [ ] Icon-only buttons have explicit labels, not just tooltips
- [ ] No duplicate names within a single window
- [ ] Names don't change when state changes
- [ ] Widget types match behaviour — no buttons made of boxes
- [ ] Custom widgets publish their contents, not one opaque rectangle
- [ ] Window app id is stable; dialogs have titles
- [ ] Positions are correct on a second monitor and at non-100% scaling
- [ ] Enabled / visible / checked reflect reality
- [ ] Long operations show something that appears and disappears
- [ ] One `assert_accessible` per screen, running in CI

---

<details>
<summary>Where these claims come from</summary>

Measured on live desktops by pyguitest or this recorder: the GTK 4 widget
position bug (zenity, Fedora 45); `toolkit-accessibility` off by default on KDE
and the silent empty results that follow (2026-09-01); Chromium and Electron
absent from the accessibility tree while the system-wide flag is false (GNOME
Shell 51, 2026-09-05); no per-widget focus reporting on GNOME Shell 50.4
Wayland across three toolkits; window titles drifting under GNOME Text Editor.
Details in pyguitest's
[validation.md](https://github.com/ctrondlp/pyguitest/blob/main/docs/validation.md).

Taken from toolkit documentation rather than verified here: the GTK 3/4 and Qt
API calls and UI-file syntax, and the Qt environment variables. They're the
standard forms, but check them against your toolkit version before treating a
failure as your application's fault.

</details>
