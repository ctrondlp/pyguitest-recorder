"""Layout-aware key decoding through libxkbcommon (ctypes).

Runs wherever `libxkbcommon.so.0` loads -- a core Linux desktop library -- and
skips where it does not (CI without it, non-Linux). The German cases are the
point: a fixed US table gets them wrong, and this is what makes a non-US
keyboard record the characters actually typed.
"""

from __future__ import annotations

import pytest

from pyguitest_recorder.backends._xkb import XkbTranslator

# evdev key codes (xkb keycode = these + 8), named to keep the tests readable.
KEY_A, KEY_2, KEY_Y, KEY_Z = 30, 3, 21, 44
KEY_LEFTSHIFT, KEY_SEMICOLON, KEY_ENTER, KEY_TAB = 42, 39, 28, 15

us = pytest.mark.skipif(
    XkbTranslator.create("us") is None, reason="libxkbcommon.so.0 not loadable"
)


@us
class TestUsLayout:
    def setup_method(self):
        self.x = XkbTranslator.create("us")

    def teardown_method(self):
        self.x.close()

    def test_letter_is_itself(self):
        assert self.x.key(KEY_A, True) == ("a", "a")

    def test_release_carries_no_text(self):
        self.x.key(KEY_A, True)
        assert self.x.key(KEY_A, False) == ("a", "")

    def test_held_shift_uppercases_the_following_letter(self):
        assert self.x.key(KEY_LEFTSHIFT, True) == ("Shift_L", "")
        assert self.x.key(KEY_A, True) == ("A", "A")
        self.x.key(KEY_A, False)
        self.x.key(KEY_LEFTSHIFT, False)
        assert self.x.key(KEY_A, True) == ("a", "a")  # shift released -> lower

    def test_shift_two_is_at(self):
        self.x.key(KEY_LEFTSHIFT, True)
        assert self.x.key(KEY_2, True) == ("at", "@")

    def test_a_named_key_has_no_typed_text(self):
        assert self.x.key(KEY_ENTER, True) == ("Return", "")
        assert self.x.key(KEY_TAB, True) == ("Tab", "")


@pytest.mark.skipif(
    XkbTranslator.create("de") is None, reason="no libxkbcommon / no 'de' layout data"
)
class TestGermanLayout:
    def setup_method(self):
        self.x = XkbTranslator.create("de")

    def teardown_method(self):
        self.x.close()

    def test_qwertz_swaps_y_and_z(self):
        # The whole reason a fixed US table is wrong: these two keys trade.
        assert self.x.key(KEY_Y, True)[1] == "z"
        assert self.x.key(KEY_Z, True)[1] == "y"

    def test_us_semicolon_key_is_o_umlaut(self):
        keysym, text = self.x.key(KEY_SEMICOLON, True)
        assert text == "ö"
        assert keysym == "odiaeresis"


def test_create_returns_none_for_a_nonexistent_layout():
    # A keymap that cannot be built is a clean fallback, not a crash.
    assert XkbTranslator.create("this-is-not-a-layout") is None
