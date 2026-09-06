import pytest

from pyguitest_recorder.model import (
    Assertion,
    Click,
    ElementRef,
    Environment,
    HotKey,
    Origin,
    Recording,
    Target,
    TextInput,
    WindowRef,
    event_from_dict,
)
from pyguitest_recorder.model.recording import FORMAT_VERSION


def test_target_relative_uses_window_origin(window):
    target = Target(x=420, y=315, window=window)
    assert target.relative == (320, 265)


def test_target_relative_is_none_without_geometry():
    target = Target(x=1, y=2, window=WindowRef(title="x"))
    assert target.relative is None


def test_event_round_trip_preserves_context(window, save_button):
    click = Click(
        timestamp=1.5,
        delay=0.25,
        target=Target(x=10, y=20, window=window, element=save_button),
        count=2,
    )
    rebuilt = event_from_dict(click.to_dict())
    assert isinstance(rebuilt, Click)
    assert rebuilt.count == 2
    assert rebuilt.target.window.app_id == "org.example.App"
    assert rebuilt.target.window.geometry == (100, 50, 800, 600)
    assert rebuilt.target.element == save_button


def test_hotkey_keys_round_trip_as_tuple():
    rebuilt = event_from_dict(HotKey(keys=("ctrl", "s")).to_dict())
    assert rebuilt.keys == ("ctrl", "s")


def test_origin_survives_round_trip():
    rebuilt = event_from_dict(TextInput(text="x", origin=Origin.MANUAL).to_dict())
    assert rebuilt.origin is Origin.MANUAL


def test_recording_round_trip(tmp_path, window):
    recording = Recording(environment=Environment(session_type="x11", xwayland=True))
    recording.add(Click(timestamp=0.0, target=Target(x=1, y=2, window=window)))
    recording.add(Click(timestamp=2.0, target=Target(x=3, y=4, window=window)))
    path = recording.save(tmp_path / "r.json")

    loaded = Recording.load(path)
    assert len(loaded.events) == 2
    assert loaded.environment.xwayland is True
    assert loaded.events[1].delay == pytest.approx(2.0)


def test_add_fills_in_delay_from_previous_event():
    recording = Recording()
    recording.add(Click(timestamp=1.0, target=Target(x=0, y=0)))
    second = recording.add(Click(timestamp=3.5, target=Target(x=0, y=0)))
    assert second.delay == pytest.approx(2.5)


def test_future_format_is_refused():
    with pytest.raises(ValueError, match="newer than this recorder"):
        Recording.from_dict({"format": FORMAT_VERSION + 1})


def test_element_addressable_requires_a_name():
    assert ElementRef(role="push button", name="Save").addressable
    assert not ElementRef(role="push button").addressable


def test_a_check_round_trips(window, save_button):
    check = Assertion(
        timestamp=3.5,
        check="text",
        target=Target(x=1, y=2, window=window, element=save_button),
        expected="Saved",
        sensitive=True,
    )
    rebuilt = event_from_dict(check.to_dict())
    assert rebuilt.check == "text"
    assert rebuilt.expected == "Saved"
    assert rebuilt.sensitive is True
    assert rebuilt.target.element.name == "Save"


def test_a_boolean_expectation_survives_the_json_form():
    # `checked` is the one check whose expected value is not a string, and
    # a recording is JSON on disk.
    rebuilt = event_from_dict(
        Assertion(check="checked", target=Target(x=0, y=0), expected=False).to_dict()
    )
    assert rebuilt.expected is False
