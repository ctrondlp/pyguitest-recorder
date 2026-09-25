#!/usr/bin/env python3
"""Record real input from a real desktop, through real win32 hooks, end to end.

The Windows counterpart of `live-capture-check.py`. Everything in `tests/`
stops at the point where a fake `user32` answers a call; this is the other
half -- a real `SetWindowsHookExW`, a real window, real input, and the whole
pipeline (capture, normalize, infer, generate, validate) run over what comes
back, then the generated script replayed against a fresh window and the result
read back.

Unlike the X11 script, this cannot run on a private, throwaway display --
Windows has no equivalent of Xvfb, and a low-level hook is scoped to whichever
window station its thread is attached to. So this **does** touch the desktop
you are sitting at: it opens a small window of its own, drives it with
synthetic input (`pyguitest`'s own `SendInput`, not a person typing), and
closes it again. It moves the mouse and can steal focus briefly. The
low-level hook this backend installs can technically see every keystroke on
the machine (see `backends/win32.py`'s own module docstring), so leave the
keyboard alone while it runs.

    scripts/win32-live-capture-check.py              # record, generate, replay
    scripts/win32-live-capture-check.py --no-replay  # skip the playback half

Requires an interactive desktop session (a console or RDP login, not a bare
SSH shell with no window station) and the `windows` extra so `uia` can serve
the element half.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from ctypes import wintypes
from pathlib import Path

if sys.platform != "win32":
    sys.exit("win32-live-capture-check.py only runs on Windows")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pyguitest  # noqa: E402
from pyguitest import Role  # noqa: E402

from pyguitest_recorder.analyzer import infer_synchronization  # noqa: E402
from pyguitest_recorder.backends.win32 import unavailable_reason  # noqa: E402
from pyguitest_recorder.config import Settings  # noqa: E402
from pyguitest_recorder.generator import generate, validate  # noqa: E402
from pyguitest_recorder.model import Origin, Recording  # noqa: E402
from pyguitest_recorder.recorder import Recorder  # noqa: E402

PROBE_SCRIPT = Path(__file__).resolve().with_name("win32_probe_window.py")

_user32 = ctypes.windll.user32
_user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
_user32.FindWindowW.restype = wintypes.HWND

_user32.SendMessageTimeoutW.argtypes = (
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.POINTER(wintypes.DWORD),
)
_user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t

_WM_CLOSE = 0x0010
_SMTO_ABORTIFHUNG = 0x0002


class Failure(Exception):
    """The check could not run, or the recorder did not see what was sent."""


def _window_exists(title: str) -> bool:
    """Whether a top-level window with exactly this title exists right now."""
    return bool(_user32.FindWindowW(None, title))


def _wait_until_responsive(title: str) -> None:
    """Block until the probe window is answering messages, not merely present.

    The top-level window exists as soon as `CreateWindowExW` returns, while the
    probe is still creating its children and has not yet reached its message
    loop. Asking UI Automation about it in that gap stalls, and the recorder's
    own session then fails to open and quietly falls back to win32 alone
    ("element resolution off: this session does not provide ELEMENT_GEOMETRY"),
    which is what made this check fail on about every other run.
    """
    hwnd = _user32.FindWindowW(None, title)
    result = wintypes.DWORD()
    for _ in range(50):
        answered = _user32.SendMessageTimeoutW(
            hwnd, 0, 0, 0, _SMTO_ABORTIFHUNG, 200, ctypes.byref(result)
        )
        if answered:
            break
        time.sleep(0.1)
    time.sleep(0.5)


def start_probe(title: str) -> subprocess.Popen[bytes]:
    """Launch the probe window, and confirm it actually appeared."""
    if _window_exists(title):
        raise Failure(f"a window titled {title!r} already exists -- clean up first")
    process = subprocess.Popen(
        [sys.executable, str(PROBE_SCRIPT), "--title", title],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        if _window_exists(title):
            _wait_until_responsive(title)
            return process
        if process.poll() is not None:
            raise Failure(
                f"probe window process exited immediately with {process.returncode}"
            )
        time.sleep(0.1)
    process.terminate()
    raise Failure(f"probe window {title!r} never appeared")


def stop_probe(process: subprocess.Popen[bytes], title: str) -> None:
    """Terminate the probe process, and force-close its window if it lingers.

    A plain hand-rolled window, not a packaged single-instance application, so
    `terminate()` ordinarily takes it down cleanly -- but a check that leaves
    a stray window behind on a failure is worse than one that double-checks.
    """
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    hwnd = _user32.FindWindowW(None, title)
    if hwnd:
        _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        time.sleep(0.3)


def _step(text: str) -> None:
    """Say what the sender is doing, flushed: a redirected run is block-buffered."""
    print(f"  [{time.strftime('%H:%M:%S')}] {text}", flush=True)


def _click_at(gui: pyguitest.Session, element: pyguitest.Element) -> None:
    """Move to the centre of `element`, click, and let the window settle."""
    x, y, w, h = gui.extents(element)
    gui.move_mouse(x + w // 2, y + h // 2)
    time.sleep(0.25)
    gui.click()
    time.sleep(0.6)


def _click_element(
    gui: pyguitest.Session, window_title: str, role: str, name: str | None
) -> None:
    """Click whatever element on `window_title` matches, by its centre.

    By coordinate rather than `Element.click()`: this probe's tabs, menu items
    and checkbox offer UI Automation no Invoke action, so a coordinate click is
    what a real recording of this window would capture too.
    """
    _step(f"click {role} {name!r}")
    win_el = gui.window_element(window_title)
    _click_at(gui, gui.element(role=role, name=name, within=win_el))


def _double_click_element(
    gui: pyguitest.Session, window_title: str, role: str, name: str
) -> None:
    """Double-click an element by name, at its own rectangle.

    For opening a tree branch, which is what a user does and therefore what a
    recording has to contain: `Element.do_action("expand")` would do the same
    thing and produce no input at all, so there would be nothing to record.
    """
    _step(f"double click {role} {name!r}")
    _element(gui, window_title, role=role, name=name).double_click()
    time.sleep(0.6)


_TAB_CONTENTS = {
    # What confirms each page is showing. A page's controls are not in the UI
    # Automation tree at all while another tab is selected -- measured on
    # Windows 11: with List showing, the entry is absent even from a search
    # that asks for invisible elements -- so their presence is the signal that
    # the tab click landed, where a fixed sleep is a guess.
    "General": (Role.ENTRY, None),
    "Advanced": (Role.CHECK_BOX, "Enable feature"),
    "List": (Role.LIST_ITEM, "Alpha"),
    "Groups": (Role.RADIO_BUTTON, "Low"),
}


def _describe(gui: pyguitest.Session, window_title: str) -> str:
    """What a window is publishing right now, for a failure message.

    A tab that never showed its controls is either a click that missed or a
    page that is not being published, and the two are told apart by what the
    tree *does* hold -- plus where the pointer and the focus are, since a
    click that did nothing is usually about one of those. Without this it is a
    Windows-side debugging session away from being visible.
    """
    try:
        active = gui.active_window()
    except Exception as error:  # noqa: BLE001 - a description, not a check
        active = f"{type(error).__name__}"
    try:
        pointer = gui.pointer_position()
    except Exception as error:  # noqa: BLE001 - a description, not a check
        pointer = f"{type(error).__name__}"
    where = f"pointer {pointer}, active {getattr(active, 'title', None)!r}"
    try:
        found = gui.find_elements(within=gui.window_element(window_title))
    except Exception as error:  # noqa: BLE001 - a description, not a check
        return f"{where}; could not read the tree ({type(error).__name__}: {error})"
    showing = ", ".join(f"{one.role}:{one.name!r}" for one in found[:25]) or "nothing"
    return f"{where}; showing: {showing}"


def _focus_window(gui: pyguitest.Session, window_title: str) -> None:
    """Bring the probe window forward, then park the pointer in its body.

    Both halves are load-bearing and the first was missing. Measured on
    Windows 11 with another window on top (the recorder's own VirtualBox
    session): the verifier clicked a tab at the tab's own coordinates and the
    *other* window took the click -- its failure message read
    `active 'GhostBSD 26.1 ... - Oracle VirtualBox'` while the probe was still
    showing the General page, and every later click landed on the VM as well,
    so the tab looked like it simply did not respond. `focus_window` asks for
    the front and confirms it arrived, which is what the driver's
    `activate_window` was already doing and this was not.

    The pointer half is the second way a click goes elsewhere: Windows 11
    opens its Snap Layouts flyout -- a separate `Popup` window -- for as long
    as the pointer rests on a maximize/restore button, and the replay ends
    with it exactly there.
    """
    window = gui.wait_for_window(window_title, timeout=10)
    if window is None:
        raise Failure(f"{window_title!r} is not there to click in")
    gui.focus_window(window)
    x, y, width, height = gui.geometry(window)
    gui.move_mouse(x + width // 2, y + height // 2)
    time.sleep(0.5)


def _element(
    gui: pyguitest.Session,
    window_title: str,
    *,
    role: str,
    name: object = None,
    timeout: float = 10.0,
) -> pyguitest.Element:
    """Find an element, retrying: a miss here is often a moment, not an absence.

    Measured on Windows 11: a lookup that raises ElementNotFound one moment
    returns the element the next, with nothing about the window changed and no
    input in between -- while `find_elements` on the same window finds the
    element the single lookup missed. The control that misses differs run to
    run, so a check that reported it as the product being wrong would be
    reporting its own race.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return gui.element(
                role=role, name=name, within=gui.window_element(window_title)
            )
        except Exception:  # noqa: BLE001 - a missed lookup, not a failure yet
            if time.monotonic() > deadline:
                raise Failure(
                    f"no {role!r} {name!r} after {timeout:g}s; showing: "
                    f"{_describe(gui, window_title)}"
                ) from None
            time.sleep(0.2)


def _select_tab(
    gui: pyguitest.Session, window_title: str, name: str, timeout: float = 10.0
) -> None:
    """Click a page tab, and wait until that page's controls are published."""
    role, label = _TAB_CONTENTS[name]
    _focus_window(gui, window_title)
    _click_element(gui, window_title, Role.PAGE_TAB, name)
    _element(gui, window_title, role=role, name=label, timeout=timeout)


def _click_titlebar_button(
    gui: pyguitest.Session, window_title: str, contains: str
) -> None:
    """Click the title-bar Minimize/Maximize/Restore/Close button by name.

    `contains` rather than an exact match: the same button reads "Maximize"
    before it is pressed and "Restore" afterward.

    The pointer is then parked inside the window's own body. Windows 11 opens
    its Snap Layouts flyout -- a separate `Popup` window -- when the pointer
    rests on a maximize/restore button, and leaving it there made UI Automation
    stop listing the probe window at all (found live: `window_element` raised
    `WindowNotFound` for a window `wait_for_window` could see, with only
    `['', 'Popup']` in UIA's window list) until something moved the pointer.
    """
    _step(f"click title-bar button like {contains!r}")
    win_el = gui.window_element(window_title)
    for element in gui.elements(role=Role.PUSH_BUTTON, within=win_el):
        if contains.lower() in (element.name or "").lower():
            _click_at(gui, element)
            x, y, w, h = gui.geometry(gui.find_window(window_title))
            gui.move_mouse(x + w // 2, y + h // 2)
            time.sleep(0.6)
            return
    raise Failure(f"no title-bar button named like {contains!r} found")


def drive_probe_window(title: str) -> None:
    """Synthesize the interaction the recorder is supposed to see.

    One of each kind that matters for a real desktop application: typed text,
    a plain button click, a tab switch, a dropdown selection, a checkbox
    toggle, a `SysListView32` row selection, a real menu action, a right
    click, a scroll, a double click, a window *drag* mid-recording (no window
    switch, the same window relocating), and a maximize/restore cycle, which
    changes both the window's origin and its size without an intervening
    `WindowActivate` event either.
    """
    gui = pyguitest.connect(key_delay=0.03)
    window = gui.wait_for_window(title, timeout=10)
    if window is None:
        raise Failure("probe window never appeared to pyguitest")
    _step("window found; activating")
    gui.activate_window(window)
    time.sleep(0.4)

    # `wait_for_window` answers from win32 and `window_element` from UI
    # Automation; on a fresh window the two can disagree for a moment.
    for _ in range(20):
        try:
            gui.window_element(title)
            break
        except pyguitest.WindowNotFound:
            time.sleep(0.25)

    _click_element(gui, title, Role.ENTRY, None)
    _step("type text")
    gui.type_text("Ada")
    time.sleep(0.6)

    _click_element(gui, title, Role.PUSH_BUTTON, "Click Me")

    # Drag the window by its title bar -- same window, no activation event,
    # just relocated -- then click the same button again from the new spot.
    _step("drag the window by its title bar")
    x, y, w, _h = gui.geometry(window)
    gui.drag((x + w // 2, y + 15), (x + w // 2 + 200, y + 15 + 160), duration=0.5)
    time.sleep(0.6)
    _click_element(gui, title, Role.PUSH_BUTTON, "Click Me")

    # Maximize, interact while maximized (new origin *and* new size), then
    # restore and interact once more back at the smaller size.
    _click_titlebar_button(gui, title, "Maximize")
    _click_element(gui, title, Role.PUSH_BUTTON, "Click Me")
    _click_titlebar_button(gui, title, "Restore")
    _click_element(gui, title, Role.PUSH_BUTTON, "Click Me")

    _select_tab(gui, title, "Advanced")
    _click_element(gui, title, Role.COMBO_BOX, None)
    time.sleep(0.3)
    _click_element(gui, title, Role.LIST_ITEM, "Beta")
    _click_element(gui, title, Role.CHECK_BOX, "Enable feature")

    # A real SysListView32 offers UI Automation a genuine SelectionItem
    # action, unlike this window's tabs, menu items and checkbox.
    _select_tab(gui, title, "List")
    _click_element(gui, title, Role.LIST_ITEM, "Gamma")
    _select_tab(gui, title, "General")
    _click_element(gui, title, Role.MENU_ITEM, "Actions")
    time.sleep(0.2)
    _click_element(gui, title, Role.MENU_ITEM, "Do Thing")

    # A right click, a scroll, and a double click. The right click's context
    # menu is dismissed with Escape before anything else touches the mouse:
    # left open, the next click lands on the menu instead of the edit box and
    # is (accurately) recorded as a menu interaction, which is not what that
    # click is testing.
    _step("right click, Escape, scroll, double click")
    win_el = gui.window_element(title)
    ex, ey, ew, eh = gui.extents(gui.element(role=Role.ENTRY, within=win_el))
    gui.move_mouse(ex + ew // 2, ey + eh // 2)
    time.sleep(0.2)
    gui.click(button=3)
    time.sleep(0.3)
    gui.tap_key("Escape")
    time.sleep(0.3)
    gui.scroll(0, -1)
    time.sleep(0.3)
    gui.click()
    gui.click()
    time.sleep(0.5)

    # Two radio groups that must not see each other, and a tree whose branch
    # has to be opened before a nested item exists to click. Both are driven
    # with the mouse rather than through the accessibility API: what is under
    # test is what the *recorder* makes of real input, and `Element.select()`
    # would produce nothing to record at all.
    _step("radio groups, and a nested tree")
    _select_tab(gui, title, "Groups")
    _click_element(gui, title, Role.RADIO_BUTTON, "High")
    _click_element(gui, title, Role.RADIO_BUTTON, "Fast")
    _double_click_element(gui, title, Role.TREE_ITEM, "Documents")
    _double_click_element(gui, title, Role.TREE_ITEM, "Reports")
    _click_element(gui, title, Role.TREE_ITEM, "Q1")

    _step("stop chord")
    # The stop chord: two Escapes, exercised rather than only described.
    gui.tap_key("Escape")
    time.sleep(0.3)
    gui.tap_key("Escape")
    time.sleep(0.5)


def report(recording: Recording) -> None:
    """Print what the capture actually saw, before anything is inferred from it."""
    print(f"\ncaptured {len(recording.raw)} raw events")
    for event in recording.raw[:60]:
        button = f"button {event['button']}" if event.get("button") else ""
        name = event.get("keysym") or button
        text = f"  text={event['text']!r}" if event.get("text") else ""
        injected = "  INJECTED" if event.get("injected") else ""
        at = f"at ({event['x']},{event['y']})"
        print(f"    {event['kind']:15} {name:12} {at}{text}{injected}")
    if not recording.raw:
        raise Failure(
            "the hook installed but delivered no events -- this process is "
            "probably not attached to the interactive window station (an SSH "
            "session, a service, or a scheduled task not set to run only "
            "when logged on)"
        )
    print("\nnormalized:", [type(e).__name__ for e in recording.events])
    targets = [getattr(e, "target", None) for e in recording.events]
    windows = {t.window.title for t in targets if t is not None and t.window}
    elements = {
        f"{t.element.role} {t.element.name!r}"
        for t in targets
        if t is not None and t.element
    }
    print("windows resolved: ", sorted(windows) or "none")
    print("elements resolved:", sorted(elements) or "none")
    for note in recording.environment.notes:
        print("    note:", note)


def round_trip(recording: Recording, source: str) -> list[str]:
    """Save the recording, read it back, and check it renders the same."""
    with tempfile.TemporaryDirectory() as directory:
        path = Recording(
            events=recording.events,
            environment=recording.environment,
            started_at=recording.started_at,
        ).save(Path(directory) / "recording.json")
        again = generate(Recording.load(path))
    if again == source:
        print("round trip:", f"{path.name} re-rendered identically")
        return []
    print("round trip: FAILED, the saved recording renders differently")
    return ["a saved recording did not re-render to the same script"]


def _verify_replay(title: str) -> list[str]:
    """Read the replayed window back, and say what did not land.

    Each control is read with its own tab selected. A page's controls are not
    in the UI Automation tree at all while another tab is selected, and the
    replay's last tab click is not something a read can depend on -- so
    reading the entry first, as this did, reported a missing control whenever
    the replay happened not to leave the General page showing.
    """
    gui = pyguitest.connect()
    _select_tab(gui, title, "General")
    problems = []

    entry = _element(gui, title, role=Role.ENTRY)
    if entry.text != "Ada":
        problems.append(f"edit control does not read 'Ada' (got {entry.text!r})")

    # Found by pattern rather than role alone: the ListView's own cells are a
    # table of "text" elements too, hidden or not.
    label = _element(
        gui, title, role=Role.TEXT, name=re.compile(r"^(clicked|menu) \d+$")
    )
    # "Click Me" is clicked 4 times (original position, after the drag, after
    # maximizing, after restoring) and "Do Thing" once after that; the label's
    # handler shares one counter between both, so a clean replay of the whole
    # sequence ends on exactly "menu 5".
    if label.name != "menu 5":
        problems.append(
            "status label does not read 'menu 5' -- not every button and menu "
            f"click landed (got {label.name!r})"
        )

    _select_tab(gui, title, "List")
    gamma = _element(gui, title, role=Role.LIST_ITEM, name="Gamma")
    if not gamma.selected:
        problems.append("ListView row 'Gamma' is not selected after replay")

    _select_tab(gui, title, "Groups")
    problems += _verify_groups(gui, title)
    return problems


def _verify_groups(gui: pyguitest.Session, title: str) -> list[str]:
    """Read the radio groups and the nested tree item back.

    The radios are what shows the groups are *separate*: moving the priority
    group has to leave the mode group where it was, which a single group
    cannot demonstrate. The tree item is what shows a nested one is reachable
    at all -- `Q1` does not exist in the tree until the replay has opened its
    branch, so finding it selected is also the check that the recorded
    double-clicks landed.
    """
    problems = []
    for name, want in (
        ("Low", False),
        ("Medium", False),
        ("High", True),
        ("Fast", True),
        ("Safe", False),
    ):
        selected = _element(gui, title, role=Role.RADIO_BUTTON, name=name).selected
        if bool(selected) != want:
            problems.append(
                f"radio {name!r} reads selected={selected}, expected {want}"
            )
    nested = _element(gui, title, role=Role.TREE_ITEM, name="Q1")
    if not nested.selected:
        problems.append("tree item 'Q1' is not selected after replay")
    return problems


def replay(source: str, title: str) -> list[str]:
    """Run the generated script against a fresh probe window, and report."""
    process = start_probe(title)
    try:
        fd, name = tempfile.mkstemp(suffix=".py")
        os.close(fd)  # left open, Windows refuses to unlink it below
        script = Path(name)
        script.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, timeout=90
        )
        script.unlink(missing_ok=True)
        if result.returncode != 0:
            print("replay stderr:\n", result.stderr)
            return [f"replay exited {result.returncode}"]
        problems = _verify_replay(title)
        if not problems:
            print(
                "replay: PASS -- every click, the menu action, the ListView "
                "selection, both radio groups and the nested tree item landed"
            )
        return problems
    finally:
        stop_probe(process, title)


def _check_the_script_names_its_controls(source: str) -> list[str]:
    """Read the generated script for the preference this package claims.

    The radio and the nested tree item were driven with the *mouse*, and the
    script has to reach for them by name, through `select()`, because a
    coordinate is the one locator that breaks the moment the window moves.
    Asserted rather than assumed: a recording is only as good as what it
    renders, and both of these controls offer a selection alongside UIA's
    `do default action` -- which, on a tree row, is a double click that
    toggles it rather than selecting it.

    Per control, not "a select() somewhere": the ListView row renders as one
    too, and would satisfy a check that only looked for the method.
    """
    problems = []
    for wanted in (
        'role=Role.RADIO_BUTTON, name="High"',
        'role=Role.TREE_ITEM, name="Q1"',
    ):
        if wanted not in source:
            problems.append(f"the generated script does not name {wanted}")
        elif not re.search(re.escape(wanted) + r"[^\n]*\)\.select\(\)", source):
            problems.append(f"{wanted} is named, but not through select()")
    return problems


def check(*, do_replay: bool) -> int:
    """Capture, render, and (optionally) replay. Returns the process exit status."""
    reason = unavailable_reason()
    if reason is not None:
        raise Failure(reason)

    title = f"pyguitest-recorder-check-{int(time.time())}"
    process = start_probe(title)
    settings = Settings(
        backend="win32",
        stop_key="Escape",
        stop_key_presses=2,
        stop_key_interval=2.0,
        record_raw=True,
    )
    recorder = Recorder(settings=settings)
    try:
        recorder.start()
    except BaseException:
        # The probe is already on the desktop, and the try/finally that would
        # close it starts further down: left alone, a failed start leaves the
        # window behind and the next run refuses to begin ("already exists").
        stop_probe(process, title)
        raise
    print(f"capturing win32 input while driving {title!r}")
    print("resolver:", type(recorder._resolver).__name__)
    for note in recorder.recording.environment.notes:
        print("    note:", note)

    sender_error: BaseException | None = None

    def run_sender() -> None:
        """Drive the probe window on its own thread, keeping any failure."""
        nonlocal sender_error
        try:
            drive_probe_window(title)
        except BaseException as exc:  # noqa: BLE001 - reported below, not swallowed
            sender_error = exc

    sender = threading.Thread(target=run_sender, daemon=True)
    watchdog = threading.Timer(90.0, recorder.stop)
    sender.start()
    watchdog.start()
    try:
        recording = recorder.run()
    finally:
        watchdog.cancel()
        recorder.stop()
        sender.join(timeout=5)
        stop_probe(process, title)

    if sender_error is not None:
        where = "".join(traceback.format_exception(sender_error)).rstrip()
        raise Failure(f"driving the probe window failed -- {where}")

    report(recording)
    recording.events = infer_synchronization(recording.events)
    added = [e for e in recording.events if e.origin is Origin.INFERRED]
    print(f"synchronization added: {len(added)}")

    source = generate(recording)
    print("\n" + source)
    problems = validate(source)
    print("validate:", problems or "clean")
    problems += round_trip(recording, source)
    problems += _check_the_script_names_its_controls(source)

    if do_replay:
        problems += replay(source, title)
    else:
        print("replay: skipped (--no-replay)")

    if problems:
        print("\nFAILED:")
        for problem in problems:
            print(" -", problem)
    return 1 if problems else 0


def main() -> int:
    """Parse arguments and run the check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-replay",
        action="store_true",
        help="record and generate only; skip running the generated script",
    )
    args = parser.parse_args()
    try:
        return check(do_replay=not args.no_replay)
    except Failure as exc:
        print(f"win32-live-capture-check: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
