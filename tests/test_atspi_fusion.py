"""Deterministic tests for input<->AT-SPI target fusion.

Synthetic `RawEvent`-shaped input records and AT-SPI effect dicts stamped on
one shared synthetic clock (both streams are `time.monotonic` in production),
so the correlation and modifier-tracking logic
is exercised exactly, with no display, no gi and no timing flakiness.
"""

from __future__ import annotations

from pyguitest_recorder.analyzer.atspi_fusion import fuse_targets


def ev(kind, t, **kw):
    return {"kind": kind, "timestamp": 1000.0 + t, **kw}


def key(t, keysym, text="", press=True):
    return ev("key_press" if press else "key_release", t, keysym=keysym, text=text)


def effect(etype, t, name=None, role=None):
    return {"type": etype, "wall": 1000.0 + t, "name": name, "role": role}


class TestClick:
    def test_click_resolves_target_from_following_focus_effect(self):
        inp = [ev("button_press", 0.1, button=1), ev("button_release", 0.15, button=1)]
        eff = [effect("object:state-changed:focused", 0.2, "Save", "push button")]
        (act,) = fuse_targets(inp, eff)
        assert act["action"] == "click"
        assert act["target"] == {"name": "Save", "role": "push button"}
        assert act["confidence"] == "high"

    def test_click_with_no_effect_is_low_confidence(self):
        inp = [ev("button_press", 0.1, button=1), ev("button_release", 0.15, button=1)]
        (act,) = fuse_targets(inp, [])
        assert act["confidence"] == "low"
        assert act["target"] is None

    def test_effect_outside_window_does_not_bind(self):
        inp = [ev("button_press", 0.1, button=1), ev("button_release", 0.15, button=1)]
        eff = [effect("object:state-changed:focused", 5.0, "Later", "push button")]
        assert fuse_targets(inp, eff)[0]["confidence"] == "low"

    def test_two_clicks_bind_their_own_effects(self):
        inp = [
            ev("button_press", 0.1, button=1),
            ev("button_release", 0.15, button=1),
            ev("button_press", 1.1, button=1),
            ev("button_release", 1.15, button=1),
        ]
        eff = [
            effect("object:state-changed:focused", 0.2, "First", "push button"),
            effect("object:state-changed:focused", 1.2, "Second", "check box"),
        ]
        acts = fuse_targets(inp, eff)
        assert [a["target"]["name"] for a in acts] == ["First", "Second"]

    def test_a_modifier_released_mid_press_does_not_stick(self):
        # The press scan used to skip ahead to the matching release, swallowing
        # the Control key_release in between -- `held` kept Control forever and
        # every later keystroke became a shortcut.
        inp = [
            key(0.0, "Control_L"),
            ev("button_press", 0.1, button=1),
            key(0.2, "Control_L", press=False),
            ev("button_release", 0.3, button=1),
            key(0.5, "a", text="a"),
            key(0.55, "a", press=False),
        ]
        acts = fuse_targets(inp, [])
        assert acts[0]["action"] == "click"
        assert acts[-1]["action"] == "type"
        assert acts[-1]["text"] == "a"
        assert all(a["action"] != "shortcut" for a in acts)


class TestText:
    def test_text_run_coalesces_and_resolves_target(self):
        inp = [key(0.1, "h", "h"), key(0.2, "i", "i")]
        eff = [effect("object:text-changed:insert", 0.25, "Full Name", "text")]
        (act,) = fuse_targets(inp, eff)
        assert act == {
            "action": "type",
            "text": "hi",
            "target": {"name": "Full Name", "role": "text"},
            "confidence": "high",
            "wall": 1000.1,
        }

    def test_an_unnamed_effect_source_is_low_confidence(self):
        # An effect whose source published no name resolves to no target;
        # confidence follows the resolved target, not the effect's existence --
        # "high" beside a null target claimed a certainty the fusion lacks.
        inp = [key(0.1, "a", "a")]
        eff = [effect("object:text-changed:insert", 0.2)]
        (act,) = fuse_targets(inp, eff)
        assert act["target"] is None
        assert act["confidence"] == "low"

    def test_shift_modified_char_stays_text_not_shortcut(self):
        # Regression (caught live on KDE): "@" is Shift_L+2 and the backend
        # already put "@" in text, so it must record as text, not a shortcut.
        inp = [
            key(0.1, "n", "n"),
            key(0.2, "Shift_L", ""),
            key(0.3, "2", "@"),
            key(0.35, "Shift_L", "", press=False),
            key(0.4, "t", "t"),
        ]
        acts = fuse_targets(inp, [])
        assert [a["action"] for a in acts] == ["type"]
        assert acts[0]["text"] == "n@t"

    def test_ctrl_breaks_text_and_is_a_shortcut(self):
        inp = [
            key(0.1, "h", "h"),
            key(0.2, "Control_L", ""),
            key(0.3, "s", "s"),
            key(0.35, "Control_L", "", press=False),
            key(0.4, "i", "i"),
        ]
        acts = fuse_targets(inp, [])
        assert [a["action"] for a in acts] == ["type", "shortcut", "type"]
        assert acts[1]["keys"] == "Control_L+s"
        assert acts[0]["text"] == "h"
        assert acts[2]["text"] == "i"


class TestNamedKeysAndScroll:
    def test_named_key_without_text_is_a_key_action(self):
        (act,) = fuse_targets([key(0.1, "Return", "")], [])
        assert act == {"action": "key", "keys": "Return", "wall": 1000.1}

    def test_scroll_passthrough(self):
        (act,) = fuse_targets([ev("scroll", 0.1, dy=1, dx=0)], [])
        assert act == {"action": "scroll", "dx": 0, "dy": 1, "wall": 1000.1}


class TestRealisticSession:
    def test_click_type_save_sequence(self):
        inp = [
            ev("button_press", 0.1, button=1),
            ev("button_release", 0.15, button=1),
            key(0.6, "h", "h"),
            key(0.7, "i", "i"),
            key(1.2, "Control_L", ""),
            key(1.3, "s", "s"),
            key(1.35, "Control_L", "", press=False),
        ]
        eff = [
            effect("object:state-changed:focused", 0.2, "Full Name", "text"),
            effect("object:text-changed:insert", 0.75, "Full Name", "text"),
        ]
        acts = fuse_targets(inp, eff)
        assert [a["action"] for a in acts] == ["click", "type", "shortcut"]
        assert acts[0]["target"]["name"] == "Full Name"
        assert acts[1]["text"] == "hi"
        assert acts[2]["keys"] == "Control_L+s"
