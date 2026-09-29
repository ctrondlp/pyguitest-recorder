#!/usr/bin/env python3
"""Live Windows pass over the `Settings` fields `win32-live-capture-check.py` skips.

That script exercises exactly one `Settings` combination (the defaults, plus
`stop_key`/`stop_key_presses`/`stop_key_interval`/`record_raw`) against a real
win32 hook and a real UI Automation session. This is the rest of the settings
inventory that AGENTS.md's "platform-specific change" rule actually asks for a
live check on -- `check_key`'s real keysym delivery, and `element_context`/
`window_context` turned off against the resolver's real, platform-specific
corroboration rules -- plus one pass of the purely-rendering `Settings` (which
need no second live recording, since `generate()` is a pure function of a
`Recording`: one real capture, several renders).

Reuses `win32-live-capture-check.py`'s probe-window plumbing rather than
duplicating it, so the two scripts cannot silently drift apart.

Moves the real mouse and steals focus briefly, same as its sibling -- see that
script's own docstring for what that means and why it cannot run headless.

    scripts/win32-live-settings-check.py
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

if sys.platform != "win32":
    sys.exit("win32-live-settings-check.py only runs on Windows")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pyguitest  # noqa: E402
from pyguitest import Role  # noqa: E402

from pyguitest_recorder.backends.win32 import unavailable_reason  # noqa: E402
from pyguitest_recorder.config import Settings  # noqa: E402
from pyguitest_recorder.generator import GeneratorOptions, generate  # noqa: E402
from pyguitest_recorder.model import Recording  # noqa: E402
from pyguitest_recorder.recorder import Recorder  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "win32_live_capture_check",
    Path(__file__).resolve().with_name("win32-live-capture-check.py"),
)
assert _spec and _spec.loader
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)


class Failure(Exception):
    """One settings combination did not behave as this check expects."""


def _run(settings: Settings, title: str, driver: Callable[[str], None]) -> Recording:
    """Record one session with `settings` while `driver(gui, title)` acts on it.

    The shape of `check()` in the sibling script, generalised over the
    driver and the settings so each settings combination gets its own probe
    window and its own hook, the way a real recording would.
    """
    reason = unavailable_reason()
    if reason is not None:
        raise Failure(reason)
    process = base.start_probe(title)
    recorder = Recorder(settings=settings)
    try:
        recorder.start()
    except BaseException:
        base.stop_probe(process, title)
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
        base.stop_probe(process, title)
    if sender_error is not None:
        raise Failure(f"driving the probe window failed: {sender_error!r}")
    return recording


def _stop(gui: pyguitest.Session) -> None:
    gui.tap_key("Escape")
    time.sleep(0.3)
    gui.tap_key("Escape")
    time.sleep(0.5)


def check_check_key(title: str) -> list[str]:
    """`check_key` (default `ctrl+1`) must arrive as a real chord, not text.

    Never exercised live before: `win32-live-capture-check.py`'s own driver
    presses the stop chord but not the check key. AGENTS.md's own history
    (a laptop's bare F1 arriving as `XF86_AudioMute`) is exactly the class of
    bug a mocked keyboard cannot produce -- this is that class of bug for
    `ctrl+1` specifically, on this machine's own keyboard.
    """

    def driver(win_title: str) -> None:
        gui = pyguitest.connect(key_delay=0.03)
        base._focus_window(gui, win_title)
        base._click_element(gui, win_title, Role.ENTRY, None)
        gui.type_text("hello")
        time.sleep(0.3)
        gui.send_keys("^(1)")  # ctrl+1, the default check_key
        time.sleep(0.3)
        _stop(gui)

    recording = _run(Settings(backend="win32"), title, driver)
    source = generate(recording)
    problems = []
    if "expect_" not in source:
        problems.append(f"ctrl+1 did not render as a check -- source:\n{source}")
    return problems


def check_element_context_off(title: str) -> list[str]:
    """`element_context=False` must leave a reason, and coordinate-only clicks.

    Closes the gap this session's own investigation found: the note added to
    `recorder.py`'s `_open_session` for this combination has a mocked test,
    but nothing had run it against a real UI Automation session that could
    otherwise have named every one of these clicks.
    """

    def driver(win_title: str) -> None:
        gui = pyguitest.connect(key_delay=0.03)
        base._focus_window(gui, win_title)
        base._click_element(gui, win_title, Role.PUSH_BUTTON, "Click Me")
        _stop(gui)

    recording = _run(Settings(backend="win32", element_context=False), title, driver)
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
    """`window_context=False` must leave every click windowless, on a real desktop."""

    def driver(win_title: str) -> None:
        gui = pyguitest.connect(key_delay=0.03)
        base._focus_window(gui, win_title)
        base._click_element(gui, win_title, Role.PUSH_BUTTON, "Click Me")
        _stop(gui)

    recording = _run(Settings(backend="win32", window_context=False), title, driver)
    problems = []
    targets = [getattr(e, "target", None) for e in recording.events]
    if any(t is not None and t.window is not None for t in targets):
        problems.append("a click carried a window even though window_context=False")
    return problems


def check_generator_settings_on_a_real_recording(title: str) -> list[str]:
    """One real capture, rendered several ways -- `generate()` needs no hook.

    Every setting below is pure rendering: AGENTS.md's live-check rule is
    about capture backends and the resolver, neither of which any of these
    touch, and each already has thorough mocked coverage (see
    `tests/test_generator.py`). What is worth the one extra render here is
    confirming each still does its job against a shape only a real Windows
    UI Automation session produces -- not re-deriving what the mocks already
    prove.
    """

    def driver(win_title: str) -> None:
        gui = pyguitest.connect(key_delay=0.03)
        base._focus_window(gui, win_title)
        base._click_element(gui, win_title, Role.ENTRY, None)
        gui.type_text("Ada")
        base._click_element(gui, win_title, Role.PUSH_BUTTON, "Click Me")
        _stop(gui)

    recording = _run(Settings(backend="win32"), title, driver)
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
