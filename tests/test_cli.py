"""The command line: the flags the parser accepts, and what they change.

Parsing is checked apart from the effects, because the two fail differently.
An unset flag that stays `None` is the mechanism by which the config file
wins, and no end-to-end test would notice the difference.
"""

import pytest

from pyguitest_recorder.cli import (
    _stop_progress_message,
    _trimmed_events,
    build_parser,
    main,
)
from pyguitest_recorder.config import Settings
from pyguitest_recorder.model import Click, Recording, Target, WindowRef


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
        import io

        self.bytes = io.BytesIO()
        self.buffer = self.bytes
        self.encoding = encoding
        self._tty = tty
        self.text = []

    def isatty(self):
        return self._tty

    def write(self, value):
        self.text.append(value)
        return len(value)

    def flush(self):
        pass


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
    # Found on Windows, where a *redirected* stdout takes the ANSI code page:
    # `pyguitest-recorder --regenerate rec.json > script.py` died with
    # "'charmap' codec can't encode" on any recording that typed an emoji or a
    # non-Latin word, and left an empty file. Even text cp1252 could hold would
    # have been written as cp1252 and read back by Python as invalid UTF-8.
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
    # An interactive console does its own Unicode; bytes written under it would
    # be shown as mojibake, so it keeps getting text.
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
    # A stand-in stream with no `.buffer` -- what a test double or an embedding
    # host may supply -- falls back to text rather than failing.
    from pyguitest_recorder.cli import _write_source

    class Bare:
        def __init__(self):
            self.written = ""

        def isatty(self):
            return False

        def write(self, value):
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
    # `--doctor` prints paths and notes that quote things this program does not
    # control -- a user name, a window title. On a redirected Windows stdout one
    # non-ANSI character in any of them ended the report with a traceback.
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
    assert "from pyguitest.backends.input import KeymapWarning" in source
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


class TestDoctorOnWindows:
    """What `--doctor` says that only a Windows session can get wrong."""

    def _detected(self, **fields):
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

    def _lines(self, monkeypatch, detected):
        import pyguitest

        from pyguitest_recorder.cli import _windows_lines

        monkeypatch.setattr(pyguitest, "detect", lambda *_a, **_k: detected)
        return _windows_lines()

    def test_an_unelevated_recorder_is_told_what_it_cannot_reach(self, monkeypatch):
        lines = self._lines(monkeypatch, self._detected())
        text = "\n".join(lines)
        assert "elevation:         not elevated" in text
        assert "300 ms" in text
        # An elevated window's input is the silent gap: no error, no note, just
        # missing events -- so the doctor has to be where it is said.
        assert "elevated window" in text and "administrator" in text

    def test_an_elevated_foreground_window_gets_the_sharper_warning(self, monkeypatch):
        lines = self._lines(monkeypatch, self._detected(foreground_is_elevated=True))
        assert any("in front right now is elevated" in line for line in lines)
        assert not any("Task Manager" in line for line in lines)

    def test_an_elevated_recorder_is_not_warned(self, monkeypatch):
        lines = self._lines(monkeypatch, self._detected(is_elevated=True))
        assert "elevation:         elevated (administrator)" in "\n".join(lines)
        assert not any(line.startswith("note:") for line in lines)

    def test_an_older_pyguitest_with_none_of_the_fields_prints_nothing_wrong(
        self, monkeypatch
    ):
        from types import SimpleNamespace

        bare = SimpleNamespace(session_type="SessionType.WIN32")
        assert self._lines(monkeypatch, bare) == []

    def test_a_detection_that_raises_does_not_fail_the_diagnostic(self, monkeypatch):
        import pyguitest

        from pyguitest_recorder.cli import _windows_lines

        def boom(*_a, **_k):
            raise RuntimeError("no detector")

        monkeypatch.setattr(pyguitest, "detect", boom)
        assert _windows_lines() == []

    def test_a_windows_report_has_no_x_display_line_to_misread(
        self, monkeypatch, tmp_path, capsys
    ):
        import pyguitest

        monkeypatch.setattr(pyguitest, "detect", lambda *_a, **_k: self._detected())
        main(["--doctor", "--config", str(_empty(tmp_path))])
        out = capsys.readouterr().out
        assert "display:           n/a (Windows has no X display)" in out
        assert "display:           unset" not in out
        assert "elevation:" in out

    def test_a_linux_report_keeps_its_display_line_and_has_no_windows_lines(
        self, monkeypatch, tmp_path, capsys
    ):
        import pyguitest

        monkeypatch.setattr(
            pyguitest,
            "detect",
            lambda *_a, **_k: self._detected(session_type="SessionType.X11"),
        )
        main(["--doctor", "--config", str(_empty(tmp_path))])
        out = capsys.readouterr().out
        assert "display:" in out and "n/a (Windows" not in out
        assert "elevation:" not in out and "hook timeout:" not in out
