#!/usr/bin/env python3
"""Record real input from a real desktop, through a real CGEventTap, end to end.

The macOS counterpart of `win32-live-capture-check.py`, and the one the
review (and `docs/`) flagged as missing: Linux and Windows each had a live
capture script, macOS had only a *settings* check. Everything in `tests/`
stops where a fake answers; this is the other half -- a real event tap, a
real window, real synthetic input, and the whole pipeline (capture,
normalize, infer, generate, validate) run over what comes back, then the
generated script replayed against a fresh window and the result read back.

Deliberately smaller than the Win32 script, matching `macos_probe_window.py`:
one text field, one push button, one checkbox, two radio buttons in a group.
That is enough to exercise typed text, a plain click, a toggle, a radio
selection, named-element resolution, and -- the point of a *recorder* check
-- that the capture backend sees synthetic input at all and that the
generated script drives named controls rather than bare coordinates.

This touches the desktop you are at. A CGEventTap sees every event on the
session (see `backends/macos.py`'s own docstring), and the sender moves the
real pointer and types into the probe window. Over SSH this is the
`Background` launchd domain, so the app is started with pyguitest's own
`start_app`; the tap itself needs the Accessibility grant that
`macos-live-settings-check.py` and `pyguitest doctor` describe. Leave the
keyboard alone while it runs.

    scripts/macos-live-capture-check.py              # record, generate, replay
    scripts/macos-live-capture-check.py --no-replay  # skip the playback half

Requires a logged-in Aqua session (not a bare SSH shell) and the `macos`
extra so the accessibility element half can serve.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

if sys.platform != "darwin":
    sys.exit("macos-live-capture-check.py only runs on macOS")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pyguitest  # noqa: E402
from pyguitest import Role  # noqa: E402

from pyguitest_recorder.analyzer import infer_synchronization  # noqa: E402
from pyguitest_recorder.backends.macos import unavailable_reason  # noqa: E402
from pyguitest_recorder.config import Settings  # noqa: E402
from pyguitest_recorder.generator import generate, validate  # noqa: E402
from pyguitest_recorder.model import Origin, Recording  # noqa: E402
from pyguitest_recorder.recorder import Recorder  # noqa: E402

PROBE_SCRIPT = Path(__file__).resolve().with_name("macos_probe_window.py")

# The capture backend taps the seat; the element and input halves are the
# AX/Quartz band, in the order `macos-live-settings-check.py` already uses.
_BACKENDS = ["macquartz", "macos"]


class Failure(Exception):
    """The check could not run, or the recorder did not see what was sent."""


def _step(text: str) -> None:
    """Say what the sender is doing, flushed: a redirected run is block-buffered."""
    print(f"  [{time.strftime('%H:%M:%S')}] {text}", flush=True)


def start_probe(title: str) -> subprocess.Popen[bytes]:
    """Launch the probe window and confirm it reached the accessibility tree.

    The window exists for AppKit before it is published to AX, so this waits
    on a pyguitest lookup rather than on the process being alive -- the same
    gap the Win32 script's `_wait_until_responsive` closes there.
    """
    process = subprocess.Popen(
        [sys.executable, str(PROBE_SCRIPT), "--title", title],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # The probe is on the desktop from here on, and the try/finally that
    # closes it belongs to the caller, who has no handle yet: anything that
    # raises below -- `connect()` refusing without the Accessibility grant is
    # the likely one -- must take the window down itself, or it outlives the
    # check and the next run finds a stale probe.
    try:
        gui = pyguitest.connect(backend=_BACKENDS)
        for _ in range(60):
            if process.poll() is not None:
                raise Failure(
                    f"probe window process exited immediately with {process.returncode}"
                )
            if gui.wait_for_window(title, timeout=0.5) is not None:
                time.sleep(0.5)
                return process
            time.sleep(0.1)
        raise Failure(f"probe window {title!r} never appeared to pyguitest")
    except BaseException:
        if process.poll() is None:
            stop_probe(process)
        raise


def stop_probe(process: subprocess.Popen[bytes]) -> None:
    """Terminate the probe process, hard if it will not go on its own."""
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _element(
    gui: pyguitest.Session,
    title: str,
    *,
    role: str,
    name: object = None,
    timeout: float = 10.0,
) -> pyguitest.Element:
    """Find an element, retrying: a miss here is often a moment, not an absence.

    The same tolerance the Win32 script needs, for the same reason -- a
    lookup can raise one moment and return the next with nothing changed.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return gui.element(role=role, name=name, within=gui.window_element(title))
        except Exception:  # noqa: BLE001 - a missed lookup, not a failure yet
            if time.monotonic() > deadline:
                raise Failure(
                    f"no {role!r} {name!r} after {timeout:g}s on {title!r}"
                ) from None
            time.sleep(0.2)


def _click_at(gui: pyguitest.Session, element: pyguitest.Element) -> None:
    """Move to the centre of `element`, click, and let the window settle."""
    x, y, w, h = gui.extents(element)
    gui.move_mouse(x + w // 2, y + h // 2)
    time.sleep(0.25)
    gui.click()
    time.sleep(0.5)


def drive_probe_window(title: str) -> None:
    """Synthesize the interaction the recorder is supposed to see.

    One of each kind the macOS probe can show: typed text, a plain button
    click, a checkbox toggle, and a radio selection -- each by the mouse or
    the keyboard, never through the accessibility API, because what is under
    test is what the *recorder* makes of real input and `Element.click()`
    would produce nothing to record.
    """
    gui = pyguitest.connect(backend=_BACKENDS, key_delay=0.03)
    window = gui.wait_for_window(title, timeout=10)
    if window is None:
        raise Failure("probe window never appeared to pyguitest")
    _step("window found; activating")
    gui.activate_window(window)
    time.sleep(0.5)

    _step("click the text field and type")
    _click_at(gui, _element(gui, title, role=Role.ENTRY))
    gui.type_text("Ada")
    time.sleep(0.5)

    _step("click the push button")
    _click_at(gui, _element(gui, title, role=Role.PUSH_BUTTON, name="Click Me"))

    _step("toggle the checkbox")
    _click_at(gui, _element(gui, title, role=Role.CHECK_BOX, name="Enable feature"))

    _step("select the second radio button")
    _click_at(gui, _element(gui, title, role=Role.RADIO_BUTTON, name="Two"))

    _step("stop chord")
    # Two Escapes, exercised rather than only described.
    gui.tap_key("Escape")
    time.sleep(0.3)
    gui.tap_key("Escape")
    time.sleep(0.5)


def report(recording: Recording) -> None:
    """Print what the capture actually saw, before anything is inferred."""
    print(f"\ncaptured {len(recording.raw)} raw events")
    for event in recording.raw[:40]:
        button = f"button {event['button']}" if event.get("button") else ""
        name = event.get("keysym") or button
        text = f"  text={event['text']!r}" if event.get("text") else ""
        at = f"at ({event['x']},{event['y']})"
        print(f"    {event['kind']:15} {name:12} {at}{text}")
    if not recording.raw:
        raise Failure(
            "the event tap installed but delivered no events -- this process "
            "is probably not in an Aqua session, or the Accessibility grant "
            "the tap needs is not in place (see macos-live-settings-check.py)"
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


def _check_the_script_names_its_controls(source: str) -> list[str]:
    """Check the generated script names its controls rather than coordinates.

    The button, checkbox and radio were driven by mouse; the script must
    reach them by *name*, since a coordinate breaks the moment the window
    moves. Asserted rather than assumed, which is the recorder's whole claim.

    Each control is matched by how the generator actually renders it, which
    differs by control: a push button and a checkbox get the dedicated
    `gui.button(...)` / `gui.checkbox(...)` helpers, and a radio button --
    which has no such helper -- gets the generic `role=..., name=...` form.
    A check that looked for `name="X"` across the board would miss the
    helper forms and report a clean recording as broken, which is exactly
    what the first version of this did.

    The quote style is not the generator's to promise either: it renders
    strings through `ast.unparse`, which writes single quotes, and only a
    `ruff format` pass -- run when ruff happens to be installed, and ruff is a
    dev dependency -- turns them into double ones. A box without ruff produces
    `gui.button('Click Me')`, a correct script this check used to report as
    missing every control. The needles are matched against the source with
    its quotes folded to one style; none of the names has a quote in it.
    """
    wanted = (
        '"Click Me"',  # gui.button("Click Me", ...)
        '"Enable feature"',  # gui.checkbox("Enable feature", ...)
        'name="Two"',  # gui.element(role=Role.RADIO_BUTTON, name="Two", ...)
    )
    folded = source.replace("'", '"')
    problems = []
    for needle in wanted:
        if needle not in folded:
            problems.append(f"the generated script does not name {needle}")
    # And no control may have fallen through to a bare coordinate click: a
    # `.click()` with no element before it is the locator that breaks when
    # the window moves, and the thing this whole check exists to rule out.
    # One bare click is legitimate -- the text field is clicked to focus it
    # before typing, and it has no stable name of its own here. More than one
    # means a named control was missed.
    bare = re.findall(r"^\s*gui\.click\(\)\s*$", source, re.MULTILINE)
    if len(bare) > 1:
        problems.append("more than one control was driven by bare coordinate")
    return problems


def _verify_replay(title: str) -> list[str]:
    """Read the replayed window back, and say what did not land.

    Each control is read by the attribute it actually publishes, measured on
    this probe (Sequoia): the text field's content is `.text`, the status
    label's is `.text` too (its `name` is empty, and it starts at "not
    clicked" and becomes "clicked N" on a click -- so the test is the "not"
    going away, not the word "clicked" appearing, which the initial text
    already contains), and the checkbox and radio carry their state as a
    `.value` of 0.0/1.0 rather than as a boolean.
    """
    gui = pyguitest.connect(backend=_BACKENDS)
    problems = []
    win = gui.window_element(title)

    entry = _element(gui, title, role=Role.ENTRY)
    if entry.text != "Ada":
        problems.append(f"text field does not read 'Ada' (got {entry.text!r})")

    labels = [
        (el.text or "")
        for el in gui.find_elements(role=Role.LABEL, within=win)
        if "clicked" in (el.text or "")
    ]
    status = labels[0] if labels else ""
    if status.startswith("clicked"):
        pass  # "clicked N" -- the click landed
    else:
        problems.append(f"status label never showed a click (got {status!r})")

    checkbox = _element(gui, title, role=Role.CHECK_BOX, name="Enable feature")
    if not checkbox.value:
        problems.append(f"checkbox is not ticked after replay (value={checkbox.value})")

    radio_two = _element(gui, title, role=Role.RADIO_BUTTON, name="Two")
    if not radio_two.value:
        problems.append(f"radio 'Two' is not selected (value={radio_two.value})")

    return problems


def replay(source: str, title: str) -> list[str]:
    """Run the generated script against a fresh probe window, and report."""
    process = start_probe(title)
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(source)
            script = Path(handle.name)
        try:
            result = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=90,
            )
        finally:
            script.unlink(missing_ok=True)
        if result.returncode != 0:
            print("replay stderr:\n", result.stderr)
            return [f"replay exited {result.returncode}"]
        problems = _verify_replay(title)
        if not problems:
            print("replay: PASS -- text, click, toggle and radio all landed")
        return problems
    finally:
        stop_probe(process)


def _capture(recorder: Recorder, title: str) -> tuple[Recording, bool]:
    """Run the recorder while the probe window is driven.

    Returns the recording and whether the *watchdog* ended the run. The run
    is meant to end on the stop chord the sender taps last; the watchdog is
    what ends it when the recorder does not see the chord -- and that is a
    capture failure in its own right, not a convenience. A backend that
    caught the clicks and missed both Escapes would otherwise render,
    validate and replay a clean-looking script, and the check would answer
    OK about a recorder that cannot be stopped from the keyboard. So the
    expiry is handed back for `check` to report as a problem.
    """
    sender_error: BaseException | None = None

    def run_sender() -> None:
        nonlocal sender_error
        try:
            drive_probe_window(title)
        except BaseException as exc:  # noqa: BLE001 - reported below, not swallowed
            sender_error = exc

    watchdog_fired = threading.Event()

    def give_up() -> None:
        watchdog_fired.set()
        recorder.stop()

    sender = threading.Thread(target=run_sender, daemon=True)
    watchdog = threading.Timer(90.0, give_up)
    sender.start()
    watchdog.start()
    try:
        recording = recorder.run()
    finally:
        watchdog.cancel()
        recorder.stop()
        sender.join(timeout=5)

    if sender_error is not None:
        where = "".join(traceback.format_exception(sender_error)).rstrip()
        raise Failure(f"driving the probe window failed -- {where}")
    return recording, watchdog_fired.is_set()


def check(*, do_replay: bool) -> int:
    """Capture, render, and (optionally) replay. Returns the exit status."""
    reason = unavailable_reason()
    if reason is not None:
        raise Failure(reason)

    title = f"pyguitest-recorder-check-{int(time.time())}"
    # Everything that can be built without the window is built before the
    # window exists, so the only thing left to fail with a probe on the
    # desktop is `recorder.start()`, and that is guarded.
    settings = Settings(
        backend="macos",
        stop_key="Escape",
        stop_key_presses=2,
        stop_key_interval=2.0,
        record_raw=True,
    )
    recorder = Recorder(settings=settings)
    process = start_probe(title)
    try:
        recorder.start()
        print(f"capturing macOS input while driving {title!r}")
        print("resolver:", type(recorder._resolver).__name__)
        for note in recorder.recording.environment.notes:
            print("    note:", note)
        recording, timed_out = _capture(recorder, title)
    finally:
        stop_probe(process)

    problems: list[str] = []
    if timed_out:
        print("stop chord: FAILED, the watchdog stopped the recorder after 90 s")
        problems.append(
            "the recorder did not stop on the Escape-Escape chord the sender "
            "tapped; the watchdog stopped it"
        )
    else:
        print("stop chord: the recorder stopped on the two Escapes")

    report(recording)
    recording.events = infer_synchronization(recording.events)
    added = [e for e in recording.events if e.origin is Origin.INFERRED]
    print(f"synchronization added: {len(added)}")

    source = generate(recording)
    print("\n" + source)
    invalid = validate(source)
    print("validate:", invalid or "clean")
    problems += invalid
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
        return 1
    print("\nOK")
    return 0


def main() -> int:
    """Parse flags and run the capture check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-replay",
        dest="replay",
        action="store_false",
        help="record and generate, but do not play the script back",
    )
    args = parser.parse_args()
    try:
        return check(do_replay=args.replay)
    except Failure as failure:
        print(f"\nCANNOT RUN: {failure}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
