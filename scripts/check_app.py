#!/usr/bin/env python3
"""A window at a position of its own, for the live capture check.

`zenity` cannot be told where to put itself, and a bare X server has no window
manager to place it, so two dialogs land exactly on top of each other at the
origin. That makes "which window is under this point" unanswerable, and the
recorder's window-switching behaviour -- raising a window the recording came
back to -- cannot be exercised at all.

This is the smallest thing that fixes that: a GTK window that takes its
position on the command line. Deliberately not a fixture with any behaviour;
it exists to be somewhere, with a title, and to accept a click.
"""

from __future__ import annotations

import argparse
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402


def main() -> int:
    """Open one window where the caller asked for it, and wait."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="Recorder Check Two")
    parser.add_argument("--at", nargs=2, type=int, default=(600, 400))
    parser.add_argument("--size", nargs=2, type=int, default=(320, 200))
    args = parser.parse_args()

    window = Gtk.Window(title=args.title)
    window.set_default_size(*args.size)
    window.move(*args.at)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    box.set_border_width(16)
    entry = Gtk.Entry()
    entry.get_accessible().set_name("Second")
    box.pack_start(entry, False, False, 0)
    box.pack_start(Gtk.Button(label="Apply"), False, False, 0)
    window.add(box)
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
