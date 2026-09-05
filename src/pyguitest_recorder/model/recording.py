"""A recording: its events, and the environment they were captured in.

The environment block is not decoration. A generated script that fails is
usually failing because the desktop it runs on differs from the one it was
recorded on, and the first question is always which difference. Recording the
session type, compositor, screen geometry and the capability set that was
available at capture time makes that answerable without reproducing anything.
"""

from __future__ import annotations

import json
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
    display: str = ""
    screens: list[tuple[int, int, int, float]] = field(default_factory=list)
    """(index, width, height, scale) for each screen the session reported."""
    capture_backend: str = ""
    capabilities: list[str] = field(default_factory=list)
    pyguitest_version: str = ""
    recorder_version: str = ""
    xwayland: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "session_type": self.session_type,
            "compositor": self.compositor,
            "display": self.display,
            "screens": [list(s) for s in self.screens],
            "capture_backend": self.capture_backend,
            "capabilities": list(self.capabilities),
            "pyguitest_version": self.pyguitest_version,
            "recorder_version": self.recorder_version,
            "xwayland": self.xwayland,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Environment:
        """Rebuild from the serialized form."""
        return cls(
            session_type=data.get("session_type", ""),
            compositor=data.get("compositor", ""),
            display=data.get("display", ""),
            screens=[tuple(s) for s in data.get("screens", [])],
            capture_backend=data.get("capture_backend", ""),
            capabilities=list(data.get("capabilities", [])),
            pyguitest_version=data.get("pyguitest_version", ""),
            recorder_version=data.get("recorder_version", ""),
            xwayland=data.get("xwayland", False),
            notes=list(data.get("notes", [])),
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
        """Rebuild a recording, rejecting a format this version cannot read."""
        version = data.get("format", 0)
        if version > FORMAT_VERSION:
            raise ValueError(
                f"recording format {version} is newer than this recorder "
                f"understands ({FORMAT_VERSION}); upgrade pyguitest-recorder"
            )
        return cls(
            events=[event_from_dict(e) for e in data.get("events", [])],
            environment=Environment.from_dict(data.get("environment", {})),
            raw=list(data.get("raw", [])),
            started_at=data.get("started_at", 0.0),
        )

    def save(self, path: str | Path) -> Path:
        """Write the recording to `path` as JSON, returning the path written."""
        target = Path(path)
        target.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> Recording:
        """Read a recording back from `path`."""
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
