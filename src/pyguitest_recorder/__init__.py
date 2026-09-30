"""Record desktop GUI activity and generate pyguitest scripts.

The recorder observes input, resolves what each action pointed at, groups the
result into semantic events, and renders them as pyguitest source. What makes
it more than a macro recorder is the middle two steps: a click resolved to an
accessible element generates `gui.button("Save").click()`, which survives the
window moving, the theme changing and the layout being redesigned, where a
recorded coordinate survives none of them.

Capture is not one platform's property: `xrecord` reads X11's event stream, the
low-level Windows hooks read theirs, and `macos` taps CGEvent at the HID tap.
Which streams a platform publishes is the platform's business — no ordinary
Wayland client can observe another client's input at all — and `backends.base`
is where that is argued.
"""

from .config import Settings
from .model import Event, Recording
from .recorder import Recorder

__version__ = "0.8.1"

__all__ = ["Event", "Recorder", "Recording", "Settings", "__version__"]
