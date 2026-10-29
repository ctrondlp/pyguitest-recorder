"""Resolve recorded-action targets by fusing input with AT-SPI effects.

EXPERIMENTAL -- not wired into the recording pipeline, and NOT the production
code path. The shipped Wayland click-naming lives in
`windows/resolver.py::_effect_element_in` + `windows/atspi_effects.py`; this
module is the Phase-0 offline spike those grew out of, kept as reference only.
It is deliberately NOT kept in sync with them (it fuses a different event set
and names targets differently), so change the resolver/effect-log for anything
real -- not this. A Phase-0 spike (2026-10-27, GNOME Wayland) established the
two halves this fuses:

- AT-SPI emits NO input events on Wayland: a passive listener sees focus,
  state (checked/selected), text-changed and structure (dialogs, dropdowns),
  but never the click or keypress that caused them. So AT-SPI alone cannot
  record; it can only say WHICH widget reacted.
- The `wayland` capture backend reads the input below the compositor: WHAT the
  user did and WHEN, but with no idea which widget it hit.

Correlating the two by timestamp turns a raw input timeline into semantic,
element-targeted actions -- the layered record the Wayland plan calls for,
with a confidence rung (high = an AT-SPI target was found, low = coordinates
only). Validated live on KWin (2026-10-27): real clicks and typing fused into
37 targeted actions.

Pure Python by design: it takes already-collected `RawEvent.to_dict()` input
records and AT-SPI effect dicts (`{"type", "wall", "name", "role"}`) and
returns actions, so it is testable with no display and no gi. Collecting the
live AT-SPI effect stream (which needs gi) lives in
scripts/wayland-capture-check.py.

Both streams carry an epoch wall-clock stamp -- the backend's `timestamp`
(evdev reports epoch seconds) and the listener's `wall` -- so they align
directly. Modifiers are tracked from the key stream, not read off each event:
the backend follows XRecord's vocabulary, where a held Control is its own
`key_press`/`key_release` pair, so this mirrors how the normalizer reads them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = ["fuse_targets", "EFFECT_WINDOW", "SHORTCUT_MODS"]

# How long after an input an AT-SPI effect may arrive and still be its effect.
EFFECT_WINDOW = 0.6

# Keysyms that make a following key a shortcut. Shift is NOT one: it is a text
# modifier the backend has already folded into the `text` field (shift+2 is
# "@", not a chord) -- a distinction caught live when a typed email's "@" was
# first misrecorded as shortcut(Shift_L+2).
SHORTCUT_MODS = frozenset(
    {"Control_L", "Control_R", "Alt_L", "Alt_R", "Super_L", "Super_R"}
)

# Modifiers already reflected in `text`, so fusion swallows them silently: they
# neither emit an action of their own nor break a run of typed characters.
TEXT_MODS = frozenset({"Shift_L", "Shift_R", "Caps_Lock"})


def _nearest_effect(
    effects: Sequence[dict[str, Any]],
    t: float,
    kinds: Sequence[str],
    window: float = EFFECT_WINDOW,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for ev in effects:
        when = ev["wall"]
        if (
            t <= when <= t + window
            and any(k in ev["type"] for k in kinds)
            and (best is None or when < best["wall"])
        ):
            best = ev
    return best


def _target_of(effect: dict[str, Any] | None) -> dict[str, Any] | None:
    if not effect:
        return None
    name, role = effect.get("name"), effect.get("role")
    if not name and not role:
        return None
    return {"name": name or None, "role": role or None}


def _coalesce_text(inputs: Sequence[dict[str, Any]], start_i: int) -> tuple[str, int]:
    """Collect one run of typed characters starting at `start_i`.

    Steps over the key releases and shift/caps events that interleave the
    stream; a ctrl/alt/super press or any named/non-key event ends the run.
    Returns the joined text and the index just past the run.
    """
    text: list[str] = []
    i = start_i
    while i < len(inputs):
        x = inputs[i]
        xk = x.get("keysym", "")
        if x["kind"] == "key_release" or xk in TEXT_MODS:
            i += 1
            continue
        if xk in SHORTCUT_MODS:
            break
        if x["kind"] == "key_press" and x.get("text"):
            text.append(x["text"])
            i += 1
            continue
        break
    return "".join(text), i


_CLICK_EFFECTS = (
    "focus",
    "state-changed:focused",
    "state-changed:checked",
    "state-changed:selected",
    "children-changed",
)


def _track_modifier(kind: str, ks: str, held: set[str]) -> bool:
    """Update `held` for a modifier/skippable record; True if it emits nothing.

    Shortcut modifiers (ctrl/alt/super) are tracked, shift/caps swallowed, and
    an ordinary key's release carries nothing -- all of which the caller skips.
    """
    if ks in SHORTCUT_MODS:
        if kind == "key_press":
            held.add(ks)
        elif kind == "key_release":
            held.discard(ks)
        return True
    return ks in TEXT_MODS or kind == "key_release"


def _fuse_click(
    inputs: Sequence[dict[str, Any]], i: int, effects: Sequence[dict[str, Any]]
) -> tuple[dict[str, Any], int]:
    """Build a click action from the press at `i`; skip to past its release."""
    r = inputs[i]
    target = _target_of(_nearest_effect(effects, r["timestamp"], _CLICK_EFFECTS))
    action = {
        "action": "click",
        "button": r.get("button", 1),
        "target": target,
        "confidence": "high" if target else "low",
        "wall": r["timestamp"],
    }
    j = i + 1
    while j < len(inputs) and inputs[j]["kind"] != "button_release":
        j += 1
    return action, (j + 1 if j < len(inputs) else i + 1)


def fuse_targets(
    inputs: Sequence[dict[str, Any]],
    effects: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fuse input records and AT-SPI effects into targeted recorded actions.

    `inputs` are time-ordered `RawEvent.to_dict()` records; `effects` are
    AT-SPI effect dicts. Returns actions: `click` (resolved `target` +
    `confidence`), coalesced `type` runs, `shortcut` for ctrl/alt/super
    chords, standalone `key` for named keys (Return, Tab, arrows), and
    `scroll`.
    """
    actions: list[dict[str, Any]] = []
    held: set[str] = set()  # ctrl/alt/super keysyms currently down
    i = 0
    while i < len(inputs):
        r = inputs[i]
        kind = r["kind"]
        ks = r.get("keysym", "")

        if _track_modifier(kind, ks, held):
            i += 1
        elif kind == "button_press":
            action, i = _fuse_click(inputs, i, effects)
            actions.append(action)
        elif kind == "key_press" and held:
            actions.append(
                {
                    "action": "shortcut",
                    "keys": "+".join([*sorted(held), ks]),
                    "wall": r["timestamp"],
                }
            )
            i += 1
        elif kind == "key_press" and r.get("text"):
            start = r["timestamp"]
            text, i = _coalesce_text(inputs, i)
            effect = _nearest_effect(
                effects, start, ("text-changed", "state-changed:focused")
            )
            actions.append(
                {
                    "action": "type",
                    "text": text,
                    "target": _target_of(effect),
                    "confidence": "high" if effect else "low",
                    "wall": start,
                }
            )
        elif kind == "key_press":  # a named key with no text (Return, Tab, ...)
            actions.append({"action": "key", "keys": ks, "wall": r["timestamp"]})
            i += 1
        elif kind == "scroll":
            actions.append(
                {
                    "action": "scroll",
                    "dx": r.get("dx", 0),
                    "dy": r.get("dy", 0),
                    "wall": r["timestamp"],
                }
            )
            i += 1
        else:
            i += 1
    return actions
