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
        """Insert an event in timestamp order, filling in its delay.

        Ordered rather than appended, because "the hover began first and must
        be emitted first" is the normalizer's own stated rule and it cannot
        always keep it on its own. A hover is deliberately not ended by
        keyboard input -- the pointer resting while someone types is not
        someone hovering something -- so its duration is only known once
        something else closes it, which can be after the typing has already
        been emitted. `Normalizer.flush()` then hands it over last, carrying
        the timestamp it actually began at.

        Left appended, that put a hover two seconds *before* a run of typing
        after it in the event list, `max(0.0, ...)` clamped its negative delay
        to zero, and the generated script moved the pointer only after the
        typing it preceded -- so the wait that gave the window time to appear
        landed after the text that needed it. Measured on a real Windows
        recording: `click OK` launched a console and the script typed into it
        on the next line with nothing in between.

        Almost always an append, since events arrive in order; the search
        walks back only for the rare one that does not, and the delays of both
        it and the event it displaces are recomputed from the neighbour each
        actually ends up with.

        Recomputed rather than filled in where empty, for the inserted event
        too. A delay is the interval to the event in front of it, so one that
        arrives carrying a delay of its own -- a saved event re-added, an
        editor moving an event between neighbours -- was measured against
        whichever predecessor it had *then*, and keeping it would put a gap in
        front of an event nothing in the recording actually waited for.
        """
        index = len(self.events)
        while index and self.events[index - 1].timestamp > event.timestamp:
            index -= 1
        self.events.insert(index, event)
        self._fill_delay(index, force=True)
        if index + 1 < len(self.events):
            # Its predecessor changed, so whatever it was told before is no
            # longer the gap in front of it.
            self._fill_delay(index + 1, force=True)
        return event

    def _fill_delay(self, index: int, force: bool = False) -> None:
        """Set one event's delay from the event now in front of it."""
        event = self.events[index]
        if index == 0:
            if force:
                event.delay = 0.0
            return
        if force or not event.delay:
            event.delay = max(0.0, event.timestamp - self.events[index - 1].timestamp)

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
        # Stable, so events sharing a timestamp keep the order they were
        # written in. A recording saved before `add` kept this invariant can
        # hold an event out of order -- a hover flushed last while carrying
        # the timestamp it began at -- and `--regenerate` is exactly where
        # someone goes to get a better script out of a recording they already
        # have. Sorting here repairs that file rather than reproducing it, and
        # costs nothing for a recording that was already in order.
        events.sort(key=lambda event: event.timestamp)
        recording = cls(
            events=events,
            environment=Environment.from_dict(data.get("environment", {})),
            raw=list(data.get("raw", [])),
            started_at=data.get("started_at", 0.0),
        )
        # Every delay is recomputed from the neighbour the sort above just gave
        # it. A delay is the interval to the event in front of it, so an event
        # the sort moved was left holding a gap measured against whichever
        # predecessor it had when the file was written -- visible in a
        # `recorded`/`verbatim` script as a pause in front of a line nothing
        # waited for. The first event's is zero, which is what a file that was
        # never out of order already had.
        for index in range(len(recording.events)):
            recording._fill_delay(index, force=True)
        return recording

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
