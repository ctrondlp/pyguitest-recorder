"""The capture interface, and the raw event every backend produces.

Capture is the one thing pyguitest does not do. pyguitest injects input;
`Capability.INPUT_STATE_QUERY` -- reading global keyboard or pointer state --
sits in its tier 6, "deliberately prevented", described there as what a
keylogger reads. That is not an oversight to work around: no ordinary Wayland
client can observe another client's input, on any compositor, and no backend
added later will change it.

X11 is the exception, which is why it is the first backend and why the
recorder is honest about being X11-only. `RawEvent` is deliberately free of
X11 vocabulary anyway, so an acquisition layer that is not XRecord -- AT-SPI
events being the plausible one, since those behave identically under Wayland
-- can feed the same normalizer.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

__all__ = ["RawEvent", "RawKind", "CaptureBackend", "CaptureUnavailable"]

RawKind = Literal[
    "motion",
    "button_press",
    "button_release",
    "scroll",
    "key_press",
    "key_release",
]


class CaptureUnavailable(Exception):
    """This machine cannot capture input the way this backend needs to."""


@dataclass(frozen=True)
class RawEvent:
    """One observed input event, before any interpretation.

    `keysym` is an X keysym name such as `Return` or `Control_L`, which is
    also the vocabulary `Session.press_key` accepts, so key names survive the
    whole pipeline unchanged. `text` is the character the key produced when it
    produced one, which is what the normalizer coalesces into typed strings.
    """

    kind: RawKind
    timestamp: float
    x: int = 0
    y: int = 0
    screen: int = 0
    button: int = 0
    dx: int = 0
    dy: int = 0
    keysym: str = ""
    text: str = ""

    def to_dict(self) -> dict[str, object]:
        """Serialize for the raw diagnostic log."""
        return {
            "kind": self.kind,
            "timestamp": round(self.timestamp, 6),
            "x": self.x,
            "y": self.y,
            "screen": self.screen,
            "button": self.button,
            "dx": self.dx,
            "dy": self.dy,
            "keysym": self.keysym,
            "text": self.text,
        }


@runtime_checkable
class CaptureBackend(Protocol):
    """A source of raw input events."""

    name: str

    def start(self) -> None:
        """Begin capturing. Raises CaptureUnavailable if it cannot."""

    def events(self) -> Iterator[RawEvent]:
        """Yield raw events until `stop` is called."""

    def drain(self) -> Iterator[RawEvent]:
        """Yield what has already been captured, without waiting for more.

        `events` blocks, which is right for the recording loop and wrong at the
        end of it: a run that was interrupted has to be able to keep whatever
        arrived before the interrupt, and waiting for input that is not coming
        would throw it away instead. A backend with nothing buffered yields
        nothing and returns immediately.
        """

    @property
    def stop_pressed_at(self) -> float | None:
        """When the stop key completed, on the capture clock, or None.

        A backend that recognises the stop chord as it captures it sets this and
        ends the stream there, so a recording ends where the press was made
        rather than where the consumer finally reached it. That distinction is
        the whole of the note a lagging recording carries: a run that ended at
        the press has everything before it, and one interrupted some other way
        may not. A backend that does not recognise the chord leaves this None
        and the consumer's own recogniser ends the recording.
        """

    def stop(self) -> None:
        """Stop capturing and release the display connection."""
