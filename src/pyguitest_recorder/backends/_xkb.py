"""Decode evdev key codes under the session's real keyboard layout.

The evdev backend reads kernel key *codes*, which mean a character only under
a layout: code 44 is `z` on a US keyboard and `y` on a German one, and `;` on
US is `ö` on German. `_evdev_keysyms`'s fixed US table is wrong for every
other layout, so where libxkbcommon is present this translates through it
instead -- the same library every Wayland compositor already uses, which
produces both the X keysym name the pipeline wants (`Return`, `odiaeresis`)
and the UTF-8 character the normalizer coalesces.

Bound through `ctypes` against `libxkbcommon.so.0`, not the `xkbcommon` PyPI
package: the library is a core part of every Linux desktop, while the package
is a cffi build that needs the `-devel` headers. So this adds a capability
where the runtime library exists and silently falls back to the US table
where it does not, with no build-time dependency either way.

The layout comes from libxkbcommon's own `XKB_DEFAULT_LAYOUT`/`_VARIANT`/
`_MODEL`/`_OPTIONS` resolution (a NULL rule-names set), which is what a
Wayland session exports; reading the compositor's active keymap over
`wl_keyboard` is a later refinement. `xkb_state` is stateful and single
-threaded -- only the backend's reader thread touches it.
"""

from __future__ import annotations

import ctypes
from typing import Any

__all__ = ["XkbTranslator"]

_XKB_KEY_UP = 0
_XKB_KEY_DOWN = 1
_EVDEV_OFFSET = 8  # xkb keycode = evdev code + 8


class _RuleNames(ctypes.Structure):
    _fields_ = [
        ("rules", ctypes.c_char_p),
        ("model", ctypes.c_char_p),
        ("layout", ctypes.c_char_p),
        ("variant", ctypes.c_char_p),
        ("options", ctypes.c_char_p),
    ]


def _bind() -> Any:
    """Load libxkbcommon and declare the functions used, or raise OSError."""
    lib = ctypes.CDLL("libxkbcommon.so.0")
    lib.xkb_context_new.restype = ctypes.c_void_p
    lib.xkb_context_new.argtypes = [ctypes.c_int]
    lib.xkb_keymap_new_from_names.restype = ctypes.c_void_p
    lib.xkb_keymap_new_from_names.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.xkb_state_new.restype = ctypes.c_void_p
    lib.xkb_state_new.argtypes = [ctypes.c_void_p]
    lib.xkb_state_update_key.restype = ctypes.c_int
    lib.xkb_state_update_key.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int]
    lib.xkb_state_key_get_one_sym.restype = ctypes.c_uint32
    lib.xkb_state_key_get_one_sym.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.xkb_keysym_get_name.restype = ctypes.c_int
    lib.xkb_keysym_get_name.argtypes = [
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    lib.xkb_state_key_get_utf8.restype = ctypes.c_int
    lib.xkb_state_key_get_utf8.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    for unref in ("xkb_state_unref", "xkb_keymap_unref", "xkb_context_unref"):
        getattr(lib, unref).argtypes = [ctypes.c_void_p]
    return lib


class XkbTranslator:
    """Translate (evdev code, pressed) to (X keysym name, typed text)."""

    def __init__(self, layout: str | None = None) -> None:
        """Build a keymap from `layout`, or from the session's XKB env if None.

        Raises OSError if libxkbcommon is absent and ValueError if the keymap
        cannot be built, so `create` can turn either into a clean fallback.
        """
        self._lib = _bind()
        self._ctx = self._lib.xkb_context_new(0)
        if not self._ctx:
            raise ValueError("xkb_context_new failed")
        names = None
        if layout is not None:
            self._names = _RuleNames(None, None, layout.encode(), None, None)
            names = ctypes.byref(self._names)
        self._keymap = self._lib.xkb_keymap_new_from_names(self._ctx, names, 0)
        if not self._keymap:
            raise ValueError("xkb_keymap_new_from_names failed")
        self._state = self._lib.xkb_state_new(self._keymap)
        if not self._state:
            raise ValueError("xkb_state_new failed")

    @classmethod
    def create(cls, layout: str | None = None) -> XkbTranslator | None:
        """A translator, or None where libxkbcommon or the keymap is unavailable."""
        try:
            return cls(layout)
        except (OSError, ValueError):
            return None

    def key(self, evdev_code: int, pressed: bool) -> tuple[str, str]:
        """Return (keysym name, text) for a key event, tracking modifiers.

        Queried against the current modifier state and only then does the
        state advance, so a letter pressed while Shift is held reads shifted.
        A release carries no text. Modifier keys pass through like any other
        (their keysym is `Shift_L` etc.) so the caller still sees them.
        """
        kc = evdev_code + _EVDEV_OFFSET
        sym = self._lib.xkb_state_key_get_one_sym(self._state, kc)
        if sym == 0:
            # NoSymbol: the active keymap has nothing on this code (a key the
            # layout does not map, a vendor extra). The US table's name -- an
            # honest evdev-derived one -- beats emitting "0x0".
            from ._evdev_keysyms import keysym_and_text  # noqa: PLC0415

            keysym, _ = keysym_and_text(evdev_code, False)
            self._lib.xkb_state_update_key(
                self._state, kc, _XKB_KEY_DOWN if pressed else _XKB_KEY_UP
            )
            return keysym, ""
        name_buf = ctypes.create_string_buffer(64)
        self._lib.xkb_keysym_get_name(sym, name_buf, 64)
        keysym = name_buf.value.decode("ascii", "replace") or f"0x{sym:x}"
        text = ""
        if pressed:
            text_buf = ctypes.create_string_buffer(16)
            n = self._lib.xkb_state_key_get_utf8(self._state, kc, text_buf, 16)
            if n > 0:
                text = text_buf.value.decode("utf-8", "replace")
        self._lib.xkb_state_update_key(
            self._state, kc, _XKB_KEY_DOWN if pressed else _XKB_KEY_UP
        )
        # A control key's "text" is a control character, not something typed;
        # the normalizer wants printable text only, so drop C0/C1 and DEL.
        if text and (len(text) == 1 and (ord(text) < 0x20 or ord(text) == 0x7F)):
            text = ""
        return keysym, text

    def close(self) -> None:
        """Release the keymap, state and context."""
        for ptr, unref in (
            (self._state, "xkb_state_unref"),
            (self._keymap, "xkb_keymap_unref"),
            (self._ctx, "xkb_context_unref"),
        ):
            if ptr:
                getattr(self._lib, unref)(ptr)
        self._state = self._keymap = self._ctx = None
