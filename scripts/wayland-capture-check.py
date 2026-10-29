#!/usr/bin/env python3
"""Live diagnostic for the Wayland input-capture path (evdev + AT-SPI fusion).

EXPERIMENTAL spike, not part of the shipped recorder. Proves the two halves
compose on a real Wayland desktop: the `wayland` capture backend reads real
input below the compositor, a passive AT-SPI listener observes the effects,
and `analyzer.atspi_fusion.fuse_targets` correlates them into targeted actions.

Run it inside a graphical session (not a bare ssh shell -- export
WAYLAND_DISPLAY/DBUS_SESSION_BUS_ADDRESS/XDG_CURRENT_DESKTOP first), then
click and type in whatever window you want recorded:

    python scripts/wayland-capture-check.py [seconds]

Needs: the `input` group (to read /dev/input), python-evdev, and gi/AT-SPI.
Prints the fused actions and writes the raw streams to /tmp for inspection.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 30


def collect_atspi(effects: list, duration: int) -> None:
    """Run an AT-SPI event loop for `duration`s, appending effect dicts."""
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib

    def on_event(ev):
        src = ev.source
        try:
            name = src.get_name() if src else None
            role = src.get_role_name() if src else None
        except Exception:
            name = role = None
        effects.append(
            # time.monotonic, not time.time: the backend rebases every input
            # stamp onto the monotonic clock, and `fuse_targets` correlates
            # effects to inputs within 0.6s -- epoch stamps here would never
            # match a rebased input again.
            {
                "type": ev.type,
                "wall": round(time.monotonic(), 4),
                "name": name,
                "role": role,
            }
        )

    listeners = []
    for et in (
        "object:state-changed:focused",
        "object:state-changed:checked",
        "object:state-changed:selected",
        "object:text-changed:insert",
        "object:selection-changed",
        "object:active-descendant-changed",
        "object:children-changed",
        "focus:",
        "window:activate",
    ):
        el = Atspi.EventListener.new(on_event)
        el.register(et)
        listeners.append(el)
    GLib.timeout_add_seconds(duration, lambda: (Atspi.event_quit(), False)[1])
    Atspi.event_main()


def main() -> int:
    """Capture, fuse and print the recorded actions; return a process code."""
    from pyguitest_recorder.analyzer.atspi_fusion import fuse_targets
    from pyguitest_recorder.backends.wayland import WaylandCaptureBackend, available

    if not available():
        print(
            "no readable evdev device -- add this user to the 'input' group",
            file=sys.stderr,
        )
        return 1

    effects: list = []
    atspi_thread = threading.Thread(
        target=collect_atspi, args=(effects, SECONDS), daemon=True
    )
    atspi_thread.start()

    backend = WaylandCaptureBackend()
    backend.start()
    print(f">>> INTERACT NOW for {SECONDS}s: click controls and type <<<", flush=True)
    time.sleep(SECONDS + 1)

    inputs = [ev.to_dict() for ev in backend.drain()]
    backend.stop()
    atspi_thread.join(timeout=3)

    actions = fuse_targets(inputs, effects)
    print(
        f"\n=== {len(inputs)} input records, {len(effects)} AT-SPI effects "
        f"-> {len(actions)} actions ==="
    )
    for a in actions:
        tgt = a.get("target")
        where = f" -> {tgt['role']} {tgt['name']!r}" if tgt else ""
        conf = f" [{a['confidence']}]" if "confidence" in a else ""
        if a["action"] == "type":
            print(f"  type({a['text']!r}){where}{conf}")
        elif a["action"] == "shortcut":
            print(f"  shortcut({a['keys']})")
        elif a["action"] == "key":
            print(f"  key({a['keys']})")
        elif a["action"] == "click":
            print(f"  click(button={a['button']}){where}{conf}")
        elif a["action"] == "scroll":
            print(f"  scroll(dx={a['dx']}, dy={a['dy']})")
    # A private, uniquely named file (mkstemp creates it 0600): the dump holds
    # every raw `text` field the run captured -- whole-machine keystrokes --
    # and a fixed world-readable /tmp path handed that to any local user.
    fd, dump_path = tempfile.mkstemp(prefix="wayland_capture_dump_", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(
            {"inputs": inputs, "effects": effects, "actions": actions}, fh, indent=2
        )
    print(f"\nraw streams -> {dump_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
