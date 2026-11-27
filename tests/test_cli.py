"""The command line: the flags the parser accepts, and what they change.

Parsing is checked apart from the effects, because the two fail differently.
An unset flag that stays `None` is the mechanism by which the config file
wins, and no end-to-end test would notice the difference.
"""

import dataclasses
import os
import stat
import sys

import pytest

from pyguitest_recorder.cli import (
    _as_secret_as_asked,
    _generator_options,
    _stop_progress_message,
    _sync_options,
    _trimmed_events,
    build_parser,
    main,
)
from pyguitest_recorder.config import Settings
from pyguitest_recorder.model import (
    Assertion,
    Click,
    ElementRef,
    Recording,
    Target,
    WindowRef,
    describe_assertion,
)


@pytest.fixture
def saved(tmp_path):
    window = WindowRef(
        title="Example", app_id="org.example.App", geometry=(0, 0, 800, 600)
    )
    recording = Recording()
    recording.add(Click(timestamp=0.0, target=Target(x=10, y=20, window=window)))
    path = tmp_path / "rec.json"
    recording.save(path)
    return path


def _clicks(*coords_at):
    """A Recording of Click events, at the given [((x, y), timestamp), ...]."""
    window = WindowRef(
        title="Example", app_id="org.example.App", geometry=(0, 0, 800, 600)
    )
    recording = Recording()
    for (x, y), timestamp in coords_at:
        recording.add(
            Click(timestamp=timestamp, target=Target(x=x, y=y, window=window))
        )
    return recording


@pytest.fixture
def multi_saved(tmp_path):
    # Four clicks: one at the very start (the "fumbling" --from is meant to
    # cut), one mis-click in the middle (what --drop is meant to remove),
    # and two good ones after it.
    recording = _clicks(
        ((10, 20), 0.0),
        ((30, 40), 5.0),
        ((50, 60), 9.0),
        ((70, 80), 12.0),
    )
    path = tmp_path / "multi.json"
    recording.save(path)
    return path


def test_parser_accepts_the_documented_flags():
    args = build_parser().parse_args(
        [
            "--display",
            ":1",
            "--screen",
            "2",
            "-o",
            "out.py",
            "--record-raw",
            "--no-window-context",
            "--absolute-coordinates",
            "--debug",
        ]
    )
    assert args.display == ":1"
    assert args.screen == 2
    assert args.window_context is False
    assert args.locators == "absolute"


def test_unset_flags_stay_none_so_the_config_file_wins():
    args = build_parser().parse_args([])
    assert args.locators is None
    assert args.window_context is None
    assert args.record_raw is None
    assert args.suppress_keymap_warning is None
    assert args.suppress_atspi_chatter is None
    assert args.announce_checks is None


def test_suppress_flags_are_parsed():
    args = build_parser().parse_args(
        ["--suppress-keymap-warning", "--suppress-atspi-chatter"]
    )
    assert args.suppress_keymap_warning is True
    assert args.suppress_atspi_chatter is True


def test_no_check_feedback_flag_is_parsed():
    args = build_parser().parse_args(["--no-check-feedback"])
    assert args.announce_checks is False


def _fake_recorder(assertion):
    """A `Recorder` stand-in for `_record()` that never touches a desktop.

    `run()` fires `on_check` itself, the same way a real recorder would the
    moment a check is recorded, so a test can see whether `_record()` wired
    the callback up at all -- which is all `announce_checks` controls.
    """

    class FakeRecorder:
        unstopped_presses = 0

        def __init__(self, settings):
            self.settings = settings
            self.on_stop_progress = None
            self.on_check = None
            self.on_lag = None
            self.recording = Recording()

        def start(self):
            pass

        def run(self):
            if self.on_check is not None:
                self.on_check(assertion)
            return Recording()

        def stop(self):
            pass

    return FakeRecorder


def test_announce_checks_gates_the_live_check_feedback(tmp_path, capsys, monkeypatch):
    # Pressing check_key has no other visible effect at all (see
    # `Settings.announce_checks`'s own docstring), so this line is the only
    # way anyone finds out a check was recorded while it happens. Existing
    # coverage only checks that `--no-check-feedback` parses
    # (test_no_check_feedback_flag_is_parsed); nothing exercises `_record()`
    # itself, so a build that stopped wiring `on_check` up at all -- or wired
    # it up unconditionally -- would still pass everything else here.
    from pyguitest_recorder import cli
    from pyguitest_recorder.model import Assertion

    assertion = Assertion(check="showing", target=Target(x=0, y=0))
    monkeypatch.setattr(cli, "Recorder", _fake_recorder(assertion))

    main(["--config", str(_empty(tmp_path)), "-o", str(tmp_path / "on.py")])
    assert "Check:" in capsys.readouterr().err

    main(
        [
            "--no-check-feedback",
            "--config",
            str(_empty(tmp_path)),
            "-o",
            str(tmp_path / "off.py"),
        ]
    )
    assert "Check:" not in capsys.readouterr().err


def test_a_degraded_session_is_said_when_recording_starts_not_after(
    tmp_path, capsys, monkeypatch
):
    # "clicks will carry bare coordinates" is worth knowing before ten minutes
    # of recording, not in the summary after them.
    from pyguitest_recorder import cli

    class Degraded(_fake_recorder(None)):
        def __init__(self, settings):
            super().__init__(settings)
            self.recording.environment.notes.append(
                "window and element context off: no pyguitest session"
            )

        def run(self):
            print("RUNNING", file=sys.stderr)
            return self.recording

    monkeypatch.setattr(cli, "Recorder", Degraded)
    main(["--config", str(_empty(tmp_path)), "-o", str(tmp_path / "s.py")])
    err = capsys.readouterr().err
    note = err.index("note: window and element context off")
    assert note < err.index("Recording.")
    assert note < err.index("RUNNING")


def test_termination_signals_end_a_recording_like_ctrl_c_and_are_restored():
    import signal

    from pyguitest_recorder import cli

    names = [n for n in ("SIGTERM", "SIGHUP", "SIGBREAK") if hasattr(signal, n)]
    before = {n: signal.getsignal(getattr(signal, n)) for n in names}
    with cli._interrupt_on_termination():
        for name in names:
            handler = signal.getsignal(getattr(signal, name))
            assert handler is not before[name]
            with pytest.raises(KeyboardInterrupt):
                handler(getattr(signal, name), None)
    assert {n: signal.getsignal(getattr(signal, n)) for n in names} == before


def test_an_ignored_sigint_is_made_to_stop_the_recording_and_then_restored():
    # A background job starts with SIGINT ignored, so `kill -INT` -- the
    # documented way to stop a recording -- did nothing at all, silently.
    import signal

    from pyguitest_recorder import cli

    original = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        with cli._interrupt_on_termination():
            handler = signal.getsignal(signal.SIGINT)
            assert handler is not signal.SIG_IGN
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGINT, None)
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, original)


def test_a_sigint_that_is_not_ignored_is_left_alone():
    # Python's default already raises KeyboardInterrupt; replacing it would
    # only lose whatever a caller (a test runner, an IDE) had installed.
    import signal

    from pyguitest_recorder import cli

    sentinel = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        with cli._interrupt_on_termination():
            assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGINT, sentinel)


class _CooperativeRecorder:
    """What `_interrupt_on_termination` needs of an X11 recorder."""

    cooperative_interrupts = True

    def __init__(self):
        import threading

        self.stopped = threading.Event()
        self.abandoned = 0

    def stop(self):
        self.stopped.set()

    def abandon_tail(self):
        self.abandoned += 1


def test_where_interrupts_are_cooperative_a_signal_stops_capture_and_raises_nothing():
    # An exception delivered into the middle of a python-xlib request leaves
    # that connection spinning forever; reproduced with no recorder at all, and
    # live as a recording that would not stop 90s after SIGTERM. So on X11 the
    # handler asks for a stop from another thread, as the stop key does.
    import signal

    from pyguitest_recorder import cli

    recorder = _CooperativeRecorder()
    with cli._interrupt_on_termination(recorder):
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)  # must not raise
        assert recorder.stopped.wait(5), "stop() was never called"
        assert recorder.abandoned == 0


def test_a_second_signal_abandons_the_backlog_instead_of_raising():
    import signal

    from pyguitest_recorder import cli

    recorder = _CooperativeRecorder()
    with cli._interrupt_on_termination(recorder):
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)
        recorder.stopped.wait(5)
        handler(signal.SIGTERM, None)
        handler(getattr(signal, "SIGHUP", signal.SIGTERM), None)
        assert recorder.abandoned == 2


def test_cooperative_interrupts_take_over_sigint_even_when_it_is_not_ignored():
    # Python's own Ctrl-C is the same asynchronous exception, so it is replaced
    # too -- and put back afterwards.
    import signal

    from pyguitest_recorder import cli

    original = signal.getsignal(signal.SIGINT)
    try:
        signal.signal(signal.SIGINT, signal.default_int_handler)
        with cli._interrupt_on_termination(_CooperativeRecorder()):
            assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGINT, original)


def test_a_recorder_that_is_not_cooperative_still_gets_a_keyboard_interrupt():
    import signal

    from pyguitest_recorder import cli

    class Plain:
        cooperative_interrupts = False

    with cli._interrupt_on_termination(Plain()):
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)


def test_a_real_sigterm_raises_keyboard_interrupt_in_the_main_thread():
    import os
    import signal
    import time

    from pyguitest_recorder import cli

    if not hasattr(signal, "SIGTERM") or os.name == "nt":
        pytest.skip("Windows cannot deliver SIGTERM to itself this way")
    with cli._interrupt_on_termination(), pytest.raises(KeyboardInterrupt):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)


def test_debug_flag_reports_which_config_file_won_and_a_resolved_value(
    tmp_path, capsys
):
    # `--debug` was only ever checked for parsing; nothing asserted what it
    # prints. `_report_settings` (cli.py:821-826) is the whole implementation
    # -- a `config:` line naming the file `load_settings` picked, then every
    # setting sorted and repr'd -- which is how someone finds out *which*
    # config file actually won and what a value resolved to, not just that
    # the flag exists.
    config = tmp_path / "c.toml"
    config.write_text("motion_threshold = 25\n")
    main(["--debug", "--doctor", "--config", str(config)])
    err = capsys.readouterr().err
    assert f"config: {config}" in err
    assert "motion_threshold = 25" in err


def test_save_session_writes_a_loadable_recording(tmp_path, window):
    # test_pipeline.py only has a comment mentioning `--save-session`; nothing
    # in the suite runs the save path and reads the file back.
    from pyguitest_recorder import cli

    recording = Recording()
    recording.add(Click(timestamp=0.0, target=Target(x=10, y=20, window=window)))
    session_path = tmp_path / "session.json"
    settings = Settings(
        session_file=str(session_path), output=str(tmp_path / "script.py")
    )
    code = cli._emit(recording, settings)
    assert code == 0
    loaded = Recording.load(session_path)
    assert len(loaded.events) == 1
    assert loaded.events[0].target.x == 10


def test_save_session_strips_raw_events_unless_record_raw_is_set(tmp_path, window):
    # cli.py:623 reads `raw=recording.raw if settings.record_raw else []`.
    # In the real `_record()` flow `recording.raw` is already empty whenever
    # `record_raw` was off during capture (recorder.py:821-822), so the two
    # can never actually disagree there -- but that makes this line easy to
    # mistake for dead code rather than the thing standing between a saved
    # session and a keylogger. Forcing recording.raw and settings.record_raw
    # to disagree here is the only way to prove the *save* path itself still
    # honours the current settings, not just whatever was true at capture
    # time.
    from pyguitest_recorder import cli

    recording = Recording(raw=[{"kind": "key_press", "keysym": "a", "text": "a"}])
    recording.add(Click(timestamp=0.0, target=Target(x=10, y=20, window=window)))

    kept = tmp_path / "kept.json"
    cli._emit(
        recording,
        Settings(
            session_file=str(kept), record_raw=True, output=str(tmp_path / "a.py")
        ),
    )
    assert Recording.load(kept).raw

    stripped = tmp_path / "stripped.json"
    cli._emit(
        recording,
        Settings(
            session_file=str(stripped), record_raw=False, output=str(tmp_path / "b.py")
        ),
    )
    assert Recording.load(stripped).raw == []


def test_regenerate_writes_a_script_without_recording(saved, tmp_path, capsys):
    out = tmp_path / "script.py"
    code = main(
        ["--regenerate", str(saved), "-o", str(out), "--config", str(_empty(tmp_path))]
    )
    assert code == 0
    source = out.read_text()
    assert "import pyguitest" in source
    compile(source, str(out), "exec")


def test_regenerate_honours_absolute_coordinates(saved, tmp_path):
    out = tmp_path / "script.py"
    main(
        [
            "--regenerate",
            str(saved),
            "-o",
            str(out),
            "--absolute-coordinates",
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    assert "gui.move_mouse(10, 20)" in out.read_text()


def test_regenerate_writes_to_stdout_without_output(saved, tmp_path, capsys):
    main(["--regenerate", str(saved), "--config", str(_empty(tmp_path))])
    assert "import pyguitest" in capsys.readouterr().out


class _Stdout:
    """A stdout with a text layer over a byte buffer, as a redirected one has.

    `encoding` is the code page the text layer would use -- cp1252 is what a
    redirected Windows stdout gets -- and `tty` says whether it is a console.
    """

    def __init__(self, encoding, tty=False):
        """Set the text layer's encoding and whether this pretends to be a console."""
        import io

        self.bytes = io.BytesIO()
        self.buffer = self.bytes
        self.encoding = encoding
        self._tty = tty
        self.text = []

    def isatty(self):
        """Report the console-ness this was built with."""
        return self._tty

    def write(self, value):
        """Collect text written through the text layer, apart from the bytes."""
        self.text.append(value)
        return len(value)

    def flush(self):
        """Do nothing: there is no device behind either layer to flush."""


def _unicode_recording(tmp_path):
    """A saved recording that typed an emoji and a non-Latin word."""
    from pyguitest_recorder.model import TextInput

    window = WindowRef(title="Example", app_id="org.example.App", geometry=(0, 0, 8, 6))
    recording = Recording()
    target = Target(x=1, y=2, window=window)
    recording.add(Click(timestamp=0.0, target=target))
    recording.add(
        TextInput(
            timestamp=1.0, text="caf\u00e9 \u65e5\u672c\u8a9e \U0001f600", target=target
        )
    )
    path = tmp_path / "unicode.json"
    recording.save(path)
    return path


def test_a_redirected_script_is_utf8_even_on_a_legacy_code_page(tmp_path, monkeypatch):
    """A redirected stdout gets UTF-8 bytes whatever code page it reports.

    Found on Windows, where a *redirected* stdout takes the ANSI code page:
    `pyguitest-recorder --regenerate rec.json > script.py` died with
    "'charmap' codec can't encode" on any recording that typed an emoji or a
    non-Latin word, and left an empty file. Even text cp1252 could hold would
    have been written as cp1252 and read back by Python as invalid UTF-8.
    """
    fake = _Stdout(encoding="cp1252")
    monkeypatch.setattr("sys.stdout", fake)
    code = main(
        [
            "--regenerate",
            str(_unicode_recording(tmp_path)),
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    assert code == 0
    source = fake.bytes.getvalue().decode("utf-8")
    assert "\U0001f600" in source and "\u65e5\u672c\u8a9e" in source
    compile(source, "script.py", "exec")
    assert fake.text == []


def test_a_console_is_handed_text_not_bytes(tmp_path, monkeypatch):
    """An interactive console does its own Unicode, so it keeps getting text.

    Bytes written under it would be shown as mojibake.
    """
    fake = _Stdout(encoding="utf-8", tty=True)
    monkeypatch.setattr("sys.stdout", fake)
    main(
        [
            "--regenerate",
            str(_unicode_recording(tmp_path)),
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    assert fake.bytes.getvalue() == b""
    assert "\U0001f600" in "".join(fake.text)


def test_a_stdout_without_a_buffer_still_gets_the_script(saved, tmp_path, capsys):
    """A stream with no `.buffer` falls back to text rather than failing.

    That is what a test double or an embedding host may supply.
    """
    from pyguitest_recorder.cli import _write_source

    class Bare:
        """A stand-in stream: a text `write` and nothing underneath it."""

        def __init__(self):
            """Start with nothing written."""
            self.written = ""

        def isatty(self):
            """Never a console."""
            return False

        def write(self, value):
            """Accumulate the text handed to it."""
            self.written += value

    import sys

    bare, real = Bare(), sys.stdout
    sys.stdout = bare
    try:
        _write_source("print('hi')\n")
    finally:
        sys.stdout = real
    assert bare.written == "print('hi')\n"


def test_diagnostics_survive_a_character_the_code_page_cannot_hold():
    """A character the code page cannot encode does not end a `--doctor` report.

    `--doctor` prints paths and notes that quote things this program does not
    control -- a user name, a window title. On a redirected Windows stdout one
    non-ANSI character in any of them ended the report with a traceback.
    """
    import io

    from pyguitest_recorder.cli import _tolerate_unencodable_output

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
    import sys

    real = sys.stdout
    sys.stdout = stream
    try:
        _tolerate_unencodable_output()
        print("C:\\Users\\\u5c71\u7530\\config.toml")
        stream.flush()
    finally:
        sys.stdout = real
    assert b"config.toml" in raw.getvalue()


def test_stop_progress_message_names_the_configured_key_and_interval():
    settings = Settings(stop_key="ctrl+Escape", stop_key_interval=2.0)
    message = _stop_progress_message(settings, 1, 2)
    assert message == "ctrl+Escape (1/2) -- press again within 2s to stop."


def test_suppress_flags_default_off_and_emit_nothing(saved, tmp_path):
    out = tmp_path / "script.py"
    main(
        ["--regenerate", str(saved), "-o", str(out), "--config", str(_empty(tmp_path))]
    )
    source = out.read_text()
    assert "KeymapWarning" not in source
    assert "dbind" not in source


def test_suppress_keymap_warning_flag_reaches_the_generated_script(saved, tmp_path):
    out = tmp_path / "script.py"
    main(
        [
            "--regenerate",
            str(saved),
            "-o",
            str(out),
            "--suppress-keymap-warning",
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    source = out.read_text()
    assert "from pyguitest import KeymapWarning" in source
    assert 'warnings.filterwarnings("ignore", category=KeymapWarning)' in source
    compile(source, str(out), "exec")


def test_suppress_atspi_chatter_flag_reaches_the_generated_script(saved, tmp_path):
    out = tmp_path / "script.py"
    main(
        [
            "--regenerate",
            str(saved),
            "-o",
            str(out),
            "--suppress-atspi-chatter",
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    source = out.read_text()
    assert "from gi.repository import GLib" in source
    assert '"dbind"' in source
    compile(source, str(out), "exec")


def test_missing_recording_is_reported(tmp_path, capsys):
    code = main(
        ["--regenerate", str(tmp_path / "nope.json"), "--config", str(_empty(tmp_path))]
    )
    assert code == 2
    assert "pyguitest-recorder:" in capsys.readouterr().err


def test_bad_config_is_reported(tmp_path, capsys):
    path = tmp_path / "bad.toml"
    path.write_text("[analyzer]\nnonsense = 1\n")
    assert main(["--config", str(path), "--doctor"]) == 2
    assert "unknown setting" in capsys.readouterr().err


def test_a_non_finite_timeout_factor_flag_is_reported_not_crashed(tmp_path, capsys):
    # argparse's plain type=float accepts "inf" as readily as an ordinary
    # number; unvalidated, it would reach math.ceil() in the analyzer instead
    # of being refused here, before a capture backend ever opens.
    code = main(
        [
            "--timeout-factor",
            "inf",
            "--config",
            str(_empty(tmp_path)),
            "--doctor",
        ]
    )
    assert code == 2
    assert "timeout_factor" in capsys.readouterr().err


def test_a_negative_event_delay_flag_is_reported_not_crashed(tmp_path, capsys):
    # The same shape as --timeout-factor above: argparse takes the number, and
    # unrefused it would be written into a generated script as a literal and
    # raise from time.sleep at replay rather than here.
    code = main(
        [
            "--event-delay",
            "-1",
            "--config",
            str(_empty(tmp_path)),
            "--doctor",
        ]
    )
    assert code == 2
    assert "event_delay" in capsys.readouterr().err


def test_the_event_delay_flag_reaches_the_generator():
    # The flag exists so a run can pace its own generated script without
    # hand-editing the file; that is only true if it reaches the options the
    # generator reads.
    assert _generator_options(Settings()).event_delay is None
    assert _generator_options(Settings(event_delay=0.3)).event_delay == 0.3
    assert _generator_options(Settings(event_delay=0.0)).event_delay == 0.0


def test_doctor_reports_without_touching_the_desktop(tmp_path, capsys):
    main(["--doctor", "--config", str(_empty(tmp_path))])
    out = capsys.readouterr().out
    assert "generator profile:" in out
    assert "capture:" in out
    assert "config searched:" in out


def test_summary_counts_observed_and_inferred(saved, tmp_path, capsys):
    main(
        [
            "--regenerate",
            str(saved),
            "-o",
            str(tmp_path / "s.py"),
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    # The click is the one observed event; the wait for its window to exist
    # is inferred, and would not have been there before the analyzer ran.
    assert "2 events (1 observed, 1 inferred)" in capsys.readouterr().err


def test_sync_inference_can_be_turned_off(saved, tmp_path, capsys):
    main(
        [
            "--regenerate",
            str(saved),
            "--no-sync-inference",
            "-o",
            str(tmp_path / "s.py"),
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    assert "1 events (1 observed, 0 inferred)" in capsys.readouterr().err


def test_the_generator_and_the_analyzer_agree_on_what_one_wait_may_take():
    # A generated file's `Timeouts:` line is a claim about the waits in it, so
    # every number on it has to be the one the analyzer applied: the floor is
    # one setting handed to both sides, and the cap and factor are the
    # analyzer's own, which the generator used to answer with literals instead
    # -- a header advertising five minutes, or a plain rounding, over numbers
    # actually cut back or multiplied by something else.
    sync, generated = _sync_options(Settings()), _generator_options(Settings())
    assert generated.default_timeout == sync.min_timeout
    assert generated.max_timeout == sync.max_timeout
    assert generated.factor == sync.factor


def test_max_timeout_and_timeout_factor_settings_reach_both_sides():
    # `max_timeout` and `timeout_factor` exist so a caller can change the cap
    # or the multiplier without hand-editing every generated file; that is
    # only true if both settings actually reach the analyzer and the header
    # that describes it, not just one or the other.
    settings = Settings(max_timeout=45.0, timeout_factor=2.5)
    sync, generated = _sync_options(settings), _generator_options(settings)
    assert sync.max_timeout == 45.0
    assert sync.factor == 2.5
    assert generated.max_timeout == 45.0
    assert generated.factor == 2.5


def _empty(tmp_path):
    path = tmp_path / "empty.toml"
    if not path.exists():
        path.write_text("")
    return path


def test_the_check_key_can_be_set_and_switched_off():
    from pyguitest_recorder.cli import _overrides

    parser = build_parser()
    assert _overrides(parser.parse_args(["--check-key", "F12"]))["check_key"] == "F12"
    assert _overrides(parser.parse_args(["--no-checks"]))["check_key"] == ""
    # Unset means the default survives, rather than being overwritten with None.
    assert _overrides(parser.parse_args([]))["check_key"] is None


def test_record_motion_has_a_flag_not_only_a_config_key():
    # It had only a config key, so the one setting that makes hover-driven
    # menu navigation recordable was undiscoverable from --help.
    from pyguitest_recorder.cli import _overrides

    parser = build_parser()
    assert _overrides(parser.parse_args(["--record-motion"]))["record_motion"] is True
    assert _overrides(parser.parse_args([]))["record_motion"] is None


def test_motion_has_flags_and_not_only_config_keys():
    from pyguitest_recorder.cli import _overrides

    parser = build_parser()
    assert _overrides(parser.parse_args(["--natural-motion"]))["motion"] == "natural"
    assert _overrides(parser.parse_args(["--recorded-motion"]))["motion"] == "recorded"
    assert _overrides(parser.parse_args(["--verbatim-motion"]))["motion"] == "verbatim"
    assert _overrides(parser.parse_args(["--teleport-motion"]))["motion"] == "teleport"
    assert _overrides(parser.parse_args(["--max-waypoints", "3"]))["max_waypoints"] == 3
    # Unset overrides nothing, so a config file's choice survives.
    assert _overrides(parser.parse_args([]))["motion"] is None
    assert _overrides(parser.parse_args([]))["max_waypoints"] is None


def test_click_via_has_a_flag():
    from pyguitest_recorder.cli import _overrides

    parser = build_parser()
    for route in ("auto", "pointer", "action"):
        args = parser.parse_args(["--click-via", route])
        assert _overrides(args)["click_via"] == route
    # Unset overrides nothing, so a config file's choice survives.
    assert _overrides(parser.parse_args([]))["click_via"] is None


# -- trimming ----------------------------------------------------------


def _events():
    recording = _clicks(((0, 0), 0.0), ((1, 1), 5.0), ((2, 2), 9.0), ((3, 3), 12.0))
    return recording.events


def test_from_drops_everything_before_it():
    kept = _trimmed_events(_events(), trim_from=5.0, trim_to=None, drop=None)
    assert [e.timestamp for e in kept] == [5.0, 9.0, 12.0]


def test_to_drops_everything_at_or_after_it():
    kept = _trimmed_events(_events(), trim_from=None, trim_to=9.0, drop=None)
    assert [e.timestamp for e in kept] == [0.0, 5.0]


def test_from_and_to_together_keep_a_window():
    kept = _trimmed_events(_events(), trim_from=5.0, trim_to=12.0, drop=None)
    assert [e.timestamp for e in kept] == [5.0, 9.0]


def test_drop_removes_specific_indices():
    kept = _trimmed_events(
        _events(), trim_from=None, trim_to=None, drop=frozenset({0, 2})
    )
    assert [e.timestamp for e in kept] == [5.0, 12.0]


def test_drop_indices_are_into_the_original_list_not_the_survivors():
    # --from 5 --drop 0 means "drop event 0 of the original four" (t=0.0,
    # already excluded by --from) -- not "drop the first survivor" (t=5.0).
    kept = _trimmed_events(_events(), trim_from=5.0, trim_to=None, drop=frozenset({0}))
    assert [e.timestamp for e in kept] == [5.0, 9.0, 12.0]


def test_an_out_of_range_drop_index_raises():
    with pytest.raises(ValueError, match="out of range"):
        _trimmed_events(_events(), trim_from=None, trim_to=None, drop=frozenset({99}))


def test_no_trim_arguments_returns_every_event_unchanged():
    events = _events()
    assert _trimmed_events(events, trim_from=None, trim_to=None, drop=None) == events


def test_regenerate_with_from_drops_the_early_fumbling(multi_saved, tmp_path):
    out = tmp_path / "s.py"
    main(
        [
            "--regenerate",
            str(multi_saved),
            "--from",
            "5",
            "--absolute-coordinates",
            "-o",
            str(out),
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    source = out.read_text()
    assert "gui.move_mouse(10, 20)" not in source
    assert "gui.move_mouse(30, 40)" in source
    assert "gui.move_mouse(70, 80)" in source


def test_regenerate_with_drop_removes_the_mis_click(multi_saved, tmp_path):
    out = tmp_path / "s.py"
    main(
        [
            "--regenerate",
            str(multi_saved),
            "--drop",
            "1",
            "--absolute-coordinates",
            "-o",
            str(out),
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    source = out.read_text()
    assert "gui.move_mouse(30, 40)" not in source
    assert "gui.move_mouse(10, 20)" in source
    assert "gui.move_mouse(50, 60)" in source
    assert "gui.move_mouse(70, 80)" in source


def test_an_invalid_drop_index_is_reported_not_a_traceback(
    multi_saved, tmp_path, capsys
):
    code = main(
        [
            "--regenerate",
            str(multi_saved),
            "--drop",
            "99",
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    assert code == 2
    assert "out of range" in capsys.readouterr().err


def test_drop_parses_a_comma_separated_list():
    args = build_parser().parse_args(["--drop", "3,7,12"])
    assert args.drop == frozenset({3, 7, 12})


def test_a_malformed_drop_list_is_rejected_by_the_parser(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--drop", "not-a-number"])


def test_trimming_recomputes_the_leading_delay():
    # The survivors keep whatever delay they had, so the first one carried the
    # gap to a predecessor the trim dropped -- a spurious leading sleep under
    # recorded/verbatim motion. Recomputed as `Recording.from_dict` does.
    from pyguitest_recorder.cli import _trimmed_recording

    recording = _clicks(((0, 0), 0.0), ((1, 1), 5.0), ((2, 2), 9.0))
    # The second click's delay is the 5s gap back to the first.
    assert recording.events[1].delay == 5.0
    trimmed = _trimmed_recording(recording, trim_from=5.0, trim_to=None, drop=None)
    assert [e.timestamp for e in trimmed.events] == [5.0, 9.0]
    # Now first, its stale 5s gap to the dropped event becomes 0, not a sleep.
    assert trimmed.events[0].delay == 0.0
    assert trimmed.events[1].delay == 4.0
    # The recomputation copies each event first, so the ORIGINAL recording's
    # events keep their delays -- the survivor that became trimmed.events[0]
    # still reads 5.0 in `recording`, not the recomputed 0.0.
    assert recording.events[1].delay == 5.0
    assert trimmed.events[0] is not recording.events[1]


def test_record_raw_with_a_trim_is_refused(saved, tmp_path, capsys):
    # The raw stream is a parallel capture a trim cannot touch, so a saved
    # session would keep in the clear exactly the keystrokes the trim removed --
    # refused the way settings.py refuses record_raw with sensitive/exclude.
    code = main(
        [
            "--regenerate",
            str(saved),
            "--from",
            "5",
            "--record-raw",
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "record_raw" in err and "trim" in err


def test_saving_a_recording_says_what_the_file_holds(tmp_path, window, capsys):
    """A saved recording is sensitive whether or not --record-raw was given.

    Redaction is applied by the generator, so the events in the file carry
    the text that was typed even when the script withheld it -- and the
    only thing said at the moment the file appeared was "Recording saved
    to X". `_emit` directly, not `--regenerate`: that path passes
    `save_session=False` and writes no session file at all.
    """
    from pyguitest_recorder import cli

    recording = Recording()
    recording.add(Click(timestamp=0.0, target=Target(x=10, y=20, window=window)))
    session_path = tmp_path / "session.json"
    settings = Settings(
        session_file=str(session_path), output=str(tmp_path / "script.py")
    )
    assert cli._emit(recording, settings) == 0
    err = capsys.readouterr().err
    assert "password file" in err
    assert "script redaction does not cover" in err
    # Not claimed unless it is true: no raw stream was asked for here.
    assert "raw event stream" not in err


def test_a_raw_recording_says_so_as_well(tmp_path, window, capsys):
    from pyguitest_recorder import cli

    recording = Recording(raw=[{"kind": "key_press", "keysym": "a", "text": "a"}])
    recording.add(Click(timestamp=0.0, target=Target(x=10, y=20, window=window)))
    session_path = tmp_path / "raw.json"
    settings = Settings(
        session_file=str(session_path),
        output=str(tmp_path / "script.py"),
        record_raw=True,
    )
    assert cli._emit(recording, settings) == 0
    err = capsys.readouterr().err
    assert "raw event stream" in err
    assert "password file" in err


def test_record_raw_with_a_drop_is_refused_too(saved, tmp_path, capsys):
    # --drop leaves the raw stream just as whole as --from/--to do.
    code = main(
        [
            "--regenerate",
            str(saved),
            "--drop",
            "0",
            "--record-raw",
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    assert code == 2
    assert "record_raw" in capsys.readouterr().err


def test_from_after_to_is_refused(saved, tmp_path, capsys):
    # An empty window silently dropped every event; refused with both bounds
    # in hand, which the per-value parse check cannot see.
    code = main(
        [
            "--regenerate",
            str(saved),
            "--from",
            "10",
            "--to",
            "5",
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    assert code == 2
    assert "empty" in capsys.readouterr().err


def test_a_non_finite_trim_bound_is_rejected_by_the_parser():
    # Plain type=float took inf/nan, both of which silently drop every event.
    for arg in ("--from", "--to"):
        for value in ("inf", "nan", "-1"):
            with pytest.raises(SystemExit):
                build_parser().parse_args([arg, value])


def test_a_trim_that_drops_every_event_says_so(multi_saved, tmp_path, capsys):
    # --to 0 keeps nothing (every timestamp is >= 0); the notice is the
    # _trimmed_events docstring's "notice, don't trim quietly" stance.
    main(
        [
            "--regenerate",
            str(multi_saved),
            "--to",
            "0",
            "--absolute-coordinates",
            "-o",
            str(tmp_path / "s.py"),
            "--config",
            str(_empty(tmp_path)),
        ]
    )
    assert "removed every event" in capsys.readouterr().err


def test_backend_flag_reaches_the_resolved_settings(tmp_path):
    # `backend` was settable only via the TOML config, yet it is central to the
    # SSH/Wayland recording workflow. The flag overrides it like every other
    # capture setting.
    from pyguitest_recorder.cli import _overrides
    from pyguitest_recorder.config import load_settings

    args = build_parser().parse_args(["--backend", "wayland"])
    assert _overrides(args)["backend"] == "wayland"
    settings, _ = load_settings(str(_empty(tmp_path)))
    assert settings.merged(**_overrides(args)).backend == "wayland"
    # Unset overrides nothing, so a config file's choice survives.
    assert _overrides(build_parser().parse_args([]))["backend"] is None


def test_backend_flag_refuses_an_unknown_name():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--backend", "nonsense"])


class TestTheDoctorVerdict:
    """The one line `--doctor` exists to print, so it must not overstate."""

    def _verdict(self, **fields):
        from pyguitest_recorder.cli import _verdict
        from pyguitest_recorder.recorder import ContextReport

        return _verdict(True, ContextReport(**fields))

    def test_elements_available_reads_as_ready(self):
        assert "clicks will be named" in self._verdict(windows=True, elements=True)

    def test_no_at_bridge_qualifies_the_verdict_rather_than_contradicting_it(self):
        """Found live: this verdict sat three lines under its own refutation.

        `NO_AT_BRIDGE` was set on a MATE session, the note above said GTK3 and
        Qt would publish nothing, and the verdict still read "ready to record,
        and clicks will be named" -- which a reader who trusts the one summary
        line, as this line exists to be trusted, would have acted on. A
        mate-calc launched from that environment resolved to no element at all.
        """
        verdict = self._verdict(windows=True, elements=True, bridge_disabled=True)
        assert "no click named" in verdict
        assert "NO_AT_BRIDGE" in verdict
        # Precisely true either way: the recorder reads its *own* environment
        # as a proxy for the one the application was launched in, and a real
        # run on this desktop launched mate-calc without the variable and did
        # name every click. Claiming the outcome rather than the risk would
        # have made that recording's own notes contradict what it did.
        assert "inherits" in verdict

    def test_no_elements_still_reads_as_window_relative(self):
        assert "window-relative" in self._verdict(windows=True, elements=False)

    def test_nothing_at_all_still_reads_as_bare_coordinates(self):
        assert "bare screen coordinate" in self._verdict(windows=False, elements=False)


class TestDoctorOnWindows:
    """What `--doctor` says that only a Windows session can get wrong."""

    def _detected(self, **fields):
        """What pyguitest's detection returns on Windows, with `fields` changed."""
        from types import SimpleNamespace

        base = {
            "session_type": "SessionType.WIN32",
            "compositor": "Compositor.DWM",
            "desktop": "",
            "is_elevated": False,
            "foreground_is_elevated": False,
            "low_level_hooks_timeout_ms": 300,
        }
        return SimpleNamespace(**{**base, **fields})

    def _capture(self, monkeypatch, backend):
        """Fix which capture backend `--doctor` finds, whatever this host offers.

        `backend` is the name it selects, or None for none being available.
        The report's platform follows the backend when there is one, so a test
        that leaves it to the host passes on one operating system and fails on
        the next.
        """
        from types import SimpleNamespace

        from pyguitest_recorder import cli
        from pyguitest_recorder.backends.base import CaptureUnavailable
        from pyguitest_recorder.recorder import ContextReport

        def choose(_settings):
            """Answer as the host would if `backend` were all it had."""
            if backend is None:
                raise CaptureUnavailable("no capture here")
            return SimpleNamespace(name=backend)

        monkeypatch.setattr(cli, "choose_backend", choose)
        monkeypatch.setattr(cli, "probe_context", lambda _s: ContextReport())

    def _lines(self, monkeypatch, detected):
        """The Windows-only lines `--doctor` prints for this detection."""
        import pyguitest

        from pyguitest_recorder.cli import _windows_lines

        monkeypatch.setattr(pyguitest, "detect", lambda *_a, **_k: detected)
        return _windows_lines()

    def test_an_unelevated_recorder_is_told_what_it_cannot_reach(self, monkeypatch):
        """An unelevated recorder is told about the elevated windows it cannot see."""
        lines = self._lines(monkeypatch, self._detected())
        text = "\n".join(lines)
        assert "elevation:         not elevated" in text
        assert "300 ms" in text
        # An elevated window's input is the silent gap: no error, no note, just
        # missing events -- so the doctor has to be where it is said.
        assert "elevated window" in text and "administrator" in text

    def test_an_elevated_foreground_window_gets_the_sharper_warning(self, monkeypatch):
        """An elevated window in front right now is named, not just possible."""
        lines = self._lines(monkeypatch, self._detected(foreground_is_elevated=True))
        assert any("in front right now is elevated" in line for line in lines)
        assert not any("Task Manager" in line for line in lines)

    def test_an_elevated_recorder_is_not_warned(self, monkeypatch):
        """An elevated recorder sees everything, so it is given no warning."""
        lines = self._lines(monkeypatch, self._detected(is_elevated=True))
        assert "elevation:         elevated (administrator)" in "\n".join(lines)
        assert not any(line.startswith("note:") for line in lines)

    def test_an_older_pyguitest_with_none_of_the_fields_prints_nothing_wrong(
        self, monkeypatch
    ):
        """A pyguitest that carries none of the fields yields no lines, not an error."""
        from types import SimpleNamespace

        bare = SimpleNamespace(session_type="SessionType.WIN32")
        assert self._lines(monkeypatch, bare) == []

    def test_a_detection_that_raises_does_not_fail_the_diagnostic(self, monkeypatch):
        """A detection that raises leaves the Windows lines out and `--doctor` up."""
        import pyguitest

        from pyguitest_recorder.cli import _windows_lines

        def boom(*_a, **_k):
            """Stand in for a detector that cannot run."""
            raise RuntimeError("no detector")

        monkeypatch.setattr(pyguitest, "detect", boom)
        assert _windows_lines() == []

    def test_a_windows_report_has_no_x_display_line_to_misread(
        self, monkeypatch, tmp_path, capsys
    ):
        """A native Windows report says `$DISPLAY` is not applicable, not unset."""
        import pyguitest

        self._capture(monkeypatch, "win32")
        monkeypatch.setattr(pyguitest, "detect", lambda *_a, **_k: self._detected())
        main(["--doctor", "--config", str(_empty(tmp_path))])
        out = capsys.readouterr().out
        assert "display:           n/a (Windows has no X display)" in out
        assert "display:           unset" not in out
        assert "elevation:" in out

    def test_a_linux_report_keeps_its_display_line_and_has_no_windows_lines(
        self, monkeypatch, tmp_path, capsys
    ):
        """An X11 report keeps its display line and gets no Windows-only ones."""
        import pyguitest

        self._capture(monkeypatch, "xrecord")
        monkeypatch.setattr(
            pyguitest,
            "detect",
            lambda *_a, **_k: self._detected(session_type="SessionType.X11"),
        )
        main(["--doctor", "--config", str(_empty(tmp_path))])
        out = capsys.readouterr().out
        assert "display:" in out and "n/a (Windows" not in out
        assert "elevation:" not in out and "hook timeout:" not in out

    def test_an_x_recording_on_a_windows_host_is_reported_as_an_x_one(
        self, monkeypatch, tmp_path, capsys
    ):
        """Selecting `xrecord` on Windows gets the X display and no Windows advice.

        Windows can run an X server (Xming, VcXsrv, WSLg), so pyguitest still
        detects a Windows session there. The recording is of that server's
        clients all the same, and the report used to hide the display being
        recorded and advise about elevation for a recording that has none.
        """
        import pyguitest

        self._capture(monkeypatch, "xrecord")
        monkeypatch.setenv("DISPLAY", ":7")
        monkeypatch.setattr(pyguitest, "detect", lambda *_a, **_k: self._detected())
        main(["--doctor", "--config", str(_empty(tmp_path))])
        out = capsys.readouterr().out
        assert "capture:           xrecord (available)" in out
        assert "display:           :7" in out and "n/a (Windows" not in out
        assert "elevation:" not in out and "hook timeout:" not in out

    def test_a_win32_backend_is_reported_as_windows_whatever_the_session_says(
        self, monkeypatch, tmp_path, capsys
    ):
        """The selected backend outranks the detected session when capture works."""
        import pyguitest

        self._capture(monkeypatch, "win32")
        monkeypatch.setenv("DISPLAY", ":7")
        monkeypatch.setattr(
            pyguitest,
            "detect",
            lambda *_a, **_k: self._detected(session_type="SessionType.X11"),
        )
        main(["--doctor", "--config", str(_empty(tmp_path))])
        out = capsys.readouterr().out
        assert "display:           n/a (Windows has no X display)" in out
        assert "elevation:" in out


def test_help_names_the_three_modes():
    """Recording is what happens with no flag, and `--help` never said so.

    `--doctor` and `--regenerate` are flags rather than subcommands, so the
    listing is a wall of options in which the tool's whole purpose -- record
    a GUI interaction -- is the one mode a reader cannot find. The epilog is
    where the three are named together.
    """
    help_text = build_parser().format_help()
    assert "--regenerate" in help_text and "--doctor" in help_text
    assert "with no flag it records" in help_text


class TestGeneratedScriptPermissions:
    """A generated script is as private as the session file beside it.

    `write_text` left it at 0644 under the usual umask while
    `Recording.save` has always used `mkstemp`'s 0600 -- and the script
    carries every string the recording typed, password text included under
    `--no-redact`. Two artifacts of one recording disagreeing about who may
    read them is the kind of gap nobody checks.
    """

    def _emit_to(self, path, recording, **settings):
        from pyguitest_recorder import cli

        cli._emit(recording, Settings(output=str(path), **settings))

    @pytest.fixture
    def recording(self, window):
        rec = Recording()
        rec.add(Click(timestamp=0.0, target=Target(x=10, y=20, window=window)))
        return rec

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_a_new_script_is_owner_only(self, tmp_path, recording):
        script = tmp_path / "out.py"
        self._emit_to(script, recording)
        assert stat.S_IMODE(script.stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_a_script_and_its_session_file_agree(self, tmp_path, recording):
        script, session = tmp_path / "out.py", tmp_path / "out.json"
        self._emit_to(script, recording, session_file=str(session))
        assert stat.S_IMODE(script.stat().st_mode) == stat.S_IMODE(
            session.stat().st_mode
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_a_session_file_is_owner_only_on_its_own(self, tmp_path, recording):
        """Pinned directly, not only against the script's mode.

        `Recording.save` gets 0600 from `mkstemp`, which the test above
        reaches only transitively -- both files widening together would still
        agree. A session file is the credential-bearing one of the pair (raw
        keystrokes, unredacted), so the mode is load-bearing rather than
        incidental, and the obvious simplification to `write_text` would
        quietly hand it the 0644 umask default.
        """
        session = tmp_path / "session.json"
        recording.save(session)
        assert stat.S_IMODE(session.stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_a_symlinked_output_is_written_through_not_replaced(
        self, tmp_path, recording
    ):
        # `os.replace` replaces the *link*, so this reported writing
        # `latest.py` while leaving the file it named untouched and swapping
        # the link for a regular file -- a silent no-op for anyone who keeps
        # a symlink pointing at their current script.
        real = tmp_path / "script.py"
        real.write_text("# the file the link names\n", encoding="utf-8")
        link = tmp_path / "latest.py"
        link.symlink_to(real)
        self._emit_to(link, recording)
        assert link.is_symlink(), "the link itself was replaced"
        assert "gui.click()" in real.read_text(encoding="utf-8")

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_a_symlink_to_a_missing_file_creates_what_it_names(
        self, tmp_path, recording
    ):
        # A reader of the link expects the script to appear where it points.
        real = tmp_path / "not-yet.py"
        link = tmp_path / "latest.py"
        link.symlink_to(real)
        self._emit_to(link, recording)
        assert link.is_symlink()
        assert real.is_file()
        assert "gui.click()" in real.read_text(encoding="utf-8")

    def test_output_into_a_missing_directory_is_a_refusal_not_a_traceback(
        self, tmp_path, recording
    ):
        # `mkstemp` raised a raw FileNotFoundError here, which `main()`
        # (catching ConfigError and ValueError alone) let out as a
        # traceback. A ValueError naming the path is the refusal it turns
        # into a one-line message.
        with pytest.raises(ValueError, match="gone"):
            self._emit_to(tmp_path / "gone" / "out.py", recording)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_a_dangling_symlink_into_a_missing_directory_also_refuses(
        self, tmp_path, recording
    ):
        # The resolve that writes through a link can follow a dangling one
        # into a directory that no longer exists; same clean refusal.
        link = tmp_path / "latest.py"
        link.symlink_to(tmp_path / "gone" / "script.py")
        with pytest.raises(ValueError, match="gone"):
            self._emit_to(link, recording)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_regenerating_keeps_a_mode_the_user_widened(self, tmp_path, recording):
        # Someone who chmodded a generated script to commit it, or to let a
        # build user read it, asked for that; a regenerate must not undo it.
        script = tmp_path / "out.py"
        self._emit_to(script, recording)
        script.chmod(0o644)
        self._emit_to(script, recording)
        assert stat.S_IMODE(script.stat().st_mode) == 0o644

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_no_redact_overrides_a_widened_mode(self, tmp_path, recording):
        """Preserving the mode alone was not safe, which is why `restrict` exists.

        Consent to publish the old contents is not consent to publish a
        password the next run writes in clear: regenerating with
        `--no-redact` over a script somebody had chmodded to 0644 kept it
        0644, so the literal password text landed in a world-readable file.
        """
        script = tmp_path / "out.py"
        self._emit_to(script, recording)
        script.chmod(0o644)
        self._emit_to(script, recording, redact_sensitive=False)
        assert stat.S_IMODE(script.stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_no_redact_is_owner_only_on_a_new_file_too(self, tmp_path, recording):
        script = tmp_path / "fresh.py"
        self._emit_to(script, recording, redact_sensitive=False)
        assert stat.S_IMODE(script.stat().st_mode) == 0o600

    def test_no_redact_says_so_on_stderr(self, tmp_path, recording, capsys):
        # The flag is set at the start of a run and read at the end, and a run
        # can be minutes long; the warning belongs where the file appears.
        script = tmp_path / "out.py"
        self._emit_to(script, recording, redact_sensitive=False)
        err = capsys.readouterr().err
        assert "--no-redact" in err

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_the_warning_names_the_mode_where_there_is_one(
        self, tmp_path, recording, capsys
    ):
        script = tmp_path / "out.py"
        self._emit_to(script, recording, redact_sensitive=False)
        assert "is mode 0600" in capsys.readouterr().err

    def test_mode_note_invents_nothing_off_posix(self, tmp_path, monkeypatch):
        """`_mode_note` reports a mode only where there is one to report.

        Tested on the helper rather than through a whole run, because
        `os.name` is what `pathlib` dispatches on too: patching it around
        `_emit` makes `Path()` build a `WindowsPath` and raise. The target is
        therefore built before the patch, and the only call under it returns
        before it touches a path at all.
        """
        from pyguitest_recorder import cli

        target = tmp_path / "out.py"
        target.write_text("")
        target.chmod(0o600)
        if os.name == "posix":
            assert cli._mode_note(target) == " is mode 0600"
        monkeypatch.setattr(cli.os, "name", "nt")
        assert cli._mode_note(target) == ""

    def test_the_warning_claims_no_mode_where_there_is_none(
        self, tmp_path, recording, capsys, monkeypatch
    ):
        """The warning must not invent a number where `_mode_note` gives none.

        This is why the mode assertion above is guarded rather than
        universal: the first version required "0600" on every platform and
        reddened the Windows job, where there is no POSIX mode and printing
        one anyway would be the same false claim the warning was rewritten to
        avoid. Stubbing `_mode_note` covers that branch wherever the suite
        runs, not only on Windows.
        """
        from pyguitest_recorder import cli

        monkeypatch.setattr(cli, "_mode_note", lambda _path: "")
        script = tmp_path / "out.py"
        self._emit_to(script, recording, redact_sensitive=False)
        err = capsys.readouterr().err
        assert "--no-redact" in err
        assert "mode" not in err

    def test_redacting_runs_say_nothing_about_it(self, tmp_path, recording, capsys):
        script = tmp_path / "out.py"
        self._emit_to(script, recording)
        assert "--no-redact" not in capsys.readouterr().err

    def test_the_script_is_still_written_correctly(self, tmp_path, recording):
        script = tmp_path / "out.py"
        self._emit_to(script, recording)
        source = script.read_text(encoding="utf-8")
        assert "import pyguitest" in source
        assert "connect" in source

    def test_no_temp_file_is_left_behind(self, tmp_path, recording):
        script = tmp_path / "out.py"
        self._emit_to(script, recording)
        assert [p.name for p in tmp_path.iterdir()] == ["out.py"]


class TestTheConsoleHonoursSensitive:
    """`--sensitive` has to mean the same thing in the terminal and in the file.

    `describe_assertion` reads the event's own flag, which is the normalizer's
    answer about the field the text went into. `--sensitive` overrides that for
    the whole run, and the generator applies it (`_described`); this is the
    live console's half, which used to withhold the value from the script and
    then echo it to the terminal as the check was recorded.
    """

    def _assertion(self):
        return Assertion(
            timestamp=0.0,
            check="text",
            expected="hunter2",
            target=Target(x=1, y=1, element=ElementRef(role="text", name="Token")),
        )

    def test_without_the_flag_the_value_is_shown(self):
        described = describe_assertion(
            _as_secret_as_asked(Settings(), self._assertion())
        )
        assert "hunter2" in described

    def test_with_the_flag_the_value_is_withheld(self):
        described = describe_assertion(
            _as_secret_as_asked(Settings(sensitive=True), self._assertion())
        )
        assert "hunter2" not in described
        assert "matches the recorded value" in described

    def test_an_already_sensitive_event_is_untouched(self):
        event = dataclasses.replace(self._assertion(), sensitive=True)
        assert _as_secret_as_asked(Settings(), event) is event


class TestTheOutputPathIsCheckedBeforeRecording:
    """`_writable_path_problem`: refuse a destination before, not after.

    Neither path is touched until `_emit`, which for a recording is after the
    whole interaction -- so a read-only mount or a typo'd directory surfaced
    as a traceback with every captured event already discarded and nothing
    left to retry with.
    """

    def _problem(self, path, flag="--output"):
        from pyguitest_recorder.cli import _writable_path_problem

        return _writable_path_problem(str(path), flag)

    def test_a_writable_directory_is_accepted(self, tmp_path):
        assert self._problem(tmp_path / "out.py") is None

    def test_a_missing_directory_is_named(self, tmp_path):
        problem = self._problem(tmp_path / "nope" / "out.py")
        assert problem is not None
        assert "does not exist" in problem

    def test_a_directory_in_place_of_a_file_is_refused(self, tmp_path):
        target = tmp_path / "adir"
        target.mkdir()
        problem = self._problem(target)
        assert problem is not None
        assert "is a directory, not a file" in problem

    @pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
    def test_an_unwritable_directory_is_named(self, tmp_path):
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            problem = self._problem(locked / "out.py")
        finally:
            locked.chmod(0o700)
        assert problem is not None
        assert "is not writable" in problem

    @pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
    def test_a_read_only_file_in_a_writable_directory_is_accepted(self, tmp_path):
        # `_write_script` and `Recording.save` both mkstemp in the parent and
        # `os.replace` into place, which needs nothing on the target file. A
        # refusal here aborted a run before capture that would have written
        # perfectly well.
        target = tmp_path / "out.py"
        target.write_text("# old\n", encoding="utf-8")
        target.chmod(0o444)
        try:
            assert self._problem(target) is None
        finally:
            target.chmod(0o644)

    @pytest.mark.skipif(os.name != "posix", reason="symlinks")
    def test_a_symlink_is_judged_by_where_it_points(self, tmp_path):
        # Both writers `resolve()` before they write, because `os.replace`
        # would otherwise replace the *link*. Checking the unresolved parent
        # asked about the wrong directory, so this passed and the write then
        # failed after the whole recording.
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        link = tmp_path / "latest.py"
        link.symlink_to(locked / "out.py")
        try:
            problem = self._problem(link)
        finally:
            locked.chmod(0o700)
        assert problem is not None
        assert "is not writable" in problem

    def test_both_destinations_are_checked_on_the_capture_path(self, tmp_path):
        from pyguitest_recorder.cli import _validate_writable_outputs
        from pyguitest_recorder.config import ConfigError

        settings = Settings(
            output=str(tmp_path / "out.py"),
            session_file=str(tmp_path / "nope" / "s.json"),
        )
        with pytest.raises(ConfigError, match="--save-session"):
            _validate_writable_outputs(settings)

    def test_the_session_file_is_not_checked_on_a_regenerate(self, tmp_path):
        # `_regenerate` passes `save_session=False`, so no session file is
        # ever written there -- checking it refused a regenerate whose script
        # destination was perfectly writable.
        from pyguitest_recorder.cli import _validate_writable_outputs

        settings = Settings(
            output=str(tmp_path / "out.py"),
            session_file=str(tmp_path / "nope" / "s.json"),
        )
        _validate_writable_outputs(settings, session_file=False)


class TestWhatEmitSaysAboutExclusionAndMotion:
    """The two notes `_emit` prints that describe the recording, not the file."""

    def _recording(self):
        window = WindowRef(title="Editor", app_id="org.x.Editor", pid=9)
        return Recording(
            events=[
                Click(timestamp=float(i), target=Target(x=1, y=1, window=window))
                for i in range(3)
            ]
        )

    def _emit(self, settings, recording=None):
        from pyguitest_recorder.cli import _emit

        _emit(recording or self._recording(), settings, save_session=False)

    def test_an_exclusion_that_removed_nothing_says_nothing(self, capsys):
        # On the capture path `Recorder._add` has already dropped those
        # events, so this second pass can never remove any -- and the line
        # landed beside the recorder's own "N event(s) were withheld" note,
        # saying the flag had done nothing while the flag had worked.
        self._emit(Settings(exclude_windows=("*Bitwarden*",)))
        assert "Excluded" not in capsys.readouterr().err

    def test_an_exclusion_that_removed_something_reports_it(self, capsys):
        self._emit(Settings(exclude_windows=("*Editor*",)))
        err = capsys.readouterr().err
        assert "Excluded 3 of 3 events" in err
        assert "--exclude-window removed every event" in err

    def test_an_explicit_trim_still_reports_its_zero(self, capsys):
        from pyguitest_recorder.cli import _emit

        # The answer to a --from or --drop that did not match what the user
        # expected it to, so it is said even when nothing was removed.
        _emit(self._recording(), Settings(), save_session=False, trim_from=0.0)
        assert "Trimmed 0 of 3 events" in capsys.readouterr().err

    def test_a_capture_that_recorded_no_route_is_not_told_to_record_one(self, capsys):
        # `--record-motion` is already in effect here, so naming it leaves
        # the reader with no next step.
        self._emit(Settings(motion="recorded", record_motion=True))
        err = capsys.readouterr().err
        assert "no route to render" in err
        assert "--record-motion" not in err

    def test_a_regenerate_of_a_motionless_session_names_the_flag(self, capsys):
        self._emit(Settings(motion="recorded", record_motion=False))
        err = capsys.readouterr().err
        assert "no route to render" in err
        assert "--record-motion" in err
