#!/usr/bin/env python3
"""Live Wayland-desktop pass over the `Settings` fields a mocked suite cannot see.

The Linux counterpart of `win32-live-settings-check.py` and
`macos-live-settings-check.py`. Like the macOS script, this one has no
sibling `linux-live-capture-check.py` to reuse plumbing from, and for a
related reason: `live-capture-check.py` is explicit that it runs only
against a **private, throwaway X server**, never the desktop you are sitting
in front of, and that a rootless XWayland is "not a substitute" for that
private server -- XTEST cannot inject into it, so there is nothing for it to
record. This package's own `backends/` directory (`x11.py`, `win32.py`,
`macos.py`) has no native-Wayland capture path at all, so on Linux a
recording is always XRecord against an X11 or XWayland display, never a
Wayland compositor's own surfaces -- see `docs/developers/architecture.md`.

What none of that exercises is a real Wayland desktop's *own* XWayland
server: a real window manager and compositor resolving windows (not a bare
Xvfb with none at all), a real, already-running accessibility bus (not one
this check has to start itself), and XRecord hooking the session's real
display rather than an isolated one it owns outright. This script is that
pass -- launching an ordinary X11 (XWayland) client on the session's own
display and recording it there, the way a user actually running this
recorder on a Wayland desktop would.

Five checks: the same four AGENTS.md's "platform-specific change" rule asks
every live run for -- `check_key`'s real keysym delivery, `element_context`/
`window_context` turned off against the resolver's real, platform-specific
corroboration rules (the `window_context` one specifically re-verifies
`DesktopResolver.resolve_windows`: before that field existed, `_window()`
looked up the window under a point unconditionally, so turning
`window_context` off silently did nothing as long as `element_context`
stayed on -- found live on macOS, fixed by gating `_window()` on
`resolve_windows`, and never previously checked live on this platform), and
one pass of the purely-rendering `Settings` (`generate()` is a pure function
of a `Recording`, so this needs no second live capture) -- plus a fifth,
specific to this platform: that a recording made this way actually carries
the "recorded through XWayland" note, confirming the recorder identifies a
real Wayland session's XWayland client by asking the display itself
(`recorder.py`'s `_is_xwayland_display`/`describe_environment`), not only the
private-Xvfb heuristic path `live-capture-check.py` already exercises.

Uses `Settings(backend="xrecord", display=...)` for capture, matching the
resolver's own non-Windows, non-macOS context pair (`x11` window half +
`atspi` element half -- see `recorder.py`'s `_context_backends`), and drives
the probe window through `pyguitest.connect(backend=["x11", "atspi"])`: the
same pair, plus X11's XTest for the actual input. XTest is named explicitly
rather than left to this platform's default composite (which prefers
`uinput`) because `uinput` needs `/dev/uinput` access this check should not
have to assume, while XTest needs no permission group and no daemon. Naming
it here is not the trap `choose_backend`'s own docstring warns about for
`xrecord` on Windows or macOS (recording a phantom desktop): the probe
window is deliberately launched as an X11 client on *this session's own*
real display, so driving it through that same display's XTest is driving
the real window on the real desktop, not a server nobody is looking at.

This moves the real mouse and briefly takes focus on whatever desktop it
runs on: it opens a small window of its own (`gtk_probe_window.py`), drives
it with synthetic input, and closes it again. Leave the keyboard alone while
it runs.

    scripts/linux-live-settings-check.py [--display :0]

Requires a live Wayland session with its own XWayland server reachable at
`--display` (default `$DISPLAY`), an accessibility bus already up (GNOME
needs `toolkit-accessibility` on: `gsettings set
org.gnome.desktop.interface toolkit-accessibility true` if element queries
come back empty with everything else present), and the `x11` and `atspi`
extras (`pip install -e '.[x11,atspi]'` alongside this package, and
pyguitest's own matching extras).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

if sys.platform not in ("linux", "linux2"):
    sys.exit("linux-live-settings-check.py only runs on Linux")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pyguitest  # noqa: E402
from pyguitest import Role  # noqa: E402

from pyguitest_recorder.backends.x11 import unavailable_reason  # noqa: E402
from pyguitest_recorder.config import Settings  # noqa: E402
from pyguitest_recorder.generator import GeneratorOptions, generate  # noqa: E402
from pyguitest_recorder.model import Recording  # noqa: E402
from pyguitest_recorder.recorder import Recorder  # noqa: E402

PROBE_SCRIPT = Path(__file__).resolve().with_name("gtk_probe_window.py")

# Named explicitly, mirroring why macos-live-settings-check.py names
# ["macquartz", "macos"]: a bare connect() reaches this platform's default
# composite instead (gnomeshell + atspi + uinput + ...), and driving through
# it would mean assuming /dev/uinput access this check has no business
# assuming. x11 (XTest) needs neither a permission group nor a daemon, and is
# the right choice on its own terms too -- see the module docstring.
_BACKENDS = ["x11", "atspi"]


class Failure(Exception):
    """One settings combination did not behave as this check expects."""


def _connect(**kwargs: object) -> pyguitest.Session:
    """Open a session through the backend pair this check drives input with."""
    return pyguitest.connect(backend=_BACKENDS, **kwargs)


def _probe_environment(display: str) -> dict[str, str]:
    """The environment the probe window is launched in: a real XWayland client.

    GDK_BACKEND=x11 and dropping WAYLAND_DISPLAY are both load-bearing, for
    the reason `live-capture-check.py`'s own `app_environment` gives: a GTK
    client with a Wayland socket in its environment opens its window on the
    compositor directly rather than through XWayland, which is silent and
    would leave nothing for XRecord to see -- defeating the entire point of
    this check. This only ever touches the probe subprocess's own
    environment; the session's ambient `WAYLAND_DISPLAY` is never touched.
    """
    environment = {**os.environ, "DISPLAY": display, "GDK_BACKEND": "x11"}
    environment.pop("WAYLAND_DISPLAY", None)
    return environment


def _window_exists(display: str, title: str) -> bool:
    """Whether an X window named exactly `title` exists on `display` right now.

    Asked of the X server directly (Xlib), not through a pyguitest session --
    the same reason `win32-live-capture-check.py` and `live-capture-check.py`
    both ask their own window systems directly rather than a live
    accessibility session about a window that may still be mapping.
    """
    from Xlib import display as xdisplay

    connection = xdisplay.Display(display)
    try:
        for child in connection.screen().root.query_tree().children:
            try:
                if child.get_wm_name() == title:
                    return True
            except Exception:  # noqa: BLE001 - windows come and go
                continue
        return False
    finally:
        connection.close()


def start_probe(display: str, title: str) -> subprocess.Popen[bytes]:
    """Launch the probe window as an XWayland client, and confirm it appeared."""
    if _window_exists(display, title):
        raise Failure(f"a window titled {title!r} already exists -- clean up first")
    process = subprocess.Popen(
        [sys.executable, str(PROBE_SCRIPT), "--title", title],
        env=_probe_environment(display),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if _window_exists(display, title):
            time.sleep(0.5)  # let the window manager finish placing it
            return process
        if process.poll() is not None:
            raise Failure(
                f"probe window process exited immediately with {process.returncode}"
            )
        time.sleep(0.1)
    process.terminate()
    raise Failure(f"probe window {title!r} never appeared")


def stop_probe(process: subprocess.Popen[bytes], _title: str) -> None:
    """Terminate the probe process. Killing it takes its window down too.

    `gtk_probe_window.py` quits on its own `destroy` signal, but there is no
    handle to a lingering window independent of the process that owns it, so
    ending the process (the default `SIGTERM` disposition Python leaves in
    place) is the whole of tearing it down -- the same as the macOS probe.
    `_title` stays in the signature to keep the call sites symmetric with the
    other platforms' scripts.
    """
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _focus_window(gui: pyguitest.Session, title: str) -> None:
    """Bring the probe window forward, then park the pointer in its body.

    A click aimed at a stale position can land on whatever the window
    manager put in front instead -- see the Windows/macOS scripts' own
    `_focus_window` for the same reasoning.
    """
    window = gui.wait_for_window(title, timeout=10)
    if window is None:
        raise Failure(f"{title!r} is not there to click in")
    gui.focus_window(window)
    x, y, width, height = gui.geometry(window)
    gui.move_mouse(x + width // 2, y + height // 2)
    time.sleep(0.5)


def _element(
    gui: pyguitest.Session,
    title: str,
    *,
    role: str,
    name: object = None,
    timeout: float = 10.0,
) -> pyguitest.Element:
    """Find an element, retrying: a miss here is often a moment, not an absence.

    Scoped by the window's *pid*, gotten from `find_window` (which
    corroborates window and app-process across the composed x11+atspi
    session), rather than through `gui.window_element(title)`. A real
    finding from this check: on this GNOME/Mutter desktop, an X11 client
    with no client-side decoration gets a second, shell-owned AT-SPI
    application (`mutter-x11-frames`) publishing its own `frame` with the
    *same name* as the real window -- a decoration proxy with panels and a
    `Close` button, not the application's own widget tree. `window_element`
    matches by name alone across every AT-SPI application on the desktop
    (see `Session.window_element` in pyguitest), so on this platform it can
    silently return that decoration frame instead of the real one, and
    every search scoped `within=` it then finds nothing -- which is exactly
    what happened here before this was changed to scope by pid instead.
    This is a pyguitest finding, not a pyguitest-recorder one; see this
    script's live-run report for the detail.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            window = gui.find_window(title)
            if window is None:
                raise Failure(f"{title!r} is not there to click in")
            return gui.element(
                role=role,
                name=name,
                predicate=lambda e, pid=window.pid: getattr(e, "pid", None) == pid,
            )
        except Exception:  # noqa: BLE001 - a missed lookup, not a failure yet
            if time.monotonic() > deadline:
                raise Failure(f"no {role!r} {name!r} after {timeout:g}s") from None
            time.sleep(0.2)


def _click_at(gui: pyguitest.Session, element: pyguitest.Element) -> None:
    """Move to the centre of `element`, click, and let the window settle."""
    x, y, w, h = gui.extents(element)
    gui.move_mouse(x + w // 2, y + h // 2)
    time.sleep(0.25)
    gui.click()
    time.sleep(0.5)


def _click_element(
    gui: pyguitest.Session, title: str, role: str, name: str | None
) -> None:
    """Click whatever element on `title` matches, by its centre."""
    _click_at(gui, _element(gui, title, role=role, name=name))


def _stop(gui: pyguitest.Session) -> None:
    """The stop chord: two Escapes, same default as every other platform."""
    gui.tap_key("Escape")
    time.sleep(0.3)
    gui.tap_key("Escape")
    time.sleep(0.5)


def _run(
    display: str, settings: Settings, title: str, driver: Callable[[str], None]
) -> Recording:
    """Record one session with `settings` while `driver(title)` acts on it.

    The shape of the Windows/macOS scripts' own `_run`, generalised over the
    driver and the settings so each settings combination gets its own probe
    window and its own capture, the way a real recording would.
    """
    reason = unavailable_reason()
    if reason is not None:
        raise Failure(reason)
    process = start_probe(display, title)
    recorder = Recorder(settings=settings)
    try:
        recorder.start()
    except BaseException:
        stop_probe(process, title)
        raise
    sender_error: BaseException | None = None

    def run_sender() -> None:
        nonlocal sender_error
        try:
            driver(title)
        except BaseException as exc:  # noqa: BLE001 - reported below
            sender_error = exc

    sender = threading.Thread(target=run_sender, daemon=True)
    watchdog = threading.Timer(60.0, recorder.stop)
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
        raise Failure(f"driving the probe window failed: {sender_error!r}")
    return recording


def check_check_key(display: str, title: str) -> list[str]:
    """`check_key` (default `ctrl+1`) must arrive as a real chord, not text.

    The class of bug AGENTS.md's own history names (a laptop's bare F1
    arriving as `XF86_AudioMute`) is a mocked keyboard's blind spot by
    construction -- this is that class of bug for `ctrl+1`, through XRecord,
    on this session's own real XWayland server.
    """

    def driver(win_title: str) -> None:
        gui = _connect(key_delay=0.03)
        _focus_window(gui, win_title)
        # Role.TEXT, not Role.ENTRY: GTK3's plain Gtk.Entry publishes AT-SPI
        # role "text" (ATSPI_ROLE_TEXT) here, confirmed against the probe's
        # own accessible tree on this at-spi2 -- ENTRY names a different
        # role this widget never reports. See Role.TEXT_ROLES, which groups
        # both under "things you type into" for exactly this reason.
        _click_element(gui, win_title, Role.TEXT, "Name")
        gui.type_text("hello")
        time.sleep(0.3)
        gui.send_keys("^(1)")  # ctrl+1, the default check_key
        time.sleep(0.3)
        _stop(gui)

    settings = Settings(backend="xrecord", display=display)
    recording = _run(display, settings, title, driver)
    source = generate(recording)
    problems = []
    if "expect_" not in source:
        problems.append(f"ctrl+1 did not render as a check -- source:\n{source}")
    return problems


def check_element_context_off(display: str, title: str) -> list[str]:
    """`element_context=False` must leave a reason, and coordinate-only clicks.

    Closes the same gap the Windows and macOS scripts closed on their
    platforms: the note `recorder.py`'s `_open_session` adds for this
    combination has a mocked test, but nothing had run it against a real,
    already-running AT-SPI bus that could otherwise have named the click.
    """

    def driver(win_title: str) -> None:
        gui = _connect(key_delay=0.03)
        _focus_window(gui, win_title)
        _click_element(gui, win_title, Role.PUSH_BUTTON, "Save")
        _stop(gui)

    recording = _run(
        display,
        Settings(backend="xrecord", display=display, element_context=False),
        title,
        driver,
    )
    problems = []
    if not any("element context off" in n for n in recording.environment.notes):
        problems.append(
            f"no 'element context off' note; notes were: {recording.environment.notes}"
        )
    source = generate(recording)
    if 'name="Save"' in source:
        problems.append(
            f"a named element reached the script despite element_context=False:"
            f"\n{source}"
        )
    return problems


def check_window_context_off(display: str, title: str) -> list[str]:
    """`window_context=False` must leave every click windowless, on a real desktop.

    This is the live re-verification of `DesktopResolver.resolve_windows`:
    before that field existed, `_window()` looked up the window under a point
    unconditionally, ignoring `window_context` outright whenever a session
    still answered window queries. The bug was silent because
    `element_context` stayed on by default -- found live on macOS, and never
    previously checked live on this platform. A live AT-SPI session here
    answers window queries just as readily as macOS's Accessibility session
    did, so this asks the same real question rather than assuming the fix
    travels unchanged: does `window_context=False` actually strip the window
    off an event, on a real X11 window list resolved through a real window
    manager?
    """

    def driver(win_title: str) -> None:
        gui = _connect(key_delay=0.03)
        _focus_window(gui, win_title)
        _click_element(gui, win_title, Role.PUSH_BUTTON, "Save")
        _stop(gui)

    recording = _run(
        display,
        Settings(backend="xrecord", display=display, window_context=False),
        title,
        driver,
    )
    problems = []
    targets = [getattr(e, "target", None) for e in recording.events]
    if any(t is not None and t.window is not None for t in targets):
        problems.append("a click carried a window even though window_context=False")
    return problems


def check_generator_settings_on_a_real_recording(display: str, title: str) -> list[str]:
    """One real capture, rendered several ways -- `generate()` needs no hook.

    Every setting below is pure rendering: AGENTS.md's live-check rule is
    about capture backends and the resolver, neither of which any of these
    touch, and each already has thorough mocked coverage (see
    `tests/test_generator.py`). What is worth the one extra render here is
    confirming each still does its job against a shape only a real XRecord
    capture, resolved through a real window manager and AT-SPI bus, produces
    -- not re-deriving what the mocks already prove.
    """

    def driver(win_title: str) -> None:
        gui = _connect(key_delay=0.03)
        _focus_window(gui, win_title)
        _click_element(gui, win_title, Role.TEXT, "Name")  # see check_check_key
        gui.type_text("Ada")
        _click_element(gui, win_title, Role.PUSH_BUTTON, "Save")
        _stop(gui)

    settings = Settings(backend="xrecord", display=display)
    recording = _run(display, settings, title, driver)
    problems = []

    absolute = generate(recording, GeneratorOptions(locators="absolute"))
    if "name=" in absolute:
        problems.append("locators='absolute' still named an element")

    no_comments = generate(recording, GeneratorOptions(comments=False))
    element = generate(recording)  # locators='element', comments=True (defaults)
    if len(no_comments.splitlines()) >= len(element.splitlines()):
        problems.append("comments=False did not shorten the script")

    no_header = generate(recording, GeneratorOptions(include_header=False))
    if no_header.lstrip().startswith('"""'):
        problems.append("include_header=False still wrote a docstring header")

    named = generate(recording, GeneratorOptions(function_name="run_scenario"))
    if "def run_scenario(" not in named or "def main(" in named:
        problems.append("function_name was not honored")

    return problems


def check_xwayland_note(display: str, title: str) -> list[str]:
    """A recording made on this session's own display must say so.

    Specific to this platform: `recorder.py`'s `describe_environment` asks
    the display itself whether it is XWayland (`_is_xwayland_display`,
    over the `XWAYLAND` X extension) rather than trusting the environment,
    precisely so that a session's *own* XWayland is told apart from a
    private Xvfb that happens to have the same two variables set -- see that
    function's own long comment about a real Wayland desktop coming back
    `x11` with `xwayland` false before this existed, silently. Every other
    check in this file could pass against a private Xvfb too, since none of
    them looks at this note; this is the one thing here that only a real
    Wayland desktop's own XWayland server can prove.
    """

    def driver(win_title: str) -> None:
        gui = _connect(key_delay=0.03)
        _focus_window(gui, win_title)
        _click_element(gui, win_title, Role.PUSH_BUTTON, "Save")
        _stop(gui)

    settings = Settings(backend="xrecord", display=display)
    recording = _run(display, settings, title, driver)
    problems = []
    if not recording.environment.xwayland:
        problems.append(
            "environment.xwayland is False for a recording made on a real "
            "Wayland session's own XWayland server"
        )
    if not any("recorded through XWayland" in n for n in recording.environment.notes):
        problems.append(
            "no 'recorded through XWayland' note; notes were: "
            f"{recording.environment.notes}"
        )
    return problems


def main() -> int:
    """Run every check, print a verdict for each, and summarize."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--display",
        default=os.environ.get("DISPLAY", ":0"),
        help="the session's own X11/XWayland display (default: $DISPLAY or :0)",
    )
    args = parser.parse_args()

    checks = (
        ("check_key", check_check_key),
        ("element_context=False", check_element_context_off),
        ("window_context=False", check_window_context_off),
        (
            "generator settings, one real recording",
            check_generator_settings_on_a_real_recording,
        ),
        ("recorded through XWayland", check_xwayland_note),
    )
    failed = False
    for name, fn in checks:
        tag = name.split("=")[0].split(" ")[0]
        title = f"pyguitest-recorder-settings-check-{tag}-{int(time.time())}"
        print(f"\n== {name} ==", flush=True)
        try:
            problems = fn(args.display, title)
        except Failure as exc:
            print(f"FAILED to run: {exc}")
            failed = True
            continue
        if problems:
            failed = True
            for problem in problems:
                print(" -", problem)
        else:
            print("PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
