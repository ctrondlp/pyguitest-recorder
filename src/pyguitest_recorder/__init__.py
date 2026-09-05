"""Record desktop GUI activity and generate pyguitest scripts.

The recorder observes input, resolves what each action pointed at, groups the
result into semantic events, and renders them as pyguitest source. What makes
it more than a macro recorder is the middle two steps: a click resolved to an
accessible element generates `gui.button("Save").click()`, which survives the
window moving, the theme changing and the layout being redesigned, where a
recorded coordinate survives none of them.

Capture is X11-only, and that is a property of the platform rather than a
missing feature. See `backends.base` for why.
"""

from .config import Settings
from .model import Event, Recording
from .recorder import Recorder

__version__ = "0.1.0"

__all__ = ["Event", "Recorder", "Recording", "Settings", "__version__"]
