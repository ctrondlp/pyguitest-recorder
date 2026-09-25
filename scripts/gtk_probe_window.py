#!/usr/bin/env python3
"""A GTK window with real, driveable controls, for the live X11 checks.

The X11 counterpart of `win32_probe_window.py`, and it exists for the same
reason: the routine live check drove two bare GTK windows -- an entry and a
label -- so every control a real application is mostly *made of* went
unexercised on this platform, while the Windows side had a tab control, a
combo box, a list view and a real menu bar to aim at. A click on a combo box
is not a click on a button as far as the resolver is concerned: it publishes
a different role, offers a different set of AT-SPI actions, and opens a popup
that is a window of its own.

Deliberately purpose-built rather than a real application, and for a
different reason than the Windows one. Real GTK applications are fine to
record -- several are recorded in `docs/developers/status.md` -- but they
move: a toolkit release renames a widget, a distribution patches a menu, and
a check that was asserting something specific quietly starts asserting
something else. What this window publishes is fixed here, in this file, and
changes only when someone changes it.

Every widget carries an explicit accessible name, because that is the thing
under test: the recorder's whole claim is that a click becomes
`gui.button("Save")` rather than a coordinate, and a widget with no name
cannot demonstrate either outcome.

    python gtk_probe_window.py --title "Probe" --at 100 100 --size 520 400
"""

from __future__ import annotations

import argparse
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

COMBO_ITEMS = ("Alpha", "Beta", "Gamma")
"""What the combo box offers. Distinct words, so a wrong selection is obvious."""

LIST_ROWS = ("First row", "Second row", "Third row")
"""Rows in the tree view, named the same way and for the same reason."""


def _named(widget: Gtk.Widget, name: str) -> Gtk.Widget:
    """Give a widget an accessible name and return it.

    Every widget here goes through this. A GTK widget's accessible name comes
    from its label where it has one, and from nothing at all where it does
    not -- an entry, a tree view -- so the half of this window that most needs
    naming is exactly the half that would otherwise have no name.
    """
    widget.get_accessible().set_name(name)
    return widget


def _menu_bar() -> Gtk.MenuBar:
    """A real menu bar, whose items are a window of their own when open.

    The one control here that is not simply a widget in the frame: an open
    GTK menu is an override-redirect window stacked over the application, and
    a click on an item in it is the case the resolver's popup handling exists
    for -- see `_popup_at` in the resolver, and the live finding that a click
    on `New` was recorded as the toolbar button underneath it.
    """
    bar = Gtk.MenuBar()
    for menu_name, items in (
        ("File", ("New", "Open", "Save")),
        ("Edit", ("Cut", "Copy", "Paste")),
    ):
        root = _named(Gtk.MenuItem(label=menu_name), menu_name)
        submenu = Gtk.Menu()
        for item_name in items:
            submenu.append(_named(Gtk.MenuItem(label=item_name), item_name))
        root.set_submenu(submenu)
        bar.append(root)
    return bar


def _open_confirm_dialog(button: Gtk.Button) -> None:
    """Raise a modal confirmation, and close it on either answer.

    Deliberately `Gtk.Dialog` with its own action buttons rather than a
    `MessageDialog`: the buttons then carry the names given here rather than
    stock ones, so a recording of it asserts something this file controls.
    """
    dialog = Gtk.Dialog(
        title="Confirm Action",
        transient_for=button.get_toplevel(),
        modal=True,
    )
    dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button("OK", Gtk.ResponseType.OK)
    content = dialog.get_content_area()
    content.set_border_width(12)
    content.pack_start(
        _named(Gtk.Label(label="Are you sure?"), "Are you sure?"), True, True, 0
    )
    dialog.show_all()
    dialog.run()
    dialog.destroy()


def _controls() -> Gtk.Widget:
    """One of each control worth telling apart, laid out in a single column."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

    box.pack_start(_named(Gtk.Entry(), "Name"), False, False, 0)

    # A real password field, which AT-SPI publishes as role `password text`
    # rather than `text` -- the one signal the recorder's redaction has to
    # key off, and something no amount of unit testing can produce.
    secret = Gtk.Entry()
    secret.set_visibility(False)
    box.pack_start(_named(secret, "Password"), False, False, 0)

    box.pack_start(_named(Gtk.Button(label="Save"), "Save"), False, False, 0)

    # A modal dialog, which is what a Save or Delete confirmation is in every
    # application that has one -- a second toplevel, with a grab, that the
    # recording has to follow into and back out of.
    confirm = _named(Gtk.Button(label="Confirm"), "Confirm")
    confirm.connect("clicked", _open_confirm_dialog)
    box.pack_start(confirm, False, False, 0)
    box.pack_start(_named(Gtk.CheckButton(label="Enabled"), "Enabled"), False, False, 0)

    first = Gtk.RadioButton.new_with_label(None, "Small")
    second = Gtk.RadioButton.new_with_label_from_widget(first, "Large")
    box.pack_start(_named(first, "Small"), False, False, 0)
    box.pack_start(_named(second, "Large"), False, False, 0)

    # A second, independent group. GTK scopes a radio group to the widget it
    # was created *from*, so one started with None above and one started from
    # `compact` here are two groups -- selecting in one leaves the other
    # alone, which a single group cannot demonstrate and which a resolver
    # that answers "the radio named X" has to get right.
    compact = Gtk.RadioButton.new_with_label(None, "Compact")
    roomy = Gtk.RadioButton.new_with_label_from_widget(compact, "Comfortable")
    box.pack_start(_named(compact, "Compact"), False, False, 0)
    box.pack_start(_named(roomy, "Comfortable"), False, False, 0)

    combo = Gtk.ComboBoxText()
    for item in COMBO_ITEMS:
        combo.append_text(item)
    combo.set_active(0)
    box.pack_start(_named(combo, "Size"), False, False, 0)

    spin = Gtk.SpinButton.new_with_range(0, 100, 1)
    spin.set_value(10)
    box.pack_start(_named(spin, "Count"), False, False, 0)

    return box


def _tree() -> Gtk.Widget:
    """A hierarchical view, where the List page's is flat.

    A tree item is its own shape to AT-SPI: role `tree item`, with an
    expanded/collapsed state of its own, and children that do not exist to be
    found at all until their parent has been opened. The List page cannot
    exercise any of that.
    """
    store = Gtk.TreeStore(str)
    documents = store.append(None, ["Documents"])
    reports = store.append(documents, ["Reports"])
    store.append(reports, ["Q1"])
    store.append(reports, ["Q2"])
    store.append(documents, ["Notes"])
    store.append(None, ["Trash"])
    view = Gtk.TreeView(model=store)
    view.append_column(Gtk.TreeViewColumn("Folder", Gtk.CellRendererText(), text=0))
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.pack_start(_named(view, "Folders"), True, True, 0)
    return box


def _tabs() -> Gtk.Notebook:
    """A notebook, so switching pages is a recordable act on this platform too.

    The Windows probe has `SysTabControl32` and this had nothing equivalent,
    which mattered: a tab is one of the few controls whose *click* changes
    what every later coordinate in a recording refers to.
    """
    notebook = Gtk.Notebook()
    notebook.append_page(_controls(), _named(Gtk.Label(label="Controls"), "Controls"))

    store = Gtk.ListStore(str)
    for row in LIST_ROWS:
        store.append([row])
    view = Gtk.TreeView(model=store)
    view.append_column(Gtk.TreeViewColumn("Item", Gtk.CellRendererText(), text=0))
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.pack_start(_named(view, "Items"), True, True, 0)
    notebook.append_page(box, _named(Gtk.Label(label="List"), "List"))

    notebook.append_page(_tree(), _named(Gtk.Label(label="Tree"), "Tree"))
    return notebook


def main() -> int:
    """Open the probe window where the caller asked for it, and wait."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="Probe Window")
    parser.add_argument("--at", nargs=2, type=int, default=(100, 100))
    parser.add_argument("--size", nargs=2, type=int, default=(520, 400))
    args = parser.parse_args()

    window = Gtk.Window(title=args.title)
    window.set_default_size(*args.size)
    window.move(*args.at)
    window.connect("destroy", Gtk.main_quit)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    outer.pack_start(_menu_bar(), False, False, 0)
    inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    inner.set_border_width(12)
    inner.pack_start(_tabs(), True, True, 0)
    outer.pack_start(inner, True, True, 0)
    window.add(outer)

    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
