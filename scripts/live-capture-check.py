#!/usr/bin/env python3
"""Record real input from a real X server, end to end, and print what came out.

Everything in `tests/` stops at the point where bytes arrive from the wire.
This is the other half: an actual X server, an actual application, actual
input, and the whole pipeline -- capture, normalize, infer, generate, validate
-- run over it. It is what turns "written" into "known to work" for the one
backend a unit test cannot reach.

It runs against a **private, throwaway X server**, never the desktop you are
sitting in front of. XRecord sees every application's keystrokes, so pointing
this at a real session would capture whatever else you happened to type.

    scripts/live-capture-check.py              # start Xvfb, run, tear down
    scripts/live-capture-check.py --display :5 # use a server already running
    scripts/live-capture-check.py --record-motion   # the costly setting too

Xvfb is the requirement (`xorg-x11-server-Xvfb` on Fedora, `xvfb` on Debian).
A rootless XWayland is *not* a substitute: the compositor owns the pointer
there, so XTEST cannot inject and there is no input to record. That is worth
knowing rather than working around -- it is the same wall the recorder itself
hits, from the other side.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pyguitest_recorder.analyzer import infer_synchronization
from pyguitest_recorder.backends.x11 import unavailable_reason
from pyguitest_recorder.config import Settings
from pyguitest_recorder.generator import generate, validate
from pyguitest_recorder.model import Origin, Recording
from pyguitest_recorder.recorder import Recorder

GEOMETRY = "1280x800x24"
"""What the private server is asked for; only the size matters to the test."""

APP_TITLE = "Recorder Check"
"""Title the test application is given, and waited for by."""

SECOND_TITLE = "Recorder Check Two"
"""A second window, somewhere else, so switching between windows is covered."""

SECOND_AT = (600, 400)
SECOND_SIZE = (320, 200)

A11Y_DIRECTORIES = (
    "/usr/libexec",
    "/usr/lib/at-spi2-core",
    "/usr/lib",
    "/usr/local/libexec",
)
"""Where the accessibility daemons live, which is not the same on every
distribution: Fedora puts them in /usr/libexec, Debian and Ubuntu have used
/usr/lib/at-spi2-core, and FreeBSD's port (GhostBSD included) installs under
/usr/local/libexec. None is on PATH, so all are searched rather than one being
assumed -- guessing wrong here does not fail, it silently drops to
"no accessibility bus" and the element half of the check stops being tested.
The last one was found exactly that way, running this on GhostBSD."""


def a11y_daemon(name: str) -> str | None:
    """Find one accessibility daemon, wherever this distribution keeps it."""
    for directory in A11Y_DIRECTORIES:
        candidate = Path(directory) / name
        if candidate.exists():
            return str(candidate)
    return shutil.which(name)


INNER = "PYGUITEST_RECORDER_CHECK_INNER"
"""Set on the re-exec, so the private bus is only established once."""

OWN_RUNTIME = "PYGUITEST_RECORDER_CHECK_OWN_RUNTIME"
"""Carries the private XDG_RUNTIME_DIR across the re-exec, so the inner run
removes what the outer one made -- the outer process is gone by then."""


def reexec_on_a_private_bus() -> None:
    """Re-run this script on a session bus of its own, if one can be had.

    Without an accessibility bus nothing publishes elements, so the resolver's
    element half -- the reason a recording can say `gui.button("Save")` rather
    than a coordinate -- is never exercised. Starting the bus needs a session
    bus to start it on, and borrowing the desktop's would put the recorded
    application on the *desktop's* accessibility bus, which is the leak this
    check exists to keep out.
    """
    if os.environ.get(INNER) or a11y_daemon("at-spi-bus-launcher") is None:
        return
    runner = shutil.which("dbus-run-session")
    if runner is None:
        return
    # A private session bus is not enough on its own, and this is the half
    # that was missing. at-spi-bus-launcher derives its socket path from
    # XDG_RUNTIME_DIR rather than from the bus it was started on, so the one
    # started below binds $XDG_RUNTIME_DIR/at-spi/bus -- the very path the
    # developer's own session keeps its accessibility socket at -- and evicts
    # it. Silently, for every GTK3 and Qt application on that desktop, until
    # the next login; and pyguitest processes then abort outright, because
    # at-spi-bus-launcher outlives the bus it launched and keeps handing out
    # its address. Measured on 2026-09-22 with four dead sockets in that
    # directory, one per session this and pyguitest's own harness had run.
    runtime = tempfile.mkdtemp(prefix="pyguitest-recorder-runtime.")
    os.chmod(runtime, 0o700)
    os.environ["XDG_RUNTIME_DIR"] = runtime
    os.environ[OWN_RUNTIME] = runtime
    os.environ[INNER] = "1"
    try:
        os.execvp(runner, [runner, "--", sys.executable, *sys.argv])
    except OSError:
        # execvp returns only by failing. The re-exec is what hands ownership
        # of this directory to the inner run, and main()'s finally -- which
        # removes it -- belongs to that run, so a failure here is the one path
        # where nothing else will ever clean it up.
        shutil.rmtree(runtime, ignore_errors=True)
        raise


def start_a11y_bus() -> list[subprocess.Popen[bytes]]:
    """Start the accessibility bus on this session bus; empty if there is none.

    Every process started is returned, because every one of them has to be
    stopped: the registry used to be started and forgotten, and each run left an
    orphaned `at-spi2-registryd` behind on the machine it ran on.
    """
    bus = a11y_daemon("at-spi-bus-launcher")
    if bus is None or not os.environ.get(INNER):
        return []
    started = [
        subprocess.Popen(
            [bus, "--launch-immediately"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    ]
    time.sleep(2)
    # Started by hand because systemd activation is not available on a private
    # session bus. Without it the bus exists and answers nothing -- and
    # `Atspi.get_desktop(0)` still succeeds against that, which is why the
    # resolver probes by counting children rather than by asking for a desktop.
    registry = a11y_daemon("at-spi2-registryd")
    if registry is not None:
        started.append(
            subprocess.Popen(
                [registry], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        )
        time.sleep(2)
    return started


def stop(process: subprocess.Popen[bytes]) -> None:
    """Terminate a process this check started, and kill it if it will not go."""
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def app_environment(display: str) -> dict[str, str]:
    """The environment an application under test is started in.

    GDK_BACKEND and dropping WAYLAND_DISPLAY are both load-bearing: a GTK client
    with a Wayland socket in its environment opens its window on the
    developer's real compositor instead of the display under test, which is
    silent and lands a window on their screen.

    So is what this does about the accessibility bridge, and it is the same kind
    of failure. `NO_AT_BRIDGE=1` -- exported by plenty of shells, containers and
    tool runners to quiet GTK's "couldn't connect to accessibility bus"
    warning -- stops the toolkit registering with the accessibility bus at all.
    Inherited into the application under test it leaves the private bus up and
    empty, every click resolving to no element, and the check still passing:
    found running it on GhostBSD from a shell that set it. With no private bus
    the bridge is turned off instead, because without one GTK stalls looking
    for it.
    """
    environment = {**os.environ, "DISPLAY": display, "GDK_BACKEND": "x11"}
    environment.pop("WAYLAND_DISPLAY", None)
    if os.environ.get(INNER):
        environment.pop("NO_AT_BRIDGE", None)
    else:
        environment["GTK_A11Y"] = "none"
    return environment


class Failure(Exception):
    """The check could not run, or the recorder did not see what was sent."""


def start_server(number: int) -> subprocess.Popen[bytes]:
    """Start a private Xvfb, or explain what is missing."""
    xvfb = shutil.which("Xvfb")
    if xvfb is None:
        raise Failure(
            "Xvfb is not installed, and this check will not run against a real "
            "session: XRecord captures every application's keystrokes. Install "
            "xorg-x11-server-Xvfb (Fedora) or xvfb (Debian), or pass --display "
            "for a private server you started yourself."
        )
    if Path(f"/tmp/.X11-unix/X{number}").exists():
        raise Failure(f"display :{number} is already in use; pass --display")
    server = subprocess.Popen(
        [xvfb, f":{number}", "-screen", "0", GEOMETRY, "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(80):
        if Path(f"/tmp/.X11-unix/X{number}").exists():
            time.sleep(0.5)
            return server
        if server.poll() is not None:
            raise Failure(f"Xvfb exited immediately with {server.returncode}")
        time.sleep(0.25)
    server.terminate()
    raise Failure("Xvfb never created its socket")


def start_app(display: str) -> subprocess.Popen[bytes]:
    """Put something on the server that has a window and takes typing."""
    for command in (
        ["zenity", "--entry", "--title=" + APP_TITLE, "--text=Name"],
        ["xterm", "-T", APP_TITLE],
        ["xmessage", "-center", APP_TITLE],
    ):
        if shutil.which(command[0]) is None:
            continue
        app = subprocess.Popen(
            command,
            env=app_environment(display),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
        if app.poll() is None:
            return app
    raise Failure("no test application available (tried zenity, xterm, xmessage)")


MIN_APP_SIZE = 100
"""Smallest window worth aiming at, in pixels on a side."""


def app_rectangle(display: str, title: str) -> tuple[int, int, int, int]:
    """Where the test application actually is, so the clicks land on it.

    A bare X server has no window manager, so the window sits wherever the
    toolkit put it, at whatever size it asked for. Clicking a fixed point in
    the middle of the screen misses it entirely, and the recorder is then only
    ever asked about empty root -- which passes, and proves nothing.

    Waiting for the window by *name* matters as much as by size. A GTK client
    maps a 1x1 leader window at the origin before its real one, and a run that
    catches that instead clicks (0, 0), resolves the leader, and produces a
    plausible transcript of an interaction that never happened.
    """
    from Xlib import display as xdisplay

    connection = xdisplay.Display(display)
    seen: list[str] = []
    try:
        for _ in range(60):
            seen = []
            for child in connection.screen().root.query_tree().children:
                try:
                    name = child.get_wm_name()
                    box = child.get_geometry()
                except Exception:  # noqa: BLE001 - windows come and go
                    continue
                seen.append(f"{name!r} {box.width}x{box.height}")
                if name != title:
                    continue
                if box.width >= MIN_APP_SIZE and box.height >= MIN_APP_SIZE:
                    return (box.x, box.y, box.width, box.height)
            time.sleep(0.25)
    finally:
        connection.close()
    raise Failure(
        f"no window at least {MIN_APP_SIZE}px square appeared for {title!r}; "
        f"aiming at whatever else is on the display would prove nothing. "
        f"Saw: {', '.join(seen) or 'nothing'}"
    )


def start_second_window(display: str) -> subprocess.Popen[bytes] | None:
    """Open a window somewhere else, or report that we cannot.

    Needs PyGObject. Without it the check still runs and says which part of the
    scenario went uncovered, rather than passing as though it had not.
    """
    script = Path(__file__).resolve().parent / "check_app.py"
    environment = app_environment(display)
    app = subprocess.Popen(
        [
            sys.executable,
            str(script),
            "--title",
            SECOND_TITLE,
            "--at",
            str(SECOND_AT[0]),
            str(SECOND_AT[1]),
            "--size",
            str(SECOND_SIZE[0]),
            str(SECOND_SIZE[1]),
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    return app if app.poll() is None else None


def sweep_pointer(connection, xtest, motion, *, start, end, steps: int = 60) -> None:
    """Walk the pointer between two points, the way a hand crosses a window.

    The one input shape that makes recorded motion expensive, and the shape
    nothing else in this check produces: one event per *position* rather than
    one per action. With `record_motion` on that is what a recording spends its
    whole time consuming, and it is what a recorder that cannot keep up falls
    behind on. Deliberately not the `point` helper inside `send_input`, whose
    0.2s pause per step exists to make each move stand as its own hover-able
    event, and which would turn sixty positions into twelve seconds of input.
    """
    for step in range(steps):
        fraction = step / steps
        xtest.fake_input(
            connection,
            motion,
            x=int(start[0] + (end[0] - start[0]) * fraction),
            y=int(start[1] + (end[1] - start[1]) * fraction),
        )
        connection.sync()
        time.sleep(0.005)


def send_input(
    display: str,
    rectangle: tuple[int, int, int, int],
    second: tuple[int, int, int, int] | None,
) -> None:
    """Synthesize the interaction the recorder is supposed to see.

    One of each kind the normalizer groups differently: a click, a word typed,
    a hotkey, a drag, a pause worth synchronizing on, a move to another window
    and back, a right click and a wheel notch.
    """
    from Xlib import XK, X
    from Xlib import display as xdisplay
    from Xlib.ext import xtest

    connection = xdisplay.Display(display)

    def key(name: str) -> int:
        """The keycode for a keysym name, on this X server's layout."""
        return connection.keysym_to_keycode(XK.string_to_keysym(name))

    def tap(*codes: int) -> None:
        """Press each code in order and release in reverse, then pause.

        The pause is not politeness. The events this script synthesizes only
        mean anything to the recorder because they are separable in time, and
        every delay in here exists for that.
        """
        for code in codes:
            xtest.fake_input(connection, X.KeyPress, code)
        for code in reversed(codes):
            xtest.fake_input(connection, X.KeyRelease, code)
        connection.sync()
        time.sleep(0.08)

    def point(x: int, y: int) -> None:
        """Put the pointer at x, y and let the move stand as its own event."""
        xtest.fake_input(connection, X.MotionNotify, x=x, y=y)
        connection.sync()
        time.sleep(0.2)

    def click(button: int = 1) -> None:
        """Press and release one button, then pause for the recorder to see it."""
        xtest.fake_input(connection, X.ButtonPress, button)
        xtest.fake_input(connection, X.ButtonRelease, button)
        connection.sync()
        time.sleep(0.3)

    def drag(from_x: int, from_y: int, to_x: int, to_y: int) -> None:
        """Press at one point, move to the other in steps, and release.

        The intermediate points are what makes it a drag: they are what the
        recorder sees between the press and the release, where a single jump
        would be a press and a release with nothing in between.

        They bow, too. A straight interpolation is a straight route, and a
        straight route is the one case where the recorded path adds nothing --
        so a check meant to exercise the path has to be curved, or it agrees
        with any amount of dropping it. See `sweep_pointer` for the same
        reasoning about motion at all.
        """
        point(from_x, from_y)
        xtest.fake_input(connection, X.ButtonPress, 1)
        connection.sync()
        bow = 18 if to_y >= from_y else -18
        for step in range(1, 6):
            fraction = step / 5
            # An arc: furthest from the straight line halfway along, back on it
            # at both ends, so the route has a shape to record.
            arc = int(bow * 4 * fraction * (1 - fraction))
            point(
                from_x + int((to_x - from_x) * fraction),
                from_y + int((to_y - from_y) * fraction) + arc,
            )
        xtest.fake_input(connection, X.ButtonRelease, 1)
        connection.sync()
        time.sleep(0.3)

    x, y, width, height = rectangle
    point(x + width // 2, y + height // 2)
    click()
    # Sixty positions across the window, before anything else is done: cheap to
    # ignore and the only thing that costs anything to record.
    sweep_pointer(
        connection,
        xtest,
        X.MotionNotify,
        start=(x + 20, y + 20),
        end=(x + width - 20, y + height - 20),
    )
    for char in "Ada":
        tap(key(char)) if char.islower() else tap(key("Shift_L"), key(char.lower()))
    tap(key("Control_L"), key("s"))
    time.sleep(2.0)  # long enough to become a Pause, and then a wait
    if second is not None:
        sx, sy, swidth, sheight = second
        point(sx + swidth // 2, sy + sheight // 2)
        click()
        # ...and back, which is the case a recording cannot replay without an
        # explicit raise: injected input goes wherever focus already is.
        point(x + width // 2, y + height // 2)
        click()
    point(x + width // 2, y + height - 20)
    click()
    click(3)
    # The drag comes last on purpose. Dragging a blank part of a window with
    # no window manager drags the *window*, and a scenario that moves the
    # thing it is clicking on invalidates every coordinate after it.
    drag(x + 40, y + 60, x + 200, y + 60)
    xtest.fake_input(connection, X.ButtonPress, 4)
    xtest.fake_input(connection, X.ButtonRelease, 4)
    connection.sync()
    time.sleep(0.5)
    # The panic key, which is also the only way a real recording ends without
    # the terminal it was started from. Exercised here rather than described --
    # and pressed *twice*, because the default chord is two presses: a single
    # press that never finds a second is handed on and recorded as the
    # application's own keystroke, which is the behaviour that keeps "press
    # Escape to close the dialog" recordable. This used to tap it once and let
    # the watchdog end the run, so the stop key was never exercised at all.
    tap(key("Pause"))
    time.sleep(0.4)
    tap(key("Pause"))
    time.sleep(0.5)
    connection.close()


def check(display: str, record_motion: bool = False) -> int:
    """Capture, render, and report. Returns the process exit status.

    Driven through `Recorder` rather than the capture backend alone, so the
    window resolver, the environment block and the panic key are exercised too
    -- a bare `Normalizer` resolves nothing, which leaves every inference rule
    with no evidence to work from and proves much less than it appears to.

    `record_motion` turns on the one setting whose cost is the reason this
    check has a note about falling behind in it at all: every pointer position
    is resolved and emitted, rather than only where the pointer rested.
    """
    os.environ["DISPLAY"] = display
    reason = unavailable_reason()
    if reason is not None:
        raise Failure(reason)

    app = start_app(display)
    second_app = start_second_window(display)
    settings = Settings(
        display=display,
        record_raw=True,
        stop_key="Pause",
        record_motion=record_motion,
    )
    recorder = Recorder(settings=settings)
    recorder.start()
    print(f"capturing on {display}")
    resolver = recorder._resolver
    print(f"resolver:  {type(resolver).__name__}")
    for note in recorder.recording.environment.notes:
        print(f"    note: {note}")

    # Injection runs alongside `run()`, which blocks until the panic key. The
    # timer is only a safety net for a run that never sees it.
    time.sleep(0.5)
    rectangle = app_rectangle(display, APP_TITLE)
    print(f"application at: {rectangle}")
    second = None
    if second_app is None:
        print("no second window (PyGObject missing); window switching uncovered")
    else:
        try:
            second = app_rectangle(display, SECOND_TITLE)
            print(f"second window at:  {second}")
        except Failure as exc:
            print(f"second window unusable, switching uncovered: {exc}")
    sender = threading.Thread(
        target=send_input, args=(display, rectangle, second), daemon=True
    )
    watchdog = threading.Timer(30.0, recorder.stop)
    sender.start()
    watchdog.start()
    try:
        recording = recorder.run()
    finally:
        watchdog.cancel()
        recorder.stop()
        for process in (app, second_app):
            if process is not None:
                process.terminate()

    report(recording)

    recording.events = infer_synchronization(recording.events)
    print("inferred:  ", [type(e).__name__ for e in recording.events])
    added = [e for e in recording.events if e.origin is Origin.INFERRED]
    print(f"synchronization added: {len(added)}")

    source = generate(recording)
    print("\n" + source)
    problems = validate(source)
    print("validate:", problems or "clean")
    problems += round_trip(recording, source)
    return 1 if problems else 0


def report(recording: Recording) -> None:
    """Print what the capture actually saw, before anything is inferred from it.

    Kept apart from `check` because the two answer different questions. This
    one is the diagnostic a failing run is read for -- which events arrived,
    which of them resolved to a window and an element, and what the resolver
    warned about -- and it is the half that grows every time a live run
    surprises us.
    """
    print(f"\ncaptured {len(recording.raw)} raw events")
    for event in recording.raw[:40]:
        button = f"button {event['button']}" if event["button"] else ""
        name = event["keysym"] or button
        print(
            f"    {event['kind']:15} {name:12} at ({event['x']},{event['y']})"
            f"{'  text=' + repr(event['text']) if event['text'] else ''}"
        )
    if not recording.raw:
        raise Failure(
            "the server accepted the recording context but delivered no events; "
            "on a rootless XWayland this is expected, because XTEST cannot "
            "inject there -- run against Xvfb instead"
        )

    print("\nnormalized:", [type(e).__name__ for e in recording.events])
    windows = {
        t.window.title or t.window.app_id
        for t in (getattr(e, "target", None) for e in recording.events)
        if t is not None and t.window is not None
    }
    elements = {
        f"{t.element.role} {t.element.name!r}"
        for t in (getattr(e, "target", None) for e in recording.events)
        if t is not None and t.element is not None
    }
    for event in recording.events:
        target = getattr(event, "target", None)
        if target is not None:
            window = target.window
            print(
                f"    ({target.x},{target.y}) -> window "
                f"{(window.title if window else None)!r} "
                f"geometry={window.geometry if window else None}"
            )
    print("windows resolved: ", sorted(windows) or "none")
    print("elements resolved:", sorted(elements) or "none")
    for event in recording.events:
        target = getattr(event, "target", None)
        if target is not None and target.element is not None:
            window_pid = target.window.pid if target.window else None
            print(
                f"    element {target.element.role!r} pid={target.element.pid} "
                f"extents={target.element.extents} | window pid={window_pid} "
                f"geometry={target.window.geometry if target.window else None}"
            )
    print("screens:", recording.environment.screens)
    for event in recording.events:
        route = getattr(event, "route", None)
        if route:
            print(
                f"    route: {len(route)} points recorded in a {type(event).__name__}"
            )
    for note in recording.environment.notes:
        print(f"    note: {note}")


def round_trip(recording: Recording, source: str) -> list[str]:
    """Save the recording, read it back, and check it renders the same.

    The claim `--regenerate` rests on: a recording is the durable artefact and
    the script is derived from it, so a saved one has to render to the same
    thing on a later run. Only ever checked against events built in a test
    before this; here it is a real capture, with real windows, elements,
    timings and environment in it.
    """
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


def main() -> int:
    """Parse arguments, run the check, and tear down whatever it started."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--display",
        help="a private X server to use; without this one is started and stopped",
    )
    parser.add_argument("--number", type=int, default=99, help="display for Xvfb")
    parser.add_argument(
        "--record-motion",
        action="store_true",
        help="record every pointer position rather than only the rests, which is "
        "the setting whose cost this check exists to measure",
    )
    args = parser.parse_args()

    reexec_on_a_private_bus()
    server = None
    a11y: list[subprocess.Popen[bytes]] = []
    try:
        display = args.display
        if display is None:
            server = start_server(args.number)
            display = f":{args.number}"
        # Before the accessibility daemons, not after. The registry injects
        # input into whichever display it was started on, so one started here
        # with the developer's own DISPLAY still set is attached to the desktop
        # they are sitting in front of -- and one started with none can click
        # into nothing while every call still reports success, which is how a
        # replay that did nothing came to exit 0 on GhostBSD.
        os.environ["DISPLAY"] = display
        a11y = start_a11y_bus()
        print("accessibility bus:", "private" if a11y else "none (elements off)")
        return check(display, record_motion=args.record_motion)
    except Failure as exc:
        print(f"live-capture-check: {exc}", file=sys.stderr)
        return 2
    finally:
        # The bus goes before the server it was started beside, and the
        # registry before the launcher that owns the bus it is registered on.
        for process in (*reversed(a11y), server):
            if process is not None:
                stop(process)
        # After the daemons that hold sockets in it, never before.
        own_runtime = os.environ.get(OWN_RUNTIME)
        if own_runtime:
            shutil.rmtree(own_runtime, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
