import pytest

from pyguitest_recorder.cli import build_parser, main
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
