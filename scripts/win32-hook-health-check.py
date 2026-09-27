#!/usr/bin/env python3
"""Prove the hook-health heartbeat notices a hook Windows removed.

The one failure mode `backends/win32.py` cannot be *told* about: a low-level hook
whose callback misses `LowLevelHooksTimeout` is unhooked silently -- no error, no
callback, nothing to catch -- and a recording made across that moment looks
complete. `Win32CaptureBackend._unseen_input_seconds` is what sees it, by
comparing the session's own last-input time with the last time a callback ran,
and no fake can stand in for that: a fake has no session whose last-input time
advances independently of the hooks.

So this removes a real hook behind the real backend's back, on the desktop you
are sitting at:

1. Install the real backend -- real `SetWindowsHookExW`, real callbacks.
2. Inject a few pixels of pointer movement through pyguitest's own `SendInput`,
   and check the hook saw it. **That is the control**: a hook that is firing must
   report no gap, or the heartbeat below proves nothing.
3. `UnhookWindowsHookEx` the mouse hook directly. Windows does this to a slow
   hook without telling anyone; doing it by hand is the only way to produce the
   state deliberately.
4. Keep injecting movement and watch `hook_lost_seconds` go from None to the size
   of the gap -- which is exactly what the recorder turns into a note on the
   recording (`recorder._hook_lost_note`).

Pointer motion only, a few pixels, and the pointer's original position is put
back at the end. The keyboard is never touched, and no window is opened or
focused. `--no-inject` skips the movement entirely, which leaves the backend
running and reporting, for a machine where even a nudge is unwelcome.

    scripts/win32-hook-health-check.py
    scripts/win32-hook-health-check.py --timeout 20

Requires an interactive desktop session (a console or RDP login, not a bare SSH
shell with no window station) and pyguitest's `win32` input backend, which is
ctypes and needs no extra.
"""

from __future__ import annotations

import argparse
import sys
import time

from pyguitest_recorder.backends import win32 as win32_module
from pyguitest_recorder.backends.win32 import HOOK_LOST_SLACK, Win32CaptureBackend


class Failure(Exception):
    """The check could not be run at all, as opposed to failing."""


def say(message: str = "") -> None:
    """Print a line, flushed, so a run that hangs still says where it got to."""
    print(message, flush=True)


def motion_events(backend: Win32CaptureBackend) -> int:
    """How many motion events the backend's queue holds, without waiting."""
    return sum(1 for event in backend.drain() if event.kind == "motion")


def nudge(gui, origin: tuple[int, int], width: int, offset: int) -> None:
    """Move the pointer a few pixels from `origin`.

    SendInput rather than a cursor-position set: injected events are what a
    low-level hook sees (with `LLMHF_INJECTED` set, which this backend records),
    and they are also what advances the session's last-input time -- which is the
    reading the heartbeat is built on. A cursor move that never went through the
    input queue would prove nothing about either.
    """
    x = min(max(origin[0] + offset, 0), max(width - 2, 0))
    gui.move_mouse(x, origin[1])


def _control(backend, gui, origin, width, do_inject) -> list[str]:
    """Step 1: a hook that is firing must report nothing.

    Without this the rest proves nothing: a heartbeat that answered "lost" for
    every session, working hook included, would pass step 3 too.
    """
    say("== 1. the control: a live hook, and nothing to report ==")
    if not do_inject:
        say("  injection skipped (--no-inject)")
    else:
        for offset in (3, 0):
            nudge(gui, origin, width, offset)
            time.sleep(0.15)
        seen = motion_events(backend)
        say(f"  the hook saw {seen} motion event(s)")
        if not seen:
            return [
                "the hooks saw no injected motion at all, so nothing below would "
                "mean anything (is another low-level hook ahead of this one, or "
                "is the session not interactive?)"
            ]
    if backend.hook_lost_seconds is not None:
        return [
            "a working hook reported a gap of "
            f"{backend.hook_lost_seconds:.1f}s -- the heartbeat is measuring "
            "something other than a removed hook"
        ]
    say(f"  hook_lost_seconds is {backend.hook_lost_seconds!r}")
    say()
    return []


def _remove_the_mouse_hook(backend) -> None:
    """Step 2: take the mouse hook away, without telling the backend.

    This is what Windows does to a callback that misses the timeout; the only
    difference is that doing it here leaves a process that can still report on
    it.
    """
    say("== 2. remove the mouse hook, the way Windows does ==")
    handle = backend._mouse_hook
    if not handle:
        raise Failure("no mouse hook was installed, so nothing can be removed")
    # Nulled as well as removed: `stop` unhooks whatever is held, and this handle
    # is no longer a live hook. The keyboard hook stays installed.
    backend._mouse_hook = None
    if not win32_module._user32().UnhookWindowsHookEx(handle):
        raise Failure("UnhookWindowsHookEx refused the mouse hook")
    say(f"  unhooked handle {handle}; the backend was not told, which is the point")
    say()


def _await_the_gap(backend, gui, origin, width, do_inject, timeout) -> list[str]:
    """Step 3: keep the session busy until the heartbeat answers."""
    say("== 3. keep the session busy and watch the heartbeat ==")
    deadline = time.monotonic() + timeout
    while backend.hook_lost_seconds is None and time.monotonic() < deadline:
        if do_inject:
            nudge(gui, origin, width, 3)
        time.sleep(0.25)
    gap = backend.hook_lost_seconds
    if gap is None:
        say("  hook_lost_seconds is still None")
        return [
            f"the heartbeat did not report the removed hook within {timeout:.0f}s "
            f"(slack is {HOOK_LOST_SLACK}s)"
        ]
    say(f"  hook_lost_seconds is {gap:.1f}s")
    problems = []
    if gap < HOOK_LOST_SLACK:
        problems.append(
            f"the gap reported ({gap:.1f}s) is below the threshold "
            f"({HOOK_LOST_SLACK}s), which the loop should not allow"
        )
    say()
    say("== what the recording would say ==")
    from pyguitest_recorder.recorder import _hook_lost_note

    say(f"  {_hook_lost_note(gap)}")
    return problems


def _put_the_pointer_back(gui, origin, do_inject) -> None:
    """Leave the desktop where it was found."""
    if not do_inject:
        return
    try:
        gui.move_mouse(origin[0], origin[1])
        say()
        say(f"pointer put back at {origin}")
    except Exception as exc:  # noqa: BLE001 - cosmetic, and reported
        say(f"could not put the pointer back: {exc}")


def _report(problems: list[str]) -> int:
    """Say what came of it, and answer the exit status."""
    if not problems:
        say()
        say("the heartbeat noticed a hook Windows removed without saying so")
        return 0
    say()
    say("FAILED:")
    for problem in problems:
        say(f" - {problem}")
    return 1


def check(*, do_inject: bool, timeout: float) -> int:
    """Run the control, remove a hook, and report what the heartbeat saw."""
    import pyguitest

    if sys.platform != "win32":
        raise Failure(f"this is not a native Windows process; it is {sys.platform!r}")
    reason = win32_module.unavailable_reason()
    if reason is not None:
        raise Failure(reason)

    gui = pyguitest.connect(backend=["win32"])
    try:
        origin = tuple(gui.pointer_position())[:2]
        width = int(gui.screens()[0].width)
    except Exception as exc:  # noqa: BLE001 - reported as "cannot run" below
        gui.close()
        raise Failure(f"this backend cannot read the pointer here: {exc}") from exc
    say(f"pointer starts at {origin}, screen {width} wide")
    say()

    backend = Win32CaptureBackend()
    problems: list[str] = []
    backend.start()
    try:
        problems += _control(backend, gui, origin, width, do_inject)
        _remove_the_mouse_hook(backend)
        problems += _await_the_gap(backend, gui, origin, width, do_inject, timeout)
    finally:
        backend.stop()
        _put_the_pointer_back(gui, origin, do_inject)
        gui.close()
    return _report(problems)


def main() -> int:
    """Parse arguments and run the check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-inject",
        dest="inject",
        action="store_false",
        help="do not move the pointer at all; start the backend and report only",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="seconds to wait for the heartbeat to notice (default: %(default)s)",
    )
    args = parser.parse_args()
    try:
        return check(do_inject=args.inject, timeout=args.timeout)
    except Failure as exc:
        print(f"win32-hook-health-check: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
