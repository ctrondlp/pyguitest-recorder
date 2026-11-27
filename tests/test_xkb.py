"""Layout-aware key decoding through libxkbcommon (ctypes).

Runs wherever `libxkbcommon.so.0` loads -- a core Linux desktop library -- and
skips where it does not (CI without it, non-Linux). The German cases are the
point: a fixed US table gets them wrong, and this is what makes a non-US
keyboard record the characters actually typed.
"""

from __future__ import annotations

from unittest import mock

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


def test_a_library_too_old_for_a_symbol_is_a_clean_fallback(monkeypatch):
    # Found on Fedora 19, whose libxkbcommon.so.0 exports no
    # `xkb_state_key_get_utf8`. ctypes raises AttributeError for a symbol the
    # object does not export -- which `create` did not catch, so merely asking
    # whether a translator could be built raised through. The module-level
    # `skipif` in this very file does that at import time, so the failure
    # arrived as a collection error that took the whole session down rather
    # than as the documented None.
    import ctypes as _ctypes

    from pyguitest_recorder.backends import _xkb

    class OldLibrary:
        """Exports everything except the one symbol an old build lacks."""

        def __getattr__(self, name):
            if name == "xkb_state_key_get_utf8":
                raise AttributeError(
                    "/lib64/libxkbcommon.so.0: undefined symbol: xkb_state_key_get_utf8"
                )
            return mock.Mock()

    monkeypatch.setattr(_ctypes, "CDLL", lambda name: OldLibrary())

    with pytest.raises(OSError, match="too old"):
        _xkb._bind()
    assert XkbTranslator.create("us") is None


class TestKeysymText:
    """`keysym_text`: libxkbcommon's own table, for the blocks python-xlib skips.

    `Xlib.XK.keysym_to_string` covers Latin-1 plus eight named control keys
    and returns None for everything else -- its own source says the other
    blocks "will have to wait" -- so a keystroke on a Cyrillic, Greek,
    Hebrew, Arabic or Thai layout carried no text at all, was recorded as a
    bare key *name*, and walked straight past redaction, since only a
    `TextInput` carries the sensitive flag.
    """

    def test_a_cyrillic_keysym_answers_its_character(self):
        from pyguitest_recorder.backends._xkb import keysym_text

        if not keysym_text(0x61):  # no libxkbcommon on this box
            pytest.skip("libxkbcommon is absent or has no keysym table")
        assert keysym_text(0x6C6) == "ф"  # Cyrillic_ef

    def test_a_keysym_naming_no_character_is_empty(self):
        from pyguitest_recorder.backends._xkb import keysym_text

        assert keysym_text(0xFFE1) == ""  # Shift_L

    @pytest.mark.parametrize(
        "keysym",
        [
            0xFE50,  # dead_grave, the block's first
            0xFE6F,  # dead_currency, where the diacritics end
            0xFE80,  # dead_a, the universal syllable-entry vowels
            0xFE8C,  # dead_greek, where those end
            0xFE90,  # dead_lowline, the German T3 additions
            0xFE93,  # dead_longsolidusoverlay, where those end
        ],
    )
    def test_every_dead_key_is_left_without_text(self, keysym):
        # X11 delivers `dead_acute` then `e` and the *application* composes
        # the result, so a recording that keeps them as two key events
        # replays correctly against that same composition. Turning a dead key
        # into text would both lose the accent and claim a character the
        # keypress did not produce. The block is not contiguous, which an
        # earlier range missed: it ran to 0xFE7F and left the last twelve
        # keysyms to whatever libxkbcommon says about them.
        from pyguitest_recorder.backends._xkb import keysym_text

        assert keysym_text(keysym) == ""

    def test_a_missing_library_is_empty_rather_than_an_error(self, monkeypatch):
        # Every caller treats "" as "not a character", so nothing has to know
        # whether the library was there.
        from pyguitest_recorder.backends import _xkb

        monkeypatch.setattr(_xkb, "_keysym_to_utf8", None)
        monkeypatch.setattr(
            _xkb.ctypes, "CDLL", mock.Mock(side_effect=OSError("no such library"))
        )
        assert _xkb.keysym_text(0x61) == ""
        assert _xkb._keysym_to_utf8 is False
