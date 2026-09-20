"""What to call the things a platform has, in text a reader will see.

A generated script and a recording's notes are read by someone sitting at the
machine they were made on, and naming another platform's mechanism in them is
worse than saying nothing: "offered AT-SPI no click action" sends a Windows
reader after an accessibility bus their machine has never had, and "no window
on the recorded display" names an X display where there is none.

Two callers, asking from opposite ends. The resolver runs *while recording*,
so `sys.platform` is right there -- but it is not the answer, because Windows
can run an X server (Xming, VcXsrv, WSLg) and `backend = "xrecord"` there
records X clients. What a recording is *of* is decided by the capture backend
that made it, so the resolver is handed that answer rather than reading the
host for itself (see `DesktopResolver.windows`). The generator runs over a
`Recording` that may have been made anywhere -- a Windows session regenerated
on Linux with `--regenerate` is an ordinary thing to do -- so it has to ask
about the recording's own environment instead, which is why `session_type` is
a parameter rather than something read here.
"""

from __future__ import annotations

import sys

__all__ = [
    "is_windows",
    "plain_name",
    "element_api",
    "foreign_focus_reason",
    "foreign_element_reason",
]


def plain_name(detected: str) -> str:
    """A detected session type or compositor, as a reader would write it.

    `Environment.session_type` and `.compositor` store the `str()` of pyguitest's
    `SessionType` and `Compositor` members -- `SessionType.X11` -- because
    `is_windows` and every recording already on disk read that form, so it is
    not changed. It is not what belongs in a file header or a diagnostic,
    though: a generated script announced `Recorded on: SessionType.X11
    (Compositor.OTHER, MATE)` where the README shows `x11 (mutter)`, the enum
    reprs having been printed as they were stored. Found running a recording on
    MATE under Python 3.12.

    Also accepts the bare spelling (`x11`) a recording or a test may already
    carry, and an empty string, which stays empty so a caller's own `or
    "unknown"` still applies.
    """
    return detected.rsplit(".", 1)[-1].lower()


def is_windows(session_type: str | None = None) -> bool:
    """Whether the session in question is a Windows one.

    `session_type` is the recording's own, as `Environment.session_type`
    stores it -- the `str()` of a `pyguitest.SessionType`, so
    `"SessionType.WIN32"`. Matched case-insensitively on the member name
    rather than parsed, since the exact spelling is pyguitest's to change and
    nothing here needs more than the answer. None asks about this machine,
    which is right for a caller with no recording and no backend to ask: it is
    what the resolver falls back to when it was built without one (see
    `DesktopResolver.windows`).
    """
    if session_type is None:
        return sys.platform == "win32"
    return "WIN32" in session_type.upper()


def element_api(session_type: str | None = None) -> str:
    """What this platform publishes accessible elements through, by name."""
    return "UI Automation" if is_windows(session_type) else "AT-SPI"


def foreign_focus_reason(session_type: str | None = None) -> str:
    """Why focus can name a process that owns none of the listed windows.

    Different mechanisms, so different sentences. On Linux the accessibility
    bus is scoped to the login session rather than to one X display, so it
    genuinely reports another session's applications. On Windows UI Automation
    is desktop-wide and the same symptom means something narrower -- an
    application this recording simply never listed a window for.
    """
    if is_windows(session_type):
        return (
            "UI Automation reports the whole desktop, so it belongs to an "
            "application this recording lists no window for"
        )
    return (
        "the accessibility bus is not scoped to one X display, so it came "
        "from another session"
    )


def foreign_element_reason(session_type: str | None = None) -> str:
    """Why an element can belong to no window this recording knows about."""
    if is_windows(session_type):
        return (
            "UI Automation reports the whole desktop, so they belong to "
            "applications this recording lists no window for"
        )
    return (
        "the accessibility bus is not scoped to one X display, so they came "
        "from another session"
    )
