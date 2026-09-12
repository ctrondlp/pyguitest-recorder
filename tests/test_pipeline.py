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
    assert settings.stop_key == "Escape"
    assert settings.stop_key_presses == 2
    assert settings.check_key == "ctrl+1"
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


def test_a_recorded_check_becomes_an_assertion_in_the_script():
    """Press the check key over a label and the script verifies what it read.

    The whole pipeline, because that is where this has to work: raw key ->
    resolved observation -> Assertion -> generated call -> validated source.
    A recording of actions alone passes as long as nothing raises.
    """
    window = WindowRef(
        title="Text Editor", app_id="org.gnome.TextEditor", geometry=(0, 0, 800, 600)
    )
    resolver = FakeResolver(
        window=window,
        elements=[
            ((400, 100, 80, 30), ElementRef(role="push button", name="Save")),
            ((0, 500, 300, 20), ElementRef(role="label", name="Status")),
        ],
        text="Saved",
    )
    normalizer = Normalizer(resolver=resolver, started=0.0)
    stream = [
        RawEvent(kind="button_press", timestamp=1.0, x=430, y=115, button=1),
        RawEvent(kind="button_release", timestamp=1.05, x=430, y=115, button=1),
        RawEvent(kind="key_press", timestamp=3.4, keysym="Control_L", x=100, y=505),
        RawEvent(kind="key_press", timestamp=3.5, keysym="1", x=100, y=505),
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

    assert 'gui.button("Save").click()' in source
    assert 'gui.expect_text(role=Role.LABEL, name="Status", equals="Saved")' in source
    # The check key itself is not part of the interaction being replayed.
    assert "send_keys" not in source


def test_a_check_survives_being_saved_and_re_rendered(tmp_path):
    # A recording outlives the script generated from it, and the check is the
    # part a reader would most notice going missing.
    resolver = FakeResolver(
        window=WindowRef(title="App", app_id="org.example.App"),
        elements=[((0, 0, 500, 500), ElementRef(role="check box", name="Read only"))],
        checked=True,
    )
    normalizer = Normalizer(resolver=resolver, started=0.0)
    recording = Recording()
    for raw in (
        RawEvent(kind="key_press", timestamp=1.0, keysym="Control_L", x=10, y=10),
        RawEvent(kind="key_press", timestamp=1.05, keysym="1", x=10, y=10),
    ):
        for event in normalizer.feed(raw):
            recording.add(event)

    path = tmp_path / "rec.json"
    recording.save(path)
    source = generate(Recording.load(path))
    assert (
        'gui.expect_checked(role=Role.CHECK_BOX, name="Read only", checked=True)'
        in source
    )


class RenamingEditor:
    """One live window, renaming itself as text arrives.

    Identity by handle, exactly as pyguitest's own Window does it, because
    that is the distinction under test: same window, different title.
    """

    def __init__(self):
        self.handle = 42
        self.title = "New Document (Draft) - Text Editor"
        self.app_id = ""
        self.pid = 900
        self.typed = 0

    def rename(self):
        self.typed += 1
        self.title = f"{'Hello'[: self.typed]} (Draft) - Text Editor"

    def __eq__(self, other):
        return getattr(other, "handle", None) == self.handle

    def __hash__(self):
        return hash(self.handle)


class OneWindowSession:
    """The little of a pyguitest Session that window resolution asks for."""

    def __init__(self, window):
        self.window = window

    @property
    def capabilities(self):
        return set()

    def window_at(self, x, y, screen=0):
        return self.window

    def active_window(self):
        return self.window

    def geometry(self, window):
        return (0, 0, 800, 600)


def test_an_editor_that_renames_itself_while_typing_stays_one_window():
    """The shape the first recording of a real application came out wrong in.

    A text editor renamed its window on every keystroke. Each title read as a
    new window, so the script waited for four windows that were always one --
    and since `wait_for_window` answers None rather than raising, replay then
    failed several lines later on `None.pid`.
    """
    from pyguitest_recorder.analyzer import infer_synchronization
    from pyguitest_recorder.windows import DesktopResolver

    editor = RenamingEditor()
    made = DesktopResolver(session=OneWindowSession(editor), elements=False)
    normalizer = Normalizer(resolver=made, started=0.0)

    recording = Recording(environment=Environment(session_type="x11"))
    stamp = 1.0
    for char in "Hello":
        raw = RawEvent(kind="key_press", timestamp=stamp, keysym=char, text=char)
        for event in normalizer.feed(raw):
            recording.add(event)
        # The editor renames itself, then the user pauses long enough for the
        # analyzer to look for something to synchronize on.
        editor.rename()
        stamp += 2.0
    for event in normalizer.flush():
        recording.add(event)

    recording.events = infer_synchronization(recording.events)
    source = generate(recording)
    assert validate(source) == []
    assert source.count("gui.expect_window") == 1
    assert "New Document" in source
