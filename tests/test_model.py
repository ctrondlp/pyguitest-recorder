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


def test_a_non_object_event_entry_raises_a_clear_error():
    with pytest.raises(ValueError, match="not an object"):
        event_from_dict("just a string")  # type: ignore[arg-type]


def test_an_event_with_no_kind_raises_a_clear_error():
    with pytest.raises(ValueError, match="no 'kind'"):
        event_from_dict({"timestamp": 1.0})


def test_an_event_with_an_unknown_kind_raises_a_clear_error():
    with pytest.raises(ValueError, match="unknown event kind 'not_a_real_kind'"):
        event_from_dict({"kind": "not_a_real_kind"})


def test_a_malformed_target_raises_a_clear_error_naming_the_event_kind():
    # Target.x/y are required. A hand-edited recording missing one used to
    # surface as a bare KeyError three calls of indirection down in _rebuild
    # -- `--regenerate` is meant to support exactly this kind of editing, so
    # the failure has to name what went wrong.
    with pytest.raises(ValueError, match="malformed 'click' event"):
        event_from_dict({"kind": "click", "target": {"y": 2}})


def test_a_required_field_missing_entirely_raises_a_clear_error():
    # MouseMove.target has no default, so an event with none at all used to
    # surface as a bare TypeError from the dataclass constructor.
    with pytest.raises(ValueError, match="malformed 'mouse_move' event"):
        event_from_dict({"kind": "mouse_move"})


def test_recording_from_dict_rejects_a_non_object_top_level():
    with pytest.raises(ValueError, match="not a JSON object"):
        Recording.from_dict(["not", "an", "object"])  # type: ignore[arg-type]


def test_recording_from_dict_rejects_a_non_list_events_field():
    with pytest.raises(ValueError, match="'events' is not a list"):
        Recording.from_dict({"events": "oops"})


def test_recording_from_dict_names_which_event_index_is_malformed():
    with pytest.raises(ValueError, match=r"event #1: malformed 'click' event"):
        Recording.from_dict(
            {
                "events": [
                    {"kind": "click", "target": {"x": 1, "y": 2}},
                    {"kind": "click", "target": {"y": 2}},
                ]
            }
        )


def test_element_addressable_requires_a_name():
    assert ElementRef(role="push button", name="Save").addressable
    assert not ElementRef(role="push button").addressable


def test_element_clickable_matches_pyguitests_own_fallback():
    # Element.click() falls back to whichever action is named "click" or
    # "press", case-insensitively -- see AtspiBackend.Element.click().
    assert ElementRef(role="push button", name="Save", actions=("click",)).clickable
    assert ElementRef(role="push button", name="OK", actions=("Press",)).clickable
    assert not ElementRef(role="label", name="Office", actions=()).clickable
    assert not ElementRef(role="label", name="Status", actions=("expand",)).clickable


def test_element_clickable_defaults_true_when_actions_were_never_recorded():
    # None means "predates this field", not "confirmed no actions".
    assert ElementRef(role="push button", name="Save").clickable
    assert ElementRef(role="push button", name="Save", actions=None).clickable


def test_element_actions_round_trip(window):
    element = ElementRef(role="label", name="Office", actions=())
    click = Click(target=Target(x=1, y=2, window=window, element=element))
    rebuilt = event_from_dict(click.to_dict())
    assert rebuilt.target.element.actions == ()
    assert not rebuilt.target.element.clickable


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


def test_save_does_not_leave_a_temp_file_behind(tmp_path, window):
    recording = Recording()
    recording.add(Click(timestamp=0.0, target=Target(x=1, y=2, window=window)))
    path = recording.save(tmp_path / "r.json")
    assert list(tmp_path.iterdir()) == [path]


def test_save_leaves_the_previous_file_untouched_if_the_write_fails(
    tmp_path, window, monkeypatch
):
    # A crash or interrupt mid-write must not corrupt the file that was
    # already there -- write-then-rename means the old file (or nothing)
    # survives, never a truncated one.
    path = tmp_path / "r.json"
    path.write_text('{"format": 1, "events": []}', encoding="utf-8")

    recording = Recording()
    recording.add(Click(timestamp=0.0, target=Target(x=1, y=2, window=window)))

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("pyguitest_recorder.model.recording.os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        recording.save(path)

    assert path.read_text(encoding="utf-8") == '{"format": 1, "events": []}'
    assert list(tmp_path.iterdir()) == [path]


def test_a_boolean_expectation_survives_the_json_form():
    # `checked` is the one check whose expected value is not a string, and
    # a recording is JSON on disk.
    rebuilt = event_from_dict(
        Assertion(check="checked", target=Target(x=0, y=0), expected=False).to_dict()
    )
    assert rebuilt.expected is False
