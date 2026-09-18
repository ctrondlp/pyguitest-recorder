"""The stop key, recognised the same way wherever the stream is read.

A recording is ended by a chord pressed in the application being recorded --
never by the terminal, which is the point: a recorder that can only be stopped
from a shell you cannot reach is a recorder you cannot stop while driving
another application full screen, which is most of the time.

That makes recognising the chord a property of the *input stream*, not of the
loop consuming it, and two places need the answer:

- The consumer, which decides what belongs in the recording. A run of presses
  that completes is the recorder's and is swallowed; presses that do not are
  the application's and are recorded like any other key.
- The capture backend, which sees the presses as they arrive -- ahead of the
  backlog in front of them -- and has to be able to end the stream there. A
  recorder that fell behind live input used to answer the stop key only once it
  had worked through everything done *before* the press, and while input
  outpaced consumption that moment never arrived at all: the run could not be
  stopped, and a hundred presses sat in the queue unread.

One implementation, so the two cannot disagree about what stops a recording.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .analyzer import MODIFIERS, chord_matches, parse_chord
from .backends.base import RawEvent
from .config import Settings

__all__ = ["Outcome", "StopKey"]


def _presses(events: list[RawEvent]) -> int:
    """How many of these are presses rather than releases."""
    return sum(1 for event in events if event.kind == "key_press")


@dataclass(frozen=True)
class Outcome:
    """What one raw event turned out to be, for whoever asked.

    Three answers, because the two callers want different ones. `record` is
    what belongs in the recording, in order. `stop` says the run completed on
    this event, so the recording ends here. `registered` says a press was
    counted towards a run without completing it -- the moment somebody pressing
    the key is waiting to hear about, and never true of the press that stops.
    """

    record: list[RawEvent]
    stop: bool = False
    registered: bool = False


@dataclass
class StopKey:
    """The stop chord, as it appears in a stream of raw events.

    Feed every event in order and read the `Outcome`: the events that belong in
    the recording, whether the run just completed, and whether a press merely
    registered. Nothing is thrown away by `feed` -- deciding what a run of
    presses *means* happens here, and what to do about it belongs to the caller.
    The consumer records what it is handed back and ends the recording when told
    to; the backend ends the stream there. Anything still held when a run ends
    some other way is handed back by `release`.
    """

    chord: str = "Escape"
    """The key, or chord, that ends a recording -- `ctrl+Escape` and the like."""

    presses: int = 2
    """How many times it must be pressed in a row. Two by default, because a
    single Escape belongs to the application being recorded. A 0 or a negative
    is read as one: a stop key that can never be pressed enough to stop is not
    a setting anybody wants."""

    interval: float = 2.0
    """Seconds within which those presses must arrive to be one run."""

    passed: int = field(default=0, init=False)
    """Presses handed back rather than ending the run.

    Worth telling the user about afterwards: a single Escape pressed with no
    second press behind it becomes a keystroke in their script.
    """

    _held: list[RawEvent] = field(default_factory=list, init=False)
    """Presses collected so far for a run that may still complete."""

    _mods: set[str] = field(default_factory=set, init=False)
    """Modifiers currently down, tracked from the stream itself.

    The stop key is read before normalization, so its modifier state cannot
    come from the normalizer -- and `ctrl+Escape` has to be told apart from a
    bare Escape by exactly this.
    """

    @classmethod
    def from_settings(cls, settings: Settings) -> StopKey:
        """Build the recogniser one recording's settings ask for."""
        return cls(
            chord=settings.stop_key,
            presses=settings.stop_key_presses,
            interval=settings.stop_key_interval,
        )

    @property
    def pending_presses(self) -> int:
        """How many presses of the current run are being held."""
        return _presses(self._held)

    def feed(self, raw: RawEvent) -> Outcome:
        """Sort one raw event in, and say what it turned out to be.

        The three endings the stream has. An event that is not part of a run
        ends any run in progress -- handing its presses back to be recorded,
        since a press that never found a second is the application's -- and is
        recorded itself. A press that arrives too long after the last one is not
        part of that run either, and starts one of its own. And a press that
        belongs to the run is held, until either the run completes or something
        else ends it.
        """
        mods = self._modifiers_after(raw)
        if not self._belongs(raw, mods):
            return Outcome([*self._hand_back(), raw])
        if self._held and raw.timestamp - self._held[-1].timestamp > self.interval:
            # Too slow to be one run, so the earlier presses were the
            # application's and this one starts a new run of its own.
            record = self._hand_back()
            self._held = [raw]
            return Outcome(record, registered=raw.kind == "key_press")
        self._held.append(raw)
        if self.pending_presses >= max(1, self.presses):
            self._held = []
            return Outcome([], stop=True)
        return Outcome([], registered=raw.kind == "key_press")

    def release(self) -> list[RawEvent]:
        """Hand back whatever is held, for a run that ended without completing.

        Presses held for a run that never completed are the application's, not
        the recorder's -- a single Escape meant to close a dialog -- so they
        belong in the recording, in order, ahead of whatever ended the run. The
        same reason `feed` hands them back: these are keystrokes, not a stop,
        and saying so afterwards is the point of counting them.
        """
        return self._hand_back()

    def _hand_back(self) -> list[RawEvent]:
        """End the run in progress, counting what it was holding.

        A press that never found its second is the application's, so it is
        handed back to be recorded -- and counted, because the count is what
        tells the user afterwards that a press of theirs went into the script as
        a keystroke. A completed run hands back nothing: those presses are the
        stop, and are the recorder's.
        """
        spent, self._held = self._held, []
        self.passed += _presses(spent)
        return spent

    def _modifiers_after(self, raw: RawEvent) -> set[str]:
        """Track which modifiers are down, which the normalizer cannot do here."""
        modifier = MODIFIERS.get(getattr(raw, "keysym", "") or "")
        if modifier is not None:
            if raw.kind == "key_press":
                self._mods.add(modifier)
            elif raw.kind == "key_release":
                self._mods.discard(modifier)
        return self._mods

    def _belongs(self, raw: RawEvent, mods: set[str]) -> bool:
        """Whether this event belongs to a run of stop-key presses.

        Releases count as belonging to the run without advancing it, so the
        release between two presses does not read as the run being broken.
        """
        if raw.kind not in ("key_press", "key_release"):
            return False
        if raw.kind == "key_release":
            _, key = parse_chord(self.chord)
            return bool(key) and raw.keysym == key and bool(self._held)
        return chord_matches(self.chord, mods, raw.keysym)
