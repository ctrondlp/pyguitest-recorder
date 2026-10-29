"""Translate Linux evdev key codes to the X keysym vocabulary the pipeline uses.

Every other capture backend already speaks X keysym names (`Return`,
`Control_L`, `a`) -- XRecord natively, win32 and macOS by their own tables --
because that is what survives unchanged to `Session.press_key` at replay. The
evdev backend reads kernel key codes instead, so it needs the same translation
its siblings do.

This is a fixed US-layout table, which is the known limitation it shares with
pyguitest's own uinput *replay* path (its `KeymapWarning`): a key code only
means a character under an assumed layout, and real layout resolution would
need xkbcommon. Codes absent from the table fall back to their evdev name as
the keysym and empty text, so an unmapped key is recorded honestly rather than
dropped.
"""

from __future__ import annotations

from evdev import ecodes as _e

__all__ = ["keysym_and_text"]

# evdev code -> (X keysym name, unshifted char, shifted char). "" char = a
# non-text key (Return, arrows, modifiers): keysym only, no typed character.
_KEYSYMS: dict[int, tuple[str, str, str]] = {
    # letters
    **{
        getattr(_e, f"KEY_{c.upper()}"): (c, c, c.upper())
        for c in "abcdefghijklmnopqrstuvwxyz"
    },
    # number row
    _e.KEY_1: ("1", "1", "!"),
    _e.KEY_2: ("2", "2", "@"),
    _e.KEY_3: ("3", "3", "#"),
    _e.KEY_4: ("4", "4", "$"),
    _e.KEY_5: ("5", "5", "%"),
    _e.KEY_6: ("6", "6", "^"),
    _e.KEY_7: ("7", "7", "&"),
    _e.KEY_8: ("8", "8", "*"),
    _e.KEY_9: ("9", "9", "("),
    _e.KEY_0: ("0", "0", ")"),
    # punctuation
    _e.KEY_MINUS: ("minus", "-", "_"),
    _e.KEY_EQUAL: ("equal", "=", "+"),
    _e.KEY_LEFTBRACE: ("bracketleft", "[", "{"),
    _e.KEY_RIGHTBRACE: ("bracketright", "]", "}"),
    _e.KEY_BACKSLASH: ("backslash", "\\", "|"),
    _e.KEY_SEMICOLON: ("semicolon", ";", ":"),
    _e.KEY_APOSTROPHE: ("apostrophe", "'", '"'),
    _e.KEY_GRAVE: ("grave", "`", "~"),
    _e.KEY_COMMA: ("comma", ",", "<"),
    _e.KEY_DOT: ("period", ".", ">"),
    _e.KEY_SLASH: ("slash", "/", "?"),
    _e.KEY_SPACE: ("space", " ", " "),
    # whitespace / editing -- keysym only
    _e.KEY_ENTER: ("Return", "", ""),
    _e.KEY_KPENTER: ("Return", "", ""),
    _e.KEY_TAB: ("Tab", "", ""),
    _e.KEY_BACKSPACE: ("BackSpace", "", ""),
    _e.KEY_DELETE: ("Delete", "", ""),
    _e.KEY_ESC: ("Escape", "", ""),
    _e.KEY_INSERT: ("Insert", "", ""),
    # navigation
    _e.KEY_LEFT: ("Left", "", ""),
    _e.KEY_RIGHT: ("Right", "", ""),
    _e.KEY_UP: ("Up", "", ""),
    _e.KEY_DOWN: ("Down", "", ""),
    _e.KEY_HOME: ("Home", "", ""),
    _e.KEY_END: ("End", "", ""),
    _e.KEY_PAGEUP: ("Prior", "", ""),
    _e.KEY_PAGEDOWN: ("Next", "", ""),
    # modifiers
    _e.KEY_LEFTCTRL: ("Control_L", "", ""),
    _e.KEY_RIGHTCTRL: ("Control_R", "", ""),
    _e.KEY_LEFTSHIFT: ("Shift_L", "", ""),
    _e.KEY_RIGHTSHIFT: ("Shift_R", "", ""),
    _e.KEY_LEFTALT: ("Alt_L", "", ""),
    _e.KEY_RIGHTALT: ("Alt_R", "", ""),
    _e.KEY_LEFTMETA: ("Super_L", "", ""),
    _e.KEY_RIGHTMETA: ("Super_R", "", ""),
    _e.KEY_CAPSLOCK: ("Caps_Lock", "", ""),
    # function keys
    **{getattr(_e, f"KEY_F{n}"): (f"F{n}", "", "") for n in range(1, 13)},
}


def keysym_and_text(code: int, shifted: bool) -> tuple[str, str]:
    """Return (X keysym name, typed character) for an evdev key code.

    `shifted` selects the shifted character and has no effect on the keysym
    name. An unmapped code yields its evdev key name and no text.
    """
    entry = _KEYSYMS.get(code)
    if entry is None:
        names = _e.keys.get(code)
        if isinstance(names, (list, tuple)):
            names = names[0]
        return ((names or f"code_{code}").removeprefix("KEY_"), "")
    keysym, lo, hi = entry
    return keysym, (hi if shifted else lo)
