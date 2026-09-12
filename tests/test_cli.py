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


def test_suppress_flags_are_parsed():
    args = build_parser().parse_args(
        ["--suppress-keymap-warning", "--suppress-atspi-chatter"]
    )
    assert args.suppress_keymap_warning is True
    assert args.suppress_atspi_chatter is True


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
