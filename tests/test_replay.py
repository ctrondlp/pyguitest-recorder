"""Generated scripts executed, not just read.

`validate()` checks a script's names and keywords against the installed
pyguitest; the pipeline tests check its text. Neither would notice a script
that parses, names real methods, and then does the wrong thing when it runs --
a click sent to the wrong element, a hotkey never sent, a wait that raises.
These run the generated source through `exec` against a real `pyguitest.Session`
over a fake backend that records what reached it, which is the contract between
the two packages: the recorder's output is pyguitest's input.
"""

import pyguitest
import pytest
from pyguitest import Capability, Role
from pyguitest.backends.base import GUIBackend, Window
from pyguitest.capabilities import CapabilitySet

from conftest import FakeResolver
from pyguitest_recorder.analyzer import Normalizer
from pyguitest_recorder.backends.base import RawEvent
from pyguitest_recorder.generator import generate, validate
from pyguitest_recorder.model import ElementRef, Environment, Recording, WindowRef


class FakeElement:
    def __init__(self, role, name):
        self.role = role
        self.name = name
        self.description = ""
        self.enabled = True
        self.visible = True
        self.pid = None
        self.log = []

    def click(self):
        self.log.append(("click",))

    def set_text(self, text):
        self.log.append(("set_text", text))

    def focus(self):
        self.log.append(("focus",))

    def _bind_session(self, session):
        self._session = session


class ReplayBackend(GUIBackend):
    """Declares everything and records the calls a replay makes.

    Declaring every capability is deliberate: a script's `gui.require(...)`
    then passes whatever it asks for, and a call the fake does not implement
    raises the library's own typed error rather than silently succeeding.
    """

    name = "replay"

    def __init__(self, elements=(), window=None, origin=(100, 50)):
        self.elements = list(elements)
        self.calls = []
        self.origin = origin
        self._windows = []
        if window is not None:
            self._windows = [
                Window("w1", self, title=window.title, app_id=window.app_id)
            ]

    @property
    def capabilities(self):
        # Everything but push notification: a replay then polls windows(),
        # which is what a desktop without window events does.
        return CapabilitySet(set(Capability) - {Capability.WINDOW_EVENTS})

    def geometry(self, window):
        return (*self.origin, 800, 600)

    def find_elements(self, role=None, name=None, within=None, **_):
        return [
            e
            for e in self.elements
            if (role is None or e.role == role) and (name is None or e.name == name)
        ]

    def windows(self):
        return list(self._windows)

    def move_mouse(self, x, y, screen=0):
        self.calls.append(("move_mouse", x, y))

    def press_button(self, button):
        self.calls.append(("press_button", button))

    def release_button(self, button):
        self.calls.append(("release_button", button))

    def scroll(self, dx=0, dy=0):
        self.calls.append(("scroll", dx, dy))

    def press_key(self, key):
        self.calls.append(("press_key", key))

    def release_key(self, key):
        self.calls.append(("release_key", key))

    def type_text(self, text, *args, **kwargs):
        self.calls.append(("type_text", text))

    def pointer_position(self):
        return (0, 0)


def _record(stream, resolver):
    normalizer = Normalizer(resolver=resolver, started=0.0)
    recording = Recording(environment=Environment(session_type="x11"))
    for raw in stream:
        for event in normalizer.feed(raw):
            recording.add(event)
    for event in normalizer.flush():
        recording.add(event)
    return generate(recording)


def _click(at, x, y):
    return [
        RawEvent(kind="button_press", timestamp=at, x=x, y=y, button=1),
        RawEvent(kind="button_release", timestamp=at + 0.05, x=x, y=y, button=1),
    ]


def _replay(source, backend, monkeypatch):
    """Run `source` as its own __main__ with connect() answered by `backend`."""
    assert validate(source) == []
    session = pyguitest.Session(backend, pyguitest.detect())
    monkeypatch.setattr(pyguitest, "connect", lambda *a, **k: session)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    exec(compile(source, "<generated>", "exec"), {"__name__": "__main__"})
    return session


WINDOW = WindowRef(
    title="Untitled - Text Editor",
    app_id="org.gnome.TextEditor",
    pid=4242,
    geometry=(100, 50, 800, 600),
)


def test_an_element_recording_replays_onto_the_named_elements(monkeypatch):
    resolver = FakeResolver(
        window=WINDOW,
        elements=[
            ((150, 150, 200, 40), ElementRef(role="entry", name="Document")),
            ((400, 100, 80, 30), ElementRef(role="push button", name="Save")),
        ],
    )
    stream = (
        _click(0.5, 200, 170)
        + [
            RawEvent(kind="key_press", timestamp=1.0, keysym="h", text="h"),
            RawEvent(kind="key_press", timestamp=1.1, keysym="i", text="i"),
        ]
        + _click(4.0, 430, 115)
    )
    document = FakeElement(Role.ENTRY, "Document")
    save = FakeElement(Role.PUSH_BUTTON, "Save")
    backend = ReplayBackend([document, save])

    _replay(_record(stream, resolver), backend, monkeypatch)

    assert ("set_text", "hi") in document.log
    assert save.log == [("click",)]
    # No coordinate was used: every action resolved to a named element.
    assert not [c for c in backend.calls if c[0] in ("move_mouse", "press_button")]


def test_a_window_relative_click_follows_the_window_to_where_it_now_is(monkeypatch):
    # Recorded with the window at (100, 50); replayed with it at (300, 200).
    # The click was 110 right and 80 down of the window's corner, so it must
    # land at (410, 280), not at the recorded screen point (210, 130).
    resolver = FakeResolver(window=WINDOW)
    backend = ReplayBackend(window=WINDOW, origin=(300, 200))

    _replay(_record(_click(0.5, 210, 130), resolver), backend, monkeypatch)

    moves = [c for c in backend.calls if c[0] == "move_mouse"]
    assert moves, backend.calls
    assert (moves[-1][1], moves[-1][2]) == (410, 280)
    assert ("press_button", 1) in backend.calls
    assert ("release_button", 1) in backend.calls


def test_a_hotkey_replays_in_order_and_leaves_no_modifier_held(monkeypatch):
    resolver = FakeResolver(window=WINDOW)
    stream = [
        RawEvent(kind="key_press", timestamp=1.0, keysym="Control_L"),
        RawEvent(kind="key_press", timestamp=1.1, keysym="s"),
        RawEvent(kind="key_release", timestamp=1.2, keysym="Control_L"),
    ]
    backend = ReplayBackend(window=WINDOW)

    _replay(_record(stream, resolver), backend, monkeypatch)

    keys = [(kind, key.lower()) for kind, key in backend.calls if "key" in kind]
    assert keys, backend.calls
    # A modifier pressed by the script is released again: one left held
    # corrupts every action after it.
    held = set()
    for kind, key in keys:
        (held.add if kind == "press_key" else held.discard)(key)
    assert held == set()


@pytest.mark.parametrize(
    "name",
    ["it's", 'say "hi"', "back\\slash", "tab\there", "line\nbreak", "café ☃"],
)
def test_a_name_from_the_application_survives_being_written_into_the_script(
    name, monkeypatch
):
    # Application-provided strings reach generated source as literals; one
    # escaped badly is a syntax error or, worse, a different name.
    resolver = FakeResolver(
        window=WINDOW,
        elements=[((0, 0, 500, 500), ElementRef(role="push button", name=name))],
    )
    button = FakeElement(Role.PUSH_BUTTON, name)
    backend = ReplayBackend([button])

    _replay(_record(_click(0.5, 10, 10), resolver), backend, monkeypatch)

    assert button.log == [("click",)]


@pytest.mark.parametrize("typed", ["it's", 'say "hi"', "a\b", "café", "x y"])
def test_typed_text_replays_as_exactly_what_was_typed(typed, monkeypatch):
    resolver = FakeResolver(
        window=WINDOW,
        elements=[((0, 0, 500, 500), ElementRef(role="entry", name="Field"))],
    )
    stream = _click(0.5, 10, 10) + [
        RawEvent(kind="key_press", timestamp=1.0 + i * 0.1, keysym=c, text=c)
        for i, c in enumerate(typed)
    ]
    field = FakeElement(Role.ENTRY, "Field")
    backend = ReplayBackend([field])

    _replay(_record(stream, resolver), backend, monkeypatch)

    assert ("set_text", typed) in field.log
