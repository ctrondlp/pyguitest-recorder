"""A recording: its events, and the environment they were captured in.

The environment block is not decoration. A generated script that fails is
usually failing because the desktop it runs on differs from the one it was
recorded on, and the first question is always which difference. Recording the
session type, compositor, screen geometry and the capability set that was
available at capture time makes that answerable without reproducing anything.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import Event, event_from_dict

__all__ = ["Environment", "Recording"]

FORMAT_VERSION = 1
"""Bumped when the on-disk shape changes incompatibly."""


@dataclass
class Environment:
    """What the machine looked like when the recording was made."""

    session_type: str = ""
    compositor: str = ""
    desktop: str = ""
    """The XDG_CURRENT_DESKTOP name (e.g. "XFCE", "GNOME") -- the desktop
    environment as branded, not the coarse Compositor family used for
    backend selection."""
    display: str = ""
    screens: list[tuple[int, int, int, float]] = field(default_factory=list)
    """(index, width, height, scale) for each screen the session reported."""
    capture_backend: str = ""
    capabilities: list[str] = field(default_factory=list)
    pyguitest_version: str = ""
    recorder_version: str = ""
    xwayland: bool = False
    notes: list[str] = field(default_factory=list)
    recorded_at: str = ""
    """Local date/time the recording started, formatted for display."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "session_type": self.session_type,
            "compositor": self.compositor,
            "desktop": self.desktop,
            "display": self.display,
            "screens": [list(s) for s in self.screens],
            "capture_backend": self.capture_backend,
            "capabilities": list(self.capabilities),
            "pyguitest_version": self.pyguitest_version,
            "recorder_version": self.recorder_version,
            "xwayland": self.xwayland,
            "notes": list(self.notes),
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Environment:
        """Rebuild from the serialized form."""
        return cls(
            session_type=data.get("session_type", ""),
            compositor=data.get("compositor", ""),
            desktop=data.get("desktop", ""),
            display=data.get("display", ""),
            screens=[tuple(s) for s in data.get("screens", [])],
            capture_backend=data.get("capture_backend", ""),
            capabilities=list(data.get("capabilities", [])),
            pyguitest_version=data.get("pyguitest_version", ""),
            recorder_version=data.get("recorder_version", ""),
            xwayland=data.get("xwayland", False),
            notes=list(data.get("notes", [])),
            recorded_at=data.get("recorded_at", ""),
        )


@dataclass
class Recording:
    """An ordered event list plus the environment it came from.

    Raw capture events are kept only when the user asked for them: they are
    the highest-fidelity diagnostic available and also the most sensitive
    thing the recorder can hold, since an unfiltered X11 stream includes every
    keystroke typed into every application, not only the target.
    """

    events: list[Event] = field(default_factory=list)
    environment: Environment = field(default_factory=Environment)
    raw: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def add(self, event: Event) -> Event:
        """Append an event, filling in its delay from the previous one."""
        if self.events and not event.delay:
            event.delay = max(0.0, event.timestamp - self.events[-1].timestamp)
        self.events.append(event)
        return event

    def to_dict(self) -> dict[str, Any]:
        """Serialize the whole recording."""
        return {
            "format": FORMAT_VERSION,
            "started_at": self.started_at,
            "environment": self.environment.to_dict(),
            "events": [e.to_dict() for e in self.events],
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Recording:
        """Rebuild a recording, rejecting a format or shape this version can't read.

        `--regenerate` invites hand-editing a saved recording, so a
        malformed file is an expected failure mode, not a crash: this raises
        `ValueError` throughout (as `main()` already catches and reports)
        rather than letting a missing key or wrong type surface as a raw
        `KeyError`/`AttributeError`/`TypeError` from wherever the code
        happened to touch it first.
        """
        if not isinstance(data, dict):
            raise ValueError(
                "recording is not a JSON object at the top level "
                f"(got {type(data).__name__})"
            )
        version = data.get("format", 0)
        if version > FORMAT_VERSION:
            raise ValueError(
                f"recording format {version} is newer than this recorder "
                f"understands ({FORMAT_VERSION}); upgrade pyguitest-recorder"
            )
        raw_events = data.get("events", [])
        if not isinstance(raw_events, list):
            raise ValueError(
                f"'events' is not a list (got {type(raw_events).__name__})"
            )
        events = []
        for index, entry in enumerate(raw_events):
            try:
                events.append(event_from_dict(entry))
            except ValueError as exc:
                raise ValueError(f"event #{index}: {exc}") from exc
        return cls(
            events=events,
            environment=Environment.from_dict(data.get("environment", {})),
            raw=list(data.get("raw", [])),
            started_at=data.get("started_at", 0.0),
        )

    def save(self, path: str | Path) -> Path:
        """Write the recording to `path` as JSON, returning the path written.

        Written to a temp file in the same directory first, then moved into
        place with `os.replace` -- a rename is atomic on the same filesystem,
        so a crash or interrupt mid-write leaves either the old file or
        nothing at `path`, never a truncated, unparsable one. A session file
        can be the only record of a recording that took real effort to make
        (or, per `Recording`'s own docstring, credential-bearing), so leaving
        a half-written one behind is worse than the extra temp file.
        """
        target = Path(path)
        payload = json.dumps(self.to_dict(), indent=2)
        descriptor, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as tmp:
                tmp.write(payload)
            os.replace(tmp_name, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp_name)
            raise
        return target

    @classmethod
    def load(cls, path: str | Path) -> Recording:
        """Read a recording back from `path`."""
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
