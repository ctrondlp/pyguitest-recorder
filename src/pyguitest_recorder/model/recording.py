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


def _text(data: dict[str, Any], key: str) -> str:
    """One string field of a serialized object, or `ValueError` naming it."""
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"'{key}' is not a string (got {type(value).__name__})")
    return value


def _list(data: dict[str, Any], key: str) -> list[Any]:
    """One list field of a serialized object, or `ValueError` naming it.

    Deliberately not `list(data.get(key, []))`: a string is iterable, so a
    hand-edited `"capabilities": "x"` would come back as one capability per
    character instead of as a refusal.
    """
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"'{key}' is not a list (got {type(value).__name__})")
    return value


def _flag(data: dict[str, Any], key: str) -> bool:
    """One boolean field of a serialized object, or `ValueError` naming it."""
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"'{key}' is not true or false (got {type(value).__name__})")
    return value


def _number(data: dict[str, Any], key: str) -> float:
    """One numeric field of a serialized object, or `ValueError` naming it.

    `bool` is refused by name rather than by type: it is an `int` subclass, so
    `"started_at": true` would otherwise be read as the number 1. Finiteness is
    not checked here, unlike an event's own timestamp -- nothing orders or
    subtracts a start time, so a NaN in this field changes no outcome.
    """
    value = data.get(key, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{key}' is not a number (got {type(value).__name__})")
    return float(value)


def _screen(entry: Any) -> tuple[int, int, int, float]:
    """One `screens` entry as (index, width, height, scale), or `ValueError`."""
    if not isinstance(entry, (list, tuple)) or len(entry) != 4:
        raise ValueError(
            f"a 'screens' entry is not [index, width, height, scale] (got {entry!r})"
        )
    try:
        return (int(entry[0]), int(entry[1]), int(entry[2]), float(entry[3]))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"a 'screens' entry is not four numbers (got {entry!r})"
        ) from exc


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
        """Rebuild from the serialized form, or refuse a shape it can't read.

        `Recording.from_dict` promises that a malformed file raises
        `ValueError`, and `--regenerate` invites the hand-editing that
        produces one -- so every field is read through a check that names it,
        rather than left to fail as an `AttributeError` or a `TypeError` from
        wherever the value is first used.
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"'environment' is not an object (got {type(data).__name__})"
            )
        return cls(
            session_type=_text(data, "session_type"),
            compositor=_text(data, "compositor"),
            desktop=_text(data, "desktop"),
            display=_text(data, "display"),
            screens=[_screen(entry) for entry in _list(data, "screens")],
            capture_backend=_text(data, "capture_backend"),
            capabilities=list(_list(data, "capabilities")),
            pyguitest_version=_text(data, "pyguitest_version"),
            recorder_version=_text(data, "recorder_version"),
            xwayland=_flag(data, "xwayland"),
            notes=list(_list(data, "notes")),
            recorded_at=_text(data, "recorded_at"),
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
            raw=_list(data, "raw"),
            started_at=_number(data, "started_at"),
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
