import pytest

from pyguitest_recorder.backends.base import RawEvent
from pyguitest_recorder.model import ElementRef, Target, WindowRef
from pyguitest_recorder.windows import Observation


class FakeResolver:
    """Resolves every point to a fixed window, and elements by rectangle."""

    def __init__(self, window=None, elements=(), text=None, checked=None, focus=None):
        self.window = window
        self.elements = list(elements)
        self.text = text
        self.checked = checked
        self.focus = focus

    def resolve(self, x, y, screen=0):
        element = None
        for rect, ref in self.elements:
            ex, ey, ew, eh = rect
            if ex <= x < ex + ew and ey <= y < ey + eh:
                element = ref
                break
        return Target(x=x, y=y, screen=screen, window=self.window, element=element)

    def inspect(self, x, y, screen=0):
        return Observation(
            target=self.resolve(x, y, screen),
            text=self.text,
            checked=self.checked,
            checkable=self.checked is not None,
        )

    def focused(self):
        """What has keyboard focus, which most desktops cannot say."""
        return self.focus

    def close(self):
        pass


@pytest.fixture
def window():
    return WindowRef(
        title="Example", app_id="org.example.App", pid=99, geometry=(100, 50, 800, 600)
    )


@pytest.fixture
def save_button():
    return ElementRef(role="push button", name="Save")


@pytest.fixture
def press():
    def make(t, x=200, y=200, button=1):
        return RawEvent(kind="button_press", timestamp=t, x=x, y=y, button=button)

    return make


@pytest.fixture
def release():
    def make(t, x=200, y=200, button=1):
        return RawEvent(kind="button_release", timestamp=t, x=x, y=y, button=button)

    return make


@pytest.fixture
def key():
    def make(t, keysym, text="", kind="key_press"):
        return RawEvent(kind=kind, timestamp=t, keysym=keysym, text=text)

    return make
