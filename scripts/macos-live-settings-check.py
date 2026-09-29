#!/usr/bin/env python3
"""Live macOS pass over the `Settings` fields a mocked test suite cannot see.

The macOS counterpart of `win32-live-settings-check.py`. That script has no
sibling `macos-live-capture-check.py` to reuse plumbing from -- macOS is new
ground for a live check in this repo -- so this one carries its own probe
process management and its own driving helpers, structured the same way
`win32-live-capture-check.py`'s `start_probe`/`stop_probe`/`drive_probe_window`
are, rather than duplicating a file that does not exist yet.

Four checks, the same four AGENTS.md's "platform-specific change" rule asks a
live run to cover: `check_key`'s real keysym delivery, `element_context`/
`window_context` turned off against the resolver's real, platform-specific
corroboration rules, and one pass of the purely-rendering `Settings` (which
need no second live recording, since `generate()` is a pure function of a
`Recording`: one real capture, several renders).

Uses `Settings(backend="macos")` for capture -- `Recorder` composes the
window and element halves itself (see `recorder.py`'s `_choose_macos` and
`_context_backends`; macOS names one backend for both, where Windows and
Linux name a pair) -- and drives the probe window through
`pyguitest.connect(backend=["macquartz", "macos"])`, the band ordering this
package's own validation notes call out: a bare `connect()` answers with the
read-only path, and `macquartz` (input injection) has to be named explicitly
to reach it.

This moves the real mouse and briefly takes focus on whatever desktop it
runs on: it opens a small window of its own, drives it with synthetic input
(`pyguitest`'s own posting, not a person typing), and closes it again. Leave
the keyboard alone while it runs.

    scripts/macos-live-settings-check.py

Requires a live, unlocked macOS session -- not a bare SSH shell with no
window server attached -- and the `macos` extra so PyObjC's Quartz can serve
both the capture tap and the driving session.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

if sys.platform != "darwin":
    sys.exit("macos-live-settings-check.py only runs on macOS")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pyguitest  # noqa: E402
import Quartz  # noqa: E402
from pyguitest import Role  # noqa: E402

from pyguitest_recorder.backends.macos import unavailable_reason  # noqa: E402
from pyguitest_recorder.config import Settings  # noqa: E402
from pyguitest_recorder.generator import GeneratorOptions, generate  # noqa: E402
from pyguitest_recorder.model import Recording  # noqa: E402
from pyguitest_recorder.recorder import Recorder  # noqa: E402

PROBE_SCRIPT = Path(__file__).resolve().with_name("macos_probe_window.py")

# `macquartz` is `opt_in` -- a bare `connect()` never reaches it -- so every
# session this check opens names both backends explicitly, the same
# incantation this package's own live `Recorder` run against a real desktop
# proved out (see `docs/validation.md` in the `pyguitest` checkout).
_BACKENDS = ["macquartz", "macos"]


class Failure(Exception):
    """One settings combination did not behave as this check expects."""


def _connect(**kwargs: object) -> pyguitest.Session:
    """Open a session through the backend pair a Mac needs for input and reads."""
    return pyguitest.connect(backend=_BACKENDS, **kwargs)


def _window_exists(title: str) -> bool:
    """Whether an on-screen window titled exactly `title` exists right now.

    Asked of the window server directly (`CGWindowListCopyWindowInfo`), not
    through a pyguitest session: this needs no Accessibility grant at all,
    only Screen Recording's own, cheaper relative -- window *names* are
    available from the compositor without it -- which keeps this check as
    fast to poll as the Win32 script's raw `FindWindowW` is, and for the same
    reason: asking a live Accessibility session about a window that is still
    being created is what `win32-live-capture-check.py`'s own
    `_wait_until_responsive` exists to avoid.
    """
    info = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    )
    return any(w.get("kCGWindowName") == title for w in info)


def start_probe(title: str) -> subprocess.Popen[bytes]:
    """Launch the probe window, and confirm it actually appeared."""
    if _window_exists(title):
        raise Failure(f"a window titled {title!r} already exists -- clean up first")
    process = subprocess.Popen(
        [sys.executable, str(PROBE_SCRIPT), "--title", title],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if _window_exists(title):
            time.sleep(0.5)  # let the run loop finish laying out its controls
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

    Unlike a Win32 window, there is no separate handle for a stray window to
    linger under: the window belongs to the process's own `NSApplication`, so
    ending the process (the default `SIGTERM` disposition Python leaves in
    place) is the whole of tearing it down. `_title` stays in the signature
    to keep the call sites symmetric with the other platforms' scripts.
    """
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _focus_window(gui: pyguitest.Session, title: str) -> None:
    """Bring the probe window forward, then park the pointer in its body.

    See `win32-live-capture-check.py`'s own `_focus_window` for why both
    halves matter: a click aimed at a stale position can land on whatever the
    window manager put in front instead.
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

    Mirrors `win32-live-capture-check.py`'s own `_element` -- a lookup racing
    the window's own layout is a timing question, not a resolution bug, on
    either platform.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return gui.element(role=role, name=name, within=gui.window_element(title))
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


def _run(settings: Settings, title: str, driver: Callable[[str], None]) -> Recording:
    """Record one session with `settings` while `driver(title)` acts on it.

    The shape of `win32-live-capture-check.py`'s `check()`, generalised over
    the driver and the settings so each settings combination gets its own
    probe window and its own tap, the way a real recording would.
    """
    reason = unavailable_reason()
    if reason is not None:
        raise Failure(reason)
    process = start_probe(title)
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


def check_check_key(title: str) -> list[str]:
    """`check_key` (default `ctrl+1`) must arrive as a real chord, not text.

    The class of bug AGENTS.md's own history names (a laptop's bare F1
    arriving as `XF86_AudioMute`) is a mocked keyboard's blind spot by
    construction -- this is that class of bug for `ctrl+1` on a real Mac
    keyboard, through a real tap.
    """

    def driver(win_title: str) -> None:
        gui = _connect(key_delay=0.03)
        _focus_window(gui, win_title)
        _click_element(gui, win_title, Role.ENTRY, None)
        gui.type_text("hello")
        time.sleep(0.3)
        gui.send_keys("^(1)")  # ctrl+1, the default check_key
        time.sleep(0.3)
        _stop(gui)

    recording = _run(Settings(backend="macos"), title, driver)
    source = generate(recording)
    problems = []
    if "expect_" not in source:
        problems.append(f"ctrl+1 did not render as a check -- source:\n{source}")
    return problems


def check_element_context_off(title: str) -> list[str]:
    """`element_context=False` must leave a reason, and coordinate-only clicks.

    Closes the same gap `win32-live-settings-check.py` closed for Windows:
    the note `recorder.py`'s `_open_session` adds for this combination has a
    mocked test, but nothing had run it against a real Accessibility session
    that could otherwise have named the click.
    """

    def driver(win_title: str) -> None:
        gui = _connect(key_delay=0.03)
        _focus_window(gui, win_title)
        _click_element(gui, win_title, Role.PUSH_BUTTON, "Click Me")
        _stop(gui)

    recording = _run(Settings(backend="macos", element_context=False), title, driver)
    problems = []
    if not any("element context off" in n for n in recording.environment.notes):
        problems.append(
            f"no 'element context off' note; notes were: {recording.environment.notes}"
        )
    source = generate(recording)
    if 'name="Click Me"' in source:
        problems.append(
            f"a named element reached the script despite element_context=False:"
            f"\n{source}"
        )
    return problems


def check_window_context_off(title: str) -> list[str]:
    """`window_context=False` must leave every click windowless, on a real desktop.

    **macOS names one backend for both halves** (see `recorder.py`'s
    `_context_backends`), unlike Windows and Linux, which name a pair and can
    drop the window half from the composed session outright. Here the same
    `macos` session still answers window questions -- its docstring says the
    window half is "ungated" by the Accessibility grant -- so this check asks
    the real question rather than assuming the answer travels the same way
    across platforms: does `window_context=False` actually strip the window
    off an event here, or does the single composed backend hand one back
    regardless of the setting?
    """

    def driver(win_title: str) -> None:
        gui = _connect(key_delay=0.03)
        _focus_window(gui, win_title)
        _click_element(gui, win_title, Role.PUSH_BUTTON, "Click Me")
        _stop(gui)

    recording = _run(Settings(backend="macos", window_context=False), title, driver)
    problems = []
    targets = [getattr(e, "target", None) for e in recording.events]
    if any(t is not None and t.window is not None for t in targets):
        problems.append("a click carried a window even though window_context=False")
    return problems


def check_generator_settings_on_a_real_recording(title: str) -> list[str]:
    """One real capture, rendered several ways -- `generate()` needs no tap.

    Every setting below is pure rendering: AGENTS.md's live-check rule is
    about capture backends and the resolver, neither of which any of these
    touch, and each already has thorough mocked coverage (see
    `tests/test_generator.py`). What is worth the one extra render here is
    confirming each still does its job against a shape only a real macOS
    Accessibility session produces -- not re-deriving what the mocks already
    prove.
    """

    def driver(win_title: str) -> None:
        gui = _connect(key_delay=0.03)
        _focus_window(gui, win_title)
        _click_element(gui, win_title, Role.ENTRY, None)
        gui.type_text("Ada")
        _click_element(gui, win_title, Role.PUSH_BUTTON, "Click Me")
        _stop(gui)

    recording = _run(Settings(backend="macos"), title, driver)
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


def main() -> int:
    """Run every check, print a verdict for each, and summarize."""
    checks = (
        ("check_key", check_check_key),
        ("element_context=False", check_element_context_off),
        ("window_context=False", check_window_context_off),
        (
            "generator settings, one real recording",
            check_generator_settings_on_a_real_recording,
        ),
    )
    failed = False
    for name, fn in checks:
        tag = name.split("=")[0].split(" ")[0]
        title = f"pyguitest-recorder-settings-check-{tag}-{int(time.time())}"
        print(f"\n== {name} ==", flush=True)
        try:
            problems = fn(title)
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
