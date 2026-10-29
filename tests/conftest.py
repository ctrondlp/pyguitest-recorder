"""Fixtures shared across the suite: a resolved window, and raw X events.

The resolver is a fake, so the modules that only need a window with something
in it run without an X server and without an accessibility bus behind them.
"""

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
        self.focus_calls = 0

    def resolve(self, x, y, screen=0, when=None):
        element = None
        for rect, ref in self.elements:
            ex, ey, ew, eh = rect
            if ex <= x < ex + ew and ey <= y < ey + eh:
                element = ref
                break
        return Target(x=x, y=y, screen=screen, window=self.window, element=element)

    def resolve_window(self, x, y, screen=0):
        """The window only, as the recorder asks for a recorded move."""
        return Target(x=x, y=y, screen=screen, window=self.window)

    def inspect(self, x, y, screen=0):
        return Observation(
            target=self.resolve(x, y, screen),
            text=self.text,
            checked=self.checked,
            checkable=self.checked is not None,
        )

    def focused(self):
        """What has keyboard focus, which most desktops cannot say."""
        self.focus_calls += 1
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
    """Register the markers this suite defines."""
    config.addinivalue_line(
        "markers",
        "needs_ruff: asserts on output `ruff format` normalized; skipped without it",
    )
    config.addinivalue_line(
        "markers",
        "needs_via: asserts on output `click(via=...)`; skipped without "
        "pyguitest's via=",
    )


def _click_supports_via() -> bool:
    """Whether the installed pyguitest's `Element.click` accepts a `via=`.

    The click_via tests assert the generated `click(via=...)`, a keyword the
    generator only writes when the installed pyguitest actually accepts it.
    A pyguitest below the release that added `via=` makes the generator
    degrade to a plain `click()`, so those assertions cannot pass -- and must
    skip, the way the formatter-dependent tests do without `ruff`.
    """
    import inspect

    try:
        import pyguitest
    except ImportError:  # pragma: no cover - pyguitest is a hard dependency
        return False
    try:
        parameters = inspect.signature(pyguitest.Element.click).parameters
    except (AttributeError, TypeError, ValueError):  # pragma: no cover
        return False
    return "via" in parameters


def pytest_collection_modifyitems(config, items):
    """Skip the formatter-dependent tests where `ruff` is not on PATH.

    `generator.python._format` runs `ruff format` and is documented to degrade
    silently when it cannot -- the generated script is still correct, just
    laid out as the emitter left it. Tests asserting on an exact rendering
    (`gui.button("Save")`, double-quoted) are therefore asserting that a
    formatter ran, and on a machine without one they fail for a reason that is
    not a defect: thirty-three of them did, first on a fresh Windows box and
    then identically on Linux with ruff hidden from PATH, which is what showed
    it was never a platform problem.

    The gate asks `_format` itself (via `_ruff_argv`) whether it will format,
    not merely whether a `ruff` binary is on PATH -- those differ on a box
    like stock COSMIC, where ruff is importable (`python -m ruff`) but ships
    no PATH entry point, and `_format` formats through the module there. One
    detector keeps the skip decision and the production path from drifting.

    Skipped rather than loosened. Quote style is not what these tests are
    about, but the alternative -- normalizing the expected string -- would
    quietly stop checking the layout the generated scripts are actually read
    and edited in, which is the thing `_format` exists to guarantee.
    """
    from pyguitest_recorder.generator.python import _ruff_argv

    if _ruff_argv() is None:
        # The CI windows job greps this reason verbatim ("ruff is
        # unavailable") to prove the marked tests actually ran there --
        # reword it and .github/workflows/ci.yml must change with it.
        skip = pytest.mark.skip(
            reason="ruff is unavailable (not on PATH and not importable), so "
            "generated output is unformatted; install it (pip install ruff) "
            "to run the rendering assertions"
        )
        for item in items:
            if "needs_ruff" in item.keywords:
                item.add_marker(skip)
    if not _click_supports_via():
        skip = pytest.mark.skip(
            reason="the installed pyguitest's Element.click() has no via= "
            "keyword; install a newer pyguitest to run the click_via assertions"
        )
        for item in items:
            if "needs_via" in item.keywords:
                item.add_marker(skip)
