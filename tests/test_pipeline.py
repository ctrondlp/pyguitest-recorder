"""End-to-end: raw events in, a valid pyguitest script out.

Everything except the capture backend itself, which needs a live X server.
"""

from pathlib import Path

from conftest import FakeResolver
from pyguitest_recorder.analyzer import Normalizer
from pyguitest_recorder.backends.base import RawEvent
from pyguitest_recorder.config import load_settings
from pyguitest_recorder.generator import generate, validate
from pyguitest_recorder.model import ElementRef, Environment, Recording, WindowRef

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.toml"


def test_example_config_parses_and_sets_only_known_keys():
    settings, source = load_settings(EXAMPLE_CONFIG)
    assert source == EXAMPLE_CONFIG
    assert settings.stop_key == "Pause"
    assert settings.locators == "element"


def test_a_recorded_interaction_becomes_a_runnable_script():
    window = WindowRef(
        title="Untitled - Text Editor",
        app_id="org.gnome.TextEditor",
        pid=4242,
        geometry=(100, 50, 800, 600),
    )
    resolver = FakeResolver(
        window=window,
        elements=[
            ((150, 150, 200, 40), ElementRef(role="entry", name="Document")),
            ((400, 100, 80, 30), ElementRef(role="push button", name="Save")),
        ],
    )
    normalizer = Normalizer(resolver=resolver, started=100.0)
    stream = [
        RawEvent(kind="button_press", timestamp=100.5, x=200, y=170, button=1),
        RawEvent(kind="button_release", timestamp=100.55, x=200, y=170, button=1),
        RawEvent(kind="key_press", timestamp=101.0, keysym="h", text="h"),
        RawEvent(kind="key_press", timestamp=101.1, keysym="i", text="i"),
        RawEvent(kind="key_press", timestamp=101.2, keysym="Control_L"),
        RawEvent(kind="key_press", timestamp=101.3, keysym="s"),
        RawEvent(kind="key_release", timestamp=101.4, keysym="Control_L"),
        RawEvent(kind="button_press", timestamp=104.0, x=430, y=115, button=1),
        RawEvent(kind="button_release", timestamp=104.05, x=430, y=115, button=1),
    ]

    recording = Recording(environment=Environment(session_type="x11"))
    for raw in stream:
        for event in normalizer.feed(raw):
            recording.add(event)
    for event in normalizer.flush():
        recording.add(event)

    source = generate(recording)
    assert validate(source) == []
    compile(source, "<pipeline>", "exec")

    # The click landed on a text field, the typing went into it by name, the
    # hotkey used send_keys' grammar, and the second click found the button.
    assert 'gui.text_field("Document").set_text("hi")' in source
    assert 'gui.send_keys("^(s)")' in source
    assert 'gui.button("Save").click()' in source
    # No coordinate survived: every action resolved to a named element.
    assert "move_mouse" not in source


def test_a_recording_survives_being_saved_and_re_rendered(tmp_path):
    window = WindowRef(title="App", app_id="org.example.App", geometry=(0, 0, 640, 480))
    resolver = FakeResolver(window=window)
    normalizer = Normalizer(resolver=resolver, started=0.0)
    recording = Recording()
    for raw in (
        RawEvent(kind="button_press", timestamp=1.0, x=10, y=20, button=1),
        RawEvent(kind="button_release", timestamp=1.05, x=10, y=20, button=1),
    ):
        for event in normalizer.feed(raw):
            recording.add(event)
    for event in normalizer.flush():
        recording.add(event)

    before = generate(recording)
    path = recording.save(tmp_path / "r.json")
    after = generate(Recording.load(path))
    assert before == after
    assert validate(after) == []
