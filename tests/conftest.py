"""Fixtures shared across the suite: a resolved window, and raw X events.

The resolver is a fake, so the modules that only need a window with something
in it run without an X server and without an accessibility bus behind them.
"""

import shutil

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


def pytest_configure(config):
    """Register the one marker this suite defines."""
    config.addinivalue_line(
        "markers",
        "needs_ruff: asserts on output `ruff format` normalized; skipped without it",
    )


def pytest_collection_modifyitems(config, items):
    """Skip the formatter-dependent tests where `ruff` is not on PATH.

    `generator.python._format` shells out to `ruff format` and is documented
    to degrade silently when it cannot -- the generated script is still
    correct, just laid out as the emitter left it. Tests asserting on an
    exact rendering (`gui.button("Save")`, double-quoted) are therefore
    asserting that a formatter ran, and on a machine without one they fail
    for a reason that is not a defect: thirty-three of them did, first on a
    fresh Windows box and then identically on Linux with ruff hidden from
    PATH, which is what showed it was never a platform problem.

    Skipped rather than loosened. Quote style is not what these tests are
    about, but the alternative -- normalizing the expected string -- would
    quietly stop checking the layout the generated scripts are actually read
    and edited in, which is the thing `_format` exists to guarantee.
    """
    if shutil.which("ruff") is not None:
        return
    skip = pytest.mark.skip(
        reason="`ruff` is not on PATH, so generated output is unformatted; "
        "install it (pip install ruff) to run the rendering assertions"
    )
    for item in items:
        if "needs_ruff" in item.keywords:
            item.add_marker(skip)
