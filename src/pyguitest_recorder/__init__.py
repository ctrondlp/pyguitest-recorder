"""Record desktop GUI activity and generate pyguitest scripts.

The recorder observes input, resolves what each action pointed at, groups the
result into semantic events, and renders them as pyguitest source. What makes
it more than a macro recorder is the middle two steps: a click resolved to an
accessible element generates `gui.button("Save").click()`, which survives the
window moving, the theme changing and the layout being redesigned, where a
recorded coordinate survives none of them.

Capture is not one platform's property: `xrecord` reads X11's event stream, the
low-level Windows hooks read theirs, `macos` taps CGEvent at the HID tap, and
`wayland` reads the kernel's `/dev/input` devices *below* the compositor —
because no ordinary Wayland client can observe another client's input through
one, which is exactly why that backend does not go through one. Which streams a
platform publishes is the platform's business, and `backends.base` is where
that is argued.
"""

from .backends.base import CaptureUnavailable
from .config import Settings
from .config.settings import ConfigError
from .generator.python import ValidationError
from .model import Event, Recording
from .recorder import Recorder

__version__ = "0.13.0"

__all__ = [
    "CaptureUnavailable",
    "ConfigError",
    "Event",
    "Recorder",
    "Recording",
    "Settings",
    "ValidationError",
    "__version__",
]
"""The public surface.

The three errors are here for the same reason the four objects are: each is
something this package *hands a caller*, and an exception a caller cannot name
is one they cannot catch.

`ConfigError` is the clearest case -- `Settings` is exported, Settings
validation raises it from ten places, so without this a caller could construct
the class but not handle what constructing it raises. `CaptureUnavailable` is
what `choose_backend` raises, which `Recorder` reaches during its own
construction, so it is the first thing a library caller meets on a machine
with no usable capture path. `ValidationError` is the generator's, raised when
emitted calls do not match the installed pyguitest API.

Reaching them through `backends.base`, `config.settings` and
`generator.python` worked, but those are module paths this package reserves
the right to move; these names are not.
"""
