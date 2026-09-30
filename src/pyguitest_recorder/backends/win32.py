r"""Input capture through Windows' low-level input hooks.

`WH_KEYBOARD_LL` and `WH_MOUSE_LL` are the Windows counterpart of XRecord --
the one interface that lets this process observe input aimed at every other
window on the desktop -- and they come with a cost XRecord does not: **a slow
hook is silently removed, and Windows does not say so.** Windows documents the
rule plainly: the hook procedure must finish inside
`HKEY_CURRENT_USER\Control Panel\Desktop\LowLevelHooksTimeout` (300ms by
default, 1000ms the most any Windows 10 1709+ build will honour even if
configured higher), and on Windows 7 and later a hook that misses that window
is unhooked without so much as a `CallNextHookEx` -- not passed through, not
logged, just gone. The mitigation this backend takes is Microsoft's own
recommendation: the callback does the least possible work -- read the
structure, enqueue it, return -- and everything else (`ToUnicodeEx`, the
queue consumer, the normalizer) runs off the hook's own thread.

**The removal is silent to this process, but it is not invisible to the
session.** `GetLastInputInfo` is the session's own last-input time, maintained
for idle detection whether or not any hook exists, so comparing it with the
last time a callback ran measures exactly the input that went unrecorded --
`_unseen_input_seconds` does that arithmetic and `_watch_health` asks it once a
second. A lost hook is therefore *reported*: `hook_lost_seconds` carries how
much input went unseen, and the recorder states it on the recording
(`recorder._hook_lost_note`) instead of letting a script with a hole in it read
as complete. `HOOK_LOST_SLACK` explains why the number is a floor, and the one
other thing that reads the same way -- input on a desktop these hooks cannot
see, such as a UAC prompt or the lock screen -- is why the note says "may be
missing" rather than naming a cause it cannot prove.

Periodic re-installation is still not attempted, and that is now a *choice*
rather than a limit: whether re-installing restores a silently-removed hook is
not settled by Windows' own documentation, and this backend no longer has to
guess in order to be honest -- the heartbeat above is what a verified
re-installation would be judged by, and until somebody measures it, a recording
says what it lost.

Raw input (`RegisterRawInputDevices`) is Microsoft's own stated preference
over low-level hooks for exactly this use, and it has neither the timeout nor
the silent-removal failure mode. It is not what this module does: it needs a
message-only window (`RegisterClassExW`/`CreateWindowExW`, a `WNDPROC` of its
own) rather than a callback, which is a materially larger surface for a
first implementation to get right -- see docs/developers/adr-003-windows.md
in pyguitest for the sibling decision to keep new Win32 surface small and
reviewable. Low-level hooks are simpler and are what nearly every real-world
tool in this space actually ships. Measured on Windows 11, the callback
(`ToUnicodeEx` included) takes 0.04ms at the median and 3ms at the worst of
20,000 key-downs, against the 300ms limit; the gap is a property of the
mechanism rather than something a live run has shown to happen, and if it does
happen the recording now carries the fact. If it proves common in practice,
raw input is the documented next step, not a surprise.

**The reach sentence this module's honesty depends on**: a low-level hook
sees every keystroke on the machine, in every application, with no permission
asked and no way for the person at the keyboard to know it is happening.
macOS at least has an Input Monitoring entry in System Settings a user can
see and revoke; Windows has nothing -- no prompt, no list entry, no
revocation UI. The only place anyone learns that running this recorder means
"everything typed on this machine is now being written to a script" is this
module's own documentation, `--help`, and the README. That is a documentation
duty this module cannot discharge by itself, and it is named here so nobody
mistakes the silence for something the platform is handling.

Text is computed in the callback, not deferred to normalization time. The
`ToUnicodeEx` call this needs is a fast, synchronous, in-process lookup --
nothing like the cost the timeout rule is written to guard against -- and
computing it here rather than reconstructing it later keeps `analyzer/normalize.py`
completely untouched: every downstream module sees the same `RawEvent` shape
X11 already produces, `raw.text` populated exactly where X11 populates it,
which is what "the same behaviour as Linux" actually requires of this
backend. The one caveat this route inherits from `ToUnicodeEx` itself: a dead
key is not resolved into the character it composes (see `_text_for`), and
what a dead-key sequence records as has not been checked against a layout that
has one -- this machine's is US English. X11's own `_resolve_keysym` does not
model dead keys either, so this is not a regression from what the Linux
backend already does -- both backends draw the same honest line at plain Shift
and CapsLock.

Keys are named the way X11 names them, so nothing downstream needs to know
which backend it is reading: Windows' one real difference, AltGr arriving as a
fake Control plus the right Alt, is folded back into X11's single
`ISO_Level3_Shift` here (see `_ALTGR_FAKE_CONTROL_SCAN`).

Modifier state is tracked from the event stream, the same thing X11's own
`_resolve_keysym` does with its shift and lock masks -- Windows documents
`GetAsyncKeyState`/`GetKeyboardState` as unusable inside a low-level hook
callback ("the callback function is called before the asynchronous state of
the key is updated"), so there is no OS call standing in for the shift/lock
bookkeeping this backend keeps by hand.

Injected events are recorded and marked, not dropped. `RawEvent.injected`
comes from `LLKHF_INJECTED`/`LLMHF_INJECTED`, which XRecord has no equivalent
of at all -- Windows is telling this backend something X11 structurally
cannot, and throwing it away would repeat the failure mode this package
criticises everywhere else in it: a recorder that silently drops what it saw.
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib.util
import queue
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

from .base import CaptureUnavailable, RawEvent

__all__ = ["Win32CaptureBackend", "available", "unavailable_reason"]

# -- Win32 constants ---------------------------------------------------------
#
# Named as the SDK names them, the same convention pyguitest's own
# `backends/_winapi.py` uses, so a reader checking one against Microsoft's
# documentation has nothing to translate.

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

HC_ACTION = 0
"""The one `nCode` value that means "this message carries real data"; any
other value -- always negative in practice -- must reach `CallNextHookEx`
untouched, which is a rule every hook procedure on the platform follows, not
a detail specific to these two hooks."""

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
"""The four keyboard messages a low-level hook can see. The SYS pair is what
a key held with Alt generates instead of the plain pair -- Alt+F4, Alt+Tab --
and without them every Alt-chord in a recording would simply be missing."""

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_MOUSEHWHEEL = 0x020E

WM_QUIT = 0x0012

_BUTTON_DOWN = {WM_LBUTTONDOWN: 1, WM_MBUTTONDOWN: 2, WM_RBUTTONDOWN: 3}
_BUTTON_UP = {WM_LBUTTONUP: 1, WM_MBUTTONUP: 2, WM_RBUTTONUP: 3}
"""Message code -> pyguitest's own button number. 1/2/3 for left/middle/right
is X11's numbering as well as pyguitest's -- see `Session.press_button` --
which is why this table needs no translation layer of its own, only two
small dicts naming which message means which button."""

_XBUTTON_NUMBER = {1: 8, 2: 9}
"""`mouseData`'s high word for an X-button message (1 or 2) -> pyguitest's
own numbering for the side buttons, matching `Win32Backend._BUTTONS` in
pyguitest itself."""

WHEEL_DELTA = 120
"""One wheel detent, in the units `mouseData`'s high word counts in for
`WM_MOUSEWHEEL`/`WM_MOUSEHWHEEL` -- pyguitest's own `_wheel_data` counts the
same units on the way out."""

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
"""`KBDLLHOOKSTRUCT.flags` bits this backend reads. EXTENDED is what tells
the numeric keypad's Enter apart from the main one -- both share
`VK_RETURN`, and the scan code carries no such prefix the way it would for an
injected event, so this flag is the only place the distinction survives.
INJECTED is the one thing XRecord cannot see at all: whether the keystroke
came from a person or from another process calling `SendInput`."""

LLMHF_INJECTED = 0x01
"""`MSLLHOOKSTRUCT.flags`' injected bit -- a different bit position from the
keyboard structure's, because they are two different structures documented
on two different pages, not two views of the same flags word."""

VK_PACKET = 0xE7
"""The virtual key `SendInput`'s `KEYEVENTF_UNICODE` events arrive as.

Documented on `KEYBDINPUT`: "Windows 2000/XP: ... this flag also causes
Windows to synthesize the keystrokes necessary to produce a character with
the specified virtual key code ... [with `KEYEVENTF_UNICODE`] ... the system
synthesizes a `VK_PACKET` keystroke". A low-level hook sees exactly that --
`vkCode == VK_PACKET` and `scanCode` holding the UTF-16 code unit that was
injected, not a real key. `ToUnicodeEx` cannot recover it: it maps a virtual
key through the active keyboard *layout*, and `VK_PACKET` names no key any
layout defines, so it answers 0 for this vk on every keyboard -- see
`_text_for`'s own docstring for the same shape of gap with dead keys. The
character is not lost, though: unlike a dead key, this one is not ambiguous
at all, it is sitting in `scanCode` verbatim, and reading it directly there
is what `_record_key` does rather than asking a keyboard layout for an
answer no layout has.

A unit, not always a character: anything outside the Basic Multilingual
Plane arrives as two of these, and `_SurrogatePairs` is where the halves are
put back together into the one character they encode.

Real, not a corner case reached only by injected test input. Every route
that is not a plain physical keystroke goes through `KEYEVENTF_UNICODE` --
IMEs composing CJK text, an on-screen keyboard, emoji pickers, clipboard-as-
keystrokes tools, and other remote-input software -- so a recording made
while any of those is how the person typed depends on this, not only a
synthetic probe."""

_VK_SHIFT, _VK_CONTROL, _VK_MENU = 0x10, 0x11, 0x12
_VK_LSHIFT, _VK_RSHIFT = 0xA0, 0xA1
_VK_LCONTROL, _VK_RCONTROL = 0xA2, 0xA3
_VK_LMENU, _VK_RMENU = 0xA4, 0xA5
_VK_CAPITAL, _VK_NUMLOCK, _VK_SCROLL = 0x14, 0x90, 0x91
_VK_RETURN = 0x0D
"""Virtual-key codes this module's own state tracking needs by number rather
than by name -- `ToUnicodeEx` checks the *generic* Shift/Control/Alt bytes in
its key-state array, not only the left/right-specific ones a low-level hook
actually reports, so both have to be kept in step by hand (3.8's own point:
`GetKeyboardState` cannot be asked inside the callback, so nothing here reads
the real one)."""

_ALTGR_FAKE_CONTROL_SCAN = 0x21D
"""The scan code of the left Control key Windows invents for AltGr.

A keyboard layout with an AltGr key (German, French, Spanish, Polish and most
others that are not US or UK English) has no such key as far as the hardware
is concerned: AltGr is the right Alt key, and Windows treats "Ctrl+Alt" as the
same thing. So a press of it arrives at a low-level hook as two keystrokes --
a `VK_LCONTROL` whose scan code is this value rather than the real left
Control's `0x1D`, immediately followed by the real `VK_RMENU`. Nothing the
person pressed produced the first of them; it is Windows saying "Ctrl+Alt".

Read as it arrives, that is a Ctrl+Alt chord: typing `@` on a German layout
(AltGr+Q) or `€` (AltGr+E) recorded as a hotkey and the character was lost.
X11 never has the problem -- there AltGr is `ISO_Level3_Shift`, one key and
one modifier the normalizer already knows makes text rather than commands --
so `_record_key` produces the same thing here: the fake Control is kept in
the shadow keyboard state (`ToUnicodeEx` needs Ctrl and Alt both down to
answer with the AltGr character) but never enqueued, and the `VK_RMENU` that
follows it is named `ISO_Level3_Shift`."""

_ALTGR_KEYSYM = "ISO_Level3_Shift"

_TOGGLE_KEYS = (_VK_CAPITAL, _VK_NUMLOCK, _VK_SCROLL)
"""Keys whose *toggle* bit (0x01 in the key-state byte) matters to
`ToUnicodeEx`, flipped on every key-down transition rather than tracked as
held/released the way every other key is."""

_KEYSTATE_DOWN = 0x80
_KEYSTATE_TOGGLED = 0x01

_TOUNICODE_NO_KEYBOARD_STATE_CHANGE = 0x04
"""`ToUnicodeEx`'s `wFlags` bit 2: read the layout without consuming its
dead-key state.

The difference between observing a keystroke and eating it. See `_text_for`,
which is the only caller and carries the whole argument. Windows 10 1607 and
newer honour it; older builds ignore the bit.
"""

_SIDED_TO_GENERIC = {
    _VK_LSHIFT: _VK_SHIFT,
    _VK_RSHIFT: _VK_SHIFT,
    _VK_LCONTROL: _VK_CONTROL,
    _VK_RCONTROL: _VK_CONTROL,
    _VK_LMENU: _VK_MENU,
    _VK_RMENU: _VK_MENU,
}

_JOIN_TIMEOUT = 2.0
"""Seconds to wait for the pump thread to leave `GetMessageW` after `stop()`
posts `WM_QUIT` -- the same margin `X11CaptureBackend` gives its own pump
thread leaving `record_enable_context`."""

_INTERRUPT_POLL = 0.25
"""Seconds `events` waits on its queue before looking up, so Ctrl-C can land.

Windows does not interrupt an untimed wait for a signal; see `events`. A quarter
of a second is short enough that an interrupt is prompt to a person and long
enough that an idle recording costs four wake-ups a second, not a busy loop."""

HOOK_HEALTH_INTERVAL = 1.0
"""Seconds between heartbeats that ask whether these hooks are still firing.

One second is the resolution of the answer a person needs: a recording whose
hook was removed is missing everything typed since, and knowing within a second
of it happening is the difference between a note that says how much is missing
and a script nobody can reconcile with what they did.

Cheap, too, and measured so: a heartbeat is `GetLastInputInfo` and `GetTickCount`
-- two reads of values the system keeps for idle detection -- once a second, on
a thread of its own, with no AX or window-server work anywhere near it.
"""

HOOK_LOST_SLACK = 2.0
"""How far the hook may trail the system's last input before it is called lost.

Seconds, and the quantity being compared is a *duration*, not a latency: the
system's "time since the last input" against this backend's "time since the last
callback", both read at the same instant. A hook that is working trails the
system by microseconds, so the difference is 0 whatever the sampling rate; a
hook Windows has removed trails it by everything since, so the difference *is*
the length of the input nobody recorded.

Two seconds rather than one because a heartbeat can only notice what has already
happened: with a one-second interval, a gap opens and is measured somewhere
between one and two seconds after it starts, so the threshold has to sit above
the interval. Above it, the reading is a floor on the gap rather than a
measurement of it, and the note this feeds says "about" for that reason.
"""

_SENTINEL = object()


# -- structures and callback shape -------------------------------------------


class _POINT(ctypes.Structure):
    """A screen coordinate, as a Win32 `POINT`: two 32-bit `LONG`s."""

    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    """What a keyboard hook receives: a virtual key, a scan code, and flags."""

    _fields_ = [
        ("vkCode", ctypes.c_ulong),
        ("scanCode", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    """What a mouse hook receives, wheel/X-button data packed into `mouseData`."""

    _fields_ = [
        ("pt", _POINT),
        ("mouseData", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _LASTINPUTINFO(ctypes.Structure):
    """`GetLastInputInfo`'s structure: a size field, then a 32-bit tick count.

    `dwTime` is `GetTickCount`'s own milliseconds-since-boot of the session's
    last input event, which is a different clock from `time.monotonic()` -- see
    `Win32CaptureBackend._unseen_input_seconds` for why only *differences*
    between the two are ever compared, and why that makes them comparable at
    all.

    `cbSize` is not optional and not decoration: the function refuses (returns
    FALSE) unless it is set to this structure's own size, which is how the API
    stays extensible.
    """

    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]


class _MSG(ctypes.Structure):
    """One message from the pump thread's queue.

    See pyguitest's own `MSG` in `backends/_winapi.py` for why nothing here
    reads a field of it.
    """

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_ulong),
        ("pt", _POINT),
    ]


_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
"""The stdcall type where it exists (Windows), cdecl everywhere else so this
module still imports off Windows -- the same reasoning pyguitest's own
`_winapi.py` gives for the identical line."""

_HOOKPROC = _FUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)
"""`HOOKPROC`: one shape for both `WH_KEYBOARD_LL` and `WH_MOUSE_LL`, since
the platform documents both callbacks with the identical
`(nCode, wParam, lParam)` signature and lets `wParam`/`lParam`'s meaning
carry the difference."""


# -- loading ------------------------------------------------------------------

_user32_cache: Any = None
_kernel32_cache: Any = None


def _load(name: str) -> Any:
    """Load one DLL, or None where `ctypes.WinDLL` does not exist at all."""
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return None
    try:
        return loader(name)
    except OSError:
        return None


def _user32() -> Any:
    """user32.dll, with every prototype this module calls declared once."""
    global _user32_cache
    if _user32_cache is None:
        lib = _load("user32")
        if lib is not None:
            _declare_user32(lib)
        _user32_cache = lib
    return _user32_cache


def _kernel32() -> Any:
    """kernel32.dll, with `GetCurrentThreadId` and `GetTickCount` declared."""
    global _kernel32_cache
    if _kernel32_cache is None:
        lib = _load("kernel32")
        if lib is not None:
            lib.GetCurrentThreadId.argtypes = ()
            lib.GetCurrentThreadId.restype = ctypes.c_ulong
            # `GetTickCount`, not `GetTickCount64`: `_LASTINPUTINFO.dwTime` is
            # the 32-bit one whatever the machine's uptime, and the heartbeat
            # compares them, so both sides have to be the same width. The
            # subtraction is done modulo 2**32 for the 49.7-day wrap.
            lib.GetTickCount.argtypes = ()
            lib.GetTickCount.restype = ctypes.c_ulong
        _kernel32_cache = lib
    return _kernel32_cache


def _declare_user32(lib: Any) -> None:
    """Declare the user32 prototypes this module uses.

    Grouped in one function for the reason pyguitest's own `_winapi.py`
    groups its declarations in one place: a wrong `argtypes` truncates a
    pointer or a 64-bit handle, and the symptom shows up somewhere else --
    so every one of them belongs where a reader can check it against the SDK
    in a single pass.
    """
    lib.SetWindowsHookExW.argtypes = (
        ctypes.c_int,
        _HOOKPROC,
        ctypes.c_void_p,
        ctypes.c_ulong,
    )
    lib.SetWindowsHookExW.restype = ctypes.c_void_p
    lib.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
    lib.UnhookWindowsHookEx.restype = ctypes.c_int
    lib.CallNextHookEx.argtypes = (
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    )
    lib.CallNextHookEx.restype = ctypes.c_ssize_t
    lib.GetMessageW.argtypes = (
        ctypes.POINTER(_MSG),
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_uint,
    )
    # BOOL in the SDK, but documented to return -1 on error too -- c_int
    # rather than an unsigned type, so -1 survives rather than becoming a
    # large positive value that reads as "got a message".
    lib.GetMessageW.restype = ctypes.c_int
    lib.PeekMessageW.argtypes = (
        ctypes.POINTER(_MSG),
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_uint,
    )
    lib.PeekMessageW.restype = ctypes.c_int
    lib.TranslateMessage.argtypes = (ctypes.POINTER(_MSG),)
    lib.TranslateMessage.restype = ctypes.c_int
    lib.DispatchMessageW.argtypes = (ctypes.POINTER(_MSG),)
    lib.DispatchMessageW.restype = ctypes.c_ssize_t
    lib.PostThreadMessageW.argtypes = (
        ctypes.c_ulong,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    )
    lib.PostThreadMessageW.restype = ctypes.c_int
    lib.GetForegroundWindow.argtypes = ()
    lib.GetForegroundWindow.restype = ctypes.c_void_p
    lib.GetWindowThreadProcessId.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    )
    lib.GetWindowThreadProcessId.restype = ctypes.c_ulong
    lib.GetKeyboardLayout.argtypes = (ctypes.c_ulong,)
    lib.GetKeyboardLayout.restype = ctypes.c_void_p
    lib.ToUnicodeEx.argtypes = (
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
    )
    lib.ToUnicodeEx.restype = ctypes.c_int
    # Read once per start, never inside a hook callback: the toggle keys'
    # state at the moment recording begins -- see `_KeyboardState.seed`.
    lib.GetKeyState.argtypes = (ctypes.c_int,)
    lib.GetKeyState.restype = ctypes.c_short
    # The heartbeat's two calls. `GetLastInputInfo` is the session's own last
    # input -- the one reading that is not this process's opinion -- and
    # `GetTickCount` is the same clock it reports in, which lives in kernel32
    # rather than here; see `_kernel32`.
    lib.GetLastInputInfo.argtypes = (ctypes.POINTER(_LASTINPUTINFO),)
    lib.GetLastInputInfo.restype = ctypes.c_int


def available() -> bool:
    """Whether this machine can install a low-level input hook at all.

    A real install-then-immediately-remove, not only a DLL-load check --
    `SetWindowsHookExW` is the call that actually fails on session 0 or a
    locked desktop, and a probe that only checked the library loaded would
    answer yes right up until `start()` discovered otherwise.
    """
    return unavailable_reason() is None


_NO_PYGUITEST_WIN32 = (
    "the installed pyguitest has no win32 backend, so there is no key "
    "vocabulary to record against -- `pyguitest.backends.win32` arrived with "
    "pyguitest's own Windows support and is absent from every release before "
    "it. Upgrade pyguitest (pip install --upgrade pyguitest)"
)
"""Why a Windows recording cannot start against an older pyguitest.

This package depends on pyguitest for the *vocabulary* a captured virtual key
is reported in -- `VK` and `key_name_for_virtual_key`, which `_KeyVocabulary`
reads -- and that module landed with pyguitest's Windows support. Until a
release carries it, a Windows recording is unavailable here for a reason that
has nothing to do with this machine, and saying so beats a
`ModuleNotFoundError` raised four frames inside a backend constructor.
"""


def _pyguitest_win32_available() -> bool:
    """Whether the installed pyguitest carries the win32 key vocabulary.

    `find_spec` rather than an import, so asking costs nothing and leaves
    nothing imported: `unavailable_reason` runs on every `--probe` and every
    backend selection, and importing pyguitest's Windows backend to find out
    whether it exists would be a side effect this question does not need.
    """
    try:
        return importlib.util.find_spec("pyguitest.backends.win32") is not None
    except (ImportError, ValueError):
        # A pyguitest too old to have the `backends` package at all, or a
        # namespace package with no spec to report. Either is "not there".
        return False


def unavailable_reason() -> str | None:
    """Why this machine cannot capture, or None if it can.

    Four distinct failures, reported apart for the same reason
    `x11.unavailable_reason` keeps its three apart: they look identical from
    the outside -- "capture will not start" -- and need different fixes. The
    fourth is not about this machine at all: an installed pyguitest older than
    its own Windows support has no key vocabulary for this backend to read,
    and that is a `pip install --upgrade` rather than anything about the
    desktop. It is checked before `user32`, because it is the one that is
    equally true on a machine where everything else is perfect.
    """
    if sys.platform != "win32":
        return "this is not a native Windows process; the win32 backend needs one"
    if not _pyguitest_win32_available():
        return _NO_PYGUITEST_WIN32
    lib = _user32()
    if lib is None:
        return "user32.dll did not load, which should not happen on Windows itself"

    def _noop(_n: int, _w: int, _l: int) -> int:
        """The probe hook's body: installed and removed without ever firing."""
        return 0

    probe = _HOOKPROC(_noop)
    hook = lib.SetWindowsHookExW(WH_KEYBOARD_LL, probe, None, 0)
    if not hook:
        return (
            "SetWindowsHookExW refused a low-level keyboard hook -- the usual "
            "cause is a process with no interactive window station (a "
            "service, a scheduled task not set to 'Run only when user is "
            "logged on', or an SSH session with no desktop of its own)"
        )
    lib.UnhookWindowsHookEx(hook)
    return _off_desktop_reason()


def _off_desktop_reason() -> str | None:
    """Why a hook that installed will still see nothing, or None.

    **Installing a hook is not evidence that it can observe anything.** A hook
    is scoped to the window station and desktop of the thread that installs
    it, and a process off the interactive desktop has a station of its own --
    so `SetWindowsHookExW` succeeds there and then reports not one keystroke
    from the session a person is actually using.

    Measured over SSH on a real Windows 11 box: the probe above installed
    cleanly, `unavailable_reason()` answered None, and `--doctor` printed
    "ready to record" for a process that could not have captured anything.
    The message it would have printed on failure even names SSH as the usual
    cause -- the branch simply never fired.

    Asked of pyguitest, whose `is_interactive_desktop` is the same question
    already probed for `detect()` and confirmed correct on that machine, so
    this neither duplicates the Win32 call nor invents a second answer to it.
    An unanswerable probe is not a refusal: `detect()` reports True where it
    cannot tell, and a recorder that refused on "cannot tell" would be worse
    than one that tries.

    **The field is newer than the floor this package declares.** It arrived
    with pyguitest's own Windows support, and no release carries it yet, so an
    installed pyguitest that is otherwise perfectly usable has no such
    attribute -- and neither does the type a checker sees when it reads the
    same floor, which is what made this a lint failure rather than a runtime
    one. `getattr` with a default of True is the shape the rest of the tree
    uses for a pyguitest question an older version cannot answer (see
    `describe_environment`): the missing field means "cannot tell", which the
    paragraph above already resolves as try-anyway.
    """
    try:
        import pyguitest

        detected = pyguitest.detect()
    except Exception:  # noqa: BLE001 - cannot tell is not a refusal
        return None
    if getattr(detected, "is_interactive_desktop", True):
        return None
    return (
        "this process is not attached to the interactive window station, so a "
        "hook installs against a desktop nobody is using and would record "
        "nothing. A service, a scheduled task not set to 'Run only when user "
        "is logged on', and an SSH session all land here -- record from the "
        "logged-in session instead"
    )


# -- the keysym vocabulary ----------------------------------------------------


def _key_vocabulary() -> tuple[dict[str, int], Any]:
    """Pyguitest's own `VK` table and `key_name_for_virtual_key`, imported once.

    Deferred rather than a module-level import: this file has to import
    cleanly wherever `pyguitest_recorder.backends` is imported at all, on
    every platform, and pyguitest itself is a hard dependency but there is no
    reason to pay for it before a Windows recording actually starts.

    **The module it wants may not be there.** `pyguitest.backends.win32`
    arrived with pyguitest's own Windows support and does not exist in any
    release before it, so an installed-but-older pyguitest reaches this line
    and raises `ModuleNotFoundError` from four frames down -- which says
    nothing about what to install. `unavailable_reason` asks the same
    question up front and refuses with a sentence; this raises the typed
    error for the paths that get here anyway.
    """
    try:
        from pyguitest.backends.win32 import VK, key_name_for_virtual_key
    except ImportError as exc:  # pragma: no cover - unavailable_reason gates it
        raise CaptureUnavailable(_NO_PYGUITEST_WIN32) from exc

    return VK, key_name_for_virtual_key


def _build_keysym_table(vk: dict[str, int]) -> dict[int, str]:
    """Virtual-key code -> X11's own keysym name, keyed by pyguitest's `VK`.

    Reuses pyguitest's `VK` for the *codes* -- already transcribed and
    tested there -- and supplies only the *names*, because pyguitest's own
    keysym vocabulary (`press_key`'s) is deliberately lower-cased
    (`"backspace"`, `"control_l"`) while `analyzer/normalize.py`'s
    `MODIFIERS` dict matches X11's real, mixed-case spelling
    (`"Control_L"`, `"Shift_L"`) exactly, character for character. Replay
    itself would not care -- `press_key` lower-cases whatever it is given
    before matching -- but the recorder's own modifier and hotkey detection
    does a plain string comparison, so a lower-cased `"control_l"` recorded
    here would make every Windows-recorded Ctrl-chord invisible to
    `_on_key_press`'s own logic. Named keys therefore get X11's real
    spelling by hand; letters, digits and anything else this table does not
    name fall back to pyguitest's own lookup, whose case is a non-issue
    there because nothing compares them for it.
    """
    named = {
        "VK_BACK": "BackSpace",
        "VK_TAB": "Tab",
        "VK_PAUSE": "Pause",
        "VK_CAPITAL": "Caps_Lock",
        "VK_ESCAPE": "Escape",
        "VK_SPACE": "space",
        "VK_PRIOR": "Prior",
        "VK_NEXT": "Next",
        "VK_END": "End",
        "VK_HOME": "Home",
        "VK_LEFT": "Left",
        "VK_UP": "Up",
        "VK_RIGHT": "Right",
        "VK_DOWN": "Down",
        "VK_SNAPSHOT": "Print",
        "VK_INSERT": "Insert",
        "VK_DELETE": "Delete",
        "VK_HELP": "Help",
        "VK_LWIN": "Super_L",
        "VK_RWIN": "Super_R",
        "VK_APPS": "Menu",
        "VK_MULTIPLY": "KP_Multiply",
        "VK_ADD": "KP_Add",
        "VK_SEPARATOR": "KP_Separator",
        "VK_SUBTRACT": "KP_Subtract",
        "VK_DECIMAL": "KP_Decimal",
        "VK_DIVIDE": "KP_Divide",
        "VK_NUMLOCK": "Num_Lock",
        "VK_SCROLL": "Scroll_Lock",
        "VK_LSHIFT": "Shift_L",
        "VK_RSHIFT": "Shift_R",
        "VK_LCONTROL": "Control_L",
        "VK_RCONTROL": "Control_R",
        "VK_LMENU": "Alt_L",
        "VK_RMENU": "Alt_R",
        "VK_CANCEL": "Cancel",
        "VK_OEM_1": "semicolon",
        "VK_OEM_PLUS": "equal",
        "VK_OEM_COMMA": "comma",
        "VK_OEM_MINUS": "minus",
        "VK_OEM_PERIOD": "period",
        "VK_OEM_2": "slash",
        "VK_OEM_3": "grave",
        "VK_OEM_4": "bracketleft",
        "VK_OEM_5": "backslash",
        "VK_OEM_6": "bracketright",
        "VK_OEM_7": "apostrophe",
    }
    named.update({f"VK_F{n}": f"F{n}" for n in range(1, 25)})
    named.update({f"VK_NUMPAD{n}": f"KP_{n}" for n in range(10)})
    return {vk[name]: keysym for name, keysym in named.items() if name in vk}


class _KeyVocabulary:
    """Resolves a captured virtual key into pyguitest's `RawEvent.keysym`.

    Built once per backend instance rather than once per event: importing
    pyguitest and building `_build_keysym_table`'s dict both cost real work,
    and every event needs the identical answer for the identical code.
    """

    def __init__(self) -> None:
        """Build the vocabulary and its keysym table once for this instance."""
        self._vk, self._fallback = _key_vocabulary()
        self._table = _build_keysym_table(self._vk)
        self._return = self._vk["VK_RETURN"]

    def name(self, vk_code: int, extended: bool) -> str:
        """The keysym for `vk_code`, resolving Enter's one ambiguity.

        Every other VK this backend can name maps to exactly one physical
        key. `VK_RETURN` is the sole exception -- the main Enter and the
        keypad's share it, and only `LLKHF_EXTENDED` on the scan code tells
        them apart, which is exactly the caveat pyguitest's own
        `key_names_for_virtual_key` names for the same code.
        """
        if vk_code == self._return:
            return "KP_Enter" if extended else "Return"
        name = self._table.get(vk_code)
        if name is not None:
            return name
        fallback = self._fallback(vk_code)
        if fallback is not None:
            return str(fallback)
        return f"0x{vk_code:x}"


# -- keyboard state and text -------------------------------------------------


class _KeyboardState:
    """A shadow of `GetKeyboardState`'s 256-byte array, kept by hand.

    Windows documents `GetAsyncKeyState`/`GetKeyboardState` as unusable
    inside a low-level hook callback -- "the callback function is called
    before the asynchronous state of the key is updated" -- so there is no
    system call standing in for this. Every transition this backend itself
    observes updates the array, the same shape X11's own shift/lock masks
    are kept in `X11CaptureBackend._resolve_keysym`.
    """

    def __init__(self) -> None:
        """Start with every key up and no modifier latched."""
        self._state = bytearray(256)

    def seed(self, lib: Any) -> None:
        """Start from the toggle keys' real state rather than from all-off.

        Caps Lock, Num Lock and Scroll Lock are *latched*, so the transitions
        this shadow sees during a recording say nothing about where they were
        when it began. Starting them off made a recording begun with Caps Lock
        on capture its opposite: measured on Windows 11, `a` pressed with Caps
        Lock on typed `A` in the application and was recorded as text `'a'`,
        so the replay typed the wrong case. `GetKeyState`'s low bit is the
        toggle, and asking it here -- once, before the hooks, rather than
        inside a callback where the documented rule forbids trusting it -- is
        what gives the shadow the state the keyboard actually has.
        """
        for vk_code in _TOGGLE_KEYS:
            if lib.GetKeyState(vk_code) & _KEYSTATE_TOGGLED:
                self._state[vk_code] |= _KEYSTATE_TOGGLED
            else:
                self._state[vk_code] &= ~_KEYSTATE_TOGGLED

    def press(self, vk_code: int) -> None:
        """Record `vk_code` going down: its own byte, generic byte, and toggle bit.

        Every documented `VK_*` constant fits one byte, but nothing enforces
        that on the way in from a hook structure -- an out-of-range code is
        silently ignored rather than raising `IndexError` out of a callback
        Windows is already timing, where an uncaught exception would abort
        the rest of this call, `CallNextHookEx` included, and the keystroke
        would never reach the application it was meant for either.
        """
        if not 0 <= vk_code < 256:
            return
        self._state[vk_code] |= _KEYSTATE_DOWN
        generic = _SIDED_TO_GENERIC.get(vk_code)
        if generic is not None:
            self._state[generic] |= _KEYSTATE_DOWN
        if vk_code in _TOGGLE_KEYS:
            self._state[vk_code] ^= _KEYSTATE_TOGGLED

    def release(self, vk_code: int) -> None:
        """Record `vk_code` going up, and its generic byte too if that clears it."""
        if not 0 <= vk_code < 256:
            return
        self._state[vk_code] &= ~_KEYSTATE_DOWN
        generic = _SIDED_TO_GENERIC.get(vk_code)
        if generic is None:
            return
        left, right = (
            (_VK_LSHIFT, _VK_RSHIFT)
            if generic == _VK_SHIFT
            else (_VK_LCONTROL, _VK_RCONTROL)
            if generic == _VK_CONTROL
            else (_VK_LMENU, _VK_RMENU)
        )
        other = right if vk_code == left else left
        if not self._state[other] & _KEYSTATE_DOWN:
            self._state[generic] &= ~_KEYSTATE_DOWN

    def array(self) -> ctypes.Array[ctypes.c_ubyte]:
        """The current state as a fresh `ctypes` byte array `ToUnicodeEx` can read."""
        return (ctypes.c_ubyte * 256)(*self._state)


def _foreground_layout(lib: Any) -> Any:
    """The keyboard layout handle of whatever window has the foreground.

    Not this process's own layout: `GetKeyboardLayout(0)` answers about the
    calling thread, and the calling thread here is the pump thread, which
    has no relationship at all to the layout of the application actually
    receiving the keystroke -- a bilingual user's two applications can run
    two different layouts at once. Asked fresh per keystroke, since Alt-Tab
    can change which application -- and which layout -- is in front between
    one key and the next.
    """
    window = lib.GetForegroundWindow()
    if not window:
        return None
    thread_id = lib.GetWindowThreadProcessId(window, None)
    if not thread_id:
        return None
    return lib.GetKeyboardLayout(thread_id)


def _text_for(lib: Any, vk_code: int, scan_code: int, state: _KeyboardState) -> str:
    """The character `vk_code` produces under the current keyboard state.

    `ToUnicodeEx` is the Windows counterpart of the X11 backend's
    `_printable(keysym)` -- the layout- and shift-aware answer to "what would
    this keystroke actually type" -- and it is what makes `RawEvent.text`
    true here the same way it is true on Linux, which is what lets
    `analyzer/normalize.py`'s text accumulation work completely unmodified. A
    negative return means a dead key was struck and is now composing; that
    composing state is real but this backend does not try to resolve it into
    the eventual composed character, the one dead-key limit this route
    shares with X11's own keysym-based `_printable`, which cannot compose
    one either. Zero or a lookup failure -- a non-printing key, most of them
    -- is the empty string, exactly what `_on_key_press` reads as "not
    text".

    **`_TOUNICODE_NO_KEYBOARD_STATE_CHANGE` is not optional.** Called with no
    flags, `ToUnicodeEx` does not merely read the layout -- it *consumes* the
    pending dead-key state, which is kernel-mode state belonging to the
    keyboard layout rather than to this process. This function runs inside a
    hook, on every key-down, ahead of the application the keystroke is going
    to: without the flag, a person typing `'` then `e` on an international
    layout gets `'e` in their editor instead of `é`, because the recorder ate
    the dead key on the way past. That is this module's own stated rule --
    observe without altering -- broken in the one place it would be blamed on
    the application. The flag says "do not change keyboard state" and arrived
    in Windows 10 1607; an older build silently ignores the bit and is left
    with the composition bug, which is the honest floor rather than something
    to work around by calling twice and hoping the second call restores what
    the first took.
    """
    layout = _foreground_layout(lib)
    buffer = ctypes.create_unicode_buffer(8)
    written = lib.ToUnicodeEx(
        vk_code,
        scan_code,
        state.array(),
        buffer,
        len(buffer),
        _TOUNICODE_NO_KEYBOARD_STATE_CHANGE,
        layout,
    )
    if written <= 0:
        return ""
    text = buffer.value[:written]
    if len(text) != 1 or ord(text) < 32 or ord(text) == 127:
        return ""
    return text


# -- unicode code units ------------------------------------------------------

_HIGH_SURROGATES = range(0xD800, 0xDC00)
_LOW_SURROGATES = range(0xDC00, 0xE000)


class _SurrogatePairs:
    r"""Rebuilds one character out of the UTF-16 code units `VK_PACKET` carries.

    Anything outside the Basic Multilingual Plane -- an emoji, the rarer CJK
    ideographs, every character above `U+FFFF` -- is two UTF-16 code units on
    the wire, and `SendInput`'s `KEYEVENTF_UNICODE` sends each of them as a
    `VK_PACKET` keystroke of its own: a high half in `0xD800-0xDBFF`, then a
    low half in `0xDC00-0xDFFF`.

    `chr()` on one of those halves produces a *lone surrogate*, and joining the
    two halves does not decode them either -- a Python string is code points,
    not UTF-16 units, so `"\\uD83D" + "\\uDE00"` is two unpaired halves rather
    than the one character they encode. That is what this used to put in
    `RawEvent.text`, and it travels a long way: `normalize.py` concatenates the
    text of consecutive keystrokes into one `TextInput`, the generator writes
    it as a literal in `gui.type_text(...)`, and the first thing that has to
    encode that string -- writing the script, or pyguitest's own injection --
    raises `UnicodeEncodeError`. A crash in the tool, over a character it had
    read perfectly.

    So the high half is held back until its pair arrives, and only the pair
    becomes text. An unpaired half is dropped rather than passed on: it has no
    character to replay, and the alternative is the crashing script above. The
    halves of a pair are always adjacent -- one `SendInput` call describes one
    character, and the key releases in between change nothing here -- and a
    half still pending when some other key is pressed was never going to be
    half of anything.
    """

    def __init__(self) -> None:
        """Start with no half of a surrogate pair pending."""
        self._high: int | None = None

    def character(self, unit: int) -> str:
        """The text one `VK_PACKET` code unit contributes, pairing where it can."""
        if self._high is not None:
            high, self._high = self._high, None
            if unit in _LOW_SURROGATES:
                return chr(0x10000 + ((high - 0xD800) << 10) + (unit - 0xDC00))
        if unit in _HIGH_SURROGATES:
            self._high = unit
            return ""
        if unit in _LOW_SURROGATES:
            # A low half whose high half never came, or whose predecessor was
            # not it: alone it is not a character either.
            return ""
        return chr(unit)

    def forget(self) -> None:
        """Drop a pending high half: the next keystroke was not its pair."""
        self._high = None


# -- the backend ---------------------------------------------------------


class Win32CaptureBackend:
    """Capture keyboard and pointer input through low-level Win32 hooks."""

    name = "win32"

    stop_pressed_at: float | None = None
    """Always None: this backend does not recognise the stop chord as it captures.

    `X11CaptureBackend` does, and ends its stream at the completing press so a
    recording that fell behind live input still ends where the key was pressed.
    Here the consumer's own recogniser is what ends a recording, which is what
    `CaptureBackend.stop_pressed_at` says a backend that leaves this None gets.
    Running one in a hook callback would put work in the one place that is kept
    to reading a structure and enqueueing it -- see `_on_keyboard_event` for
    why -- and the consumer's recogniser costs nothing a person can perceive.
    """

    def __init__(self, display: str | None = None, screen: int = 0) -> None:
        """Prepare a capture backend. `display` is accepted and ignored.

        `display` exists only so `choose_backend` can construct either
        backend the same way: it names an X server, which Windows has no
        equivalent of. `screen` is the same opaque tag
        `X11CaptureBackend` carries through to every `RawEvent` -- neither
        backend queries a monitor index from the platform, both simply echo
        back what the caller supplied.
        """
        self.screen = screen
        self._keyboard_hook: Any = None
        self._mouse_hook: Any = None
        self._keyboard_proc: Any = None
        self._mouse_proc: Any = None
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._start_error: Exception | None = None
        self._queue: queue.Queue[Any] = queue.Queue()
        self._vocabulary = _KeyVocabulary()
        self._state = _KeyboardState()
        self._surrogates = _SurrogatePairs()
        self._wheel = _WheelAccumulator()
        self._altgr_announced = False
        self._altgr_down = False
        self._last_event: float = 0.0
        """When a hook callback last ran, on `time.monotonic`'s clock.

        Stamped by both callbacks, which is the one thing the heartbeat asks of
        them: a single float assignment, the cheapest write there is, in the one
        place the platform's `LowLevelHooksTimeout` rule says to keep minimal.
        The module's own `_now` is the same clock and is deliberately *not* used
        here -- one function call less, in the callback.
        """
        self._health_stop = threading.Event()
        self._health_thread: threading.Thread | None = None

    hook_lost_seconds: float | None = None
    """How much input went unseen by these hooks, or None if none did.

    Set by the heartbeat (`_watch_health`) when the system reports input this
    backend's callbacks never saw. It is the *duration* of the gap rather than a
    flag, and it is a floor rather than a measurement -- see `HOOK_LOST_SLACK` --
    because the heartbeat can only answer for the interval it has just watched.

    None means the question never came up: hooks that kept firing, or a run too
    short for a heartbeat to be sure. The recorder turns this into a note (see
    `recorder._hook_lost_note`), which is the whole reason it is public: a
    recording whose hook was removed looks complete, and a script generated from
    it replays a sequence with a hole in the middle that nobody can see.

    The other reading of the same number is not a defect and not distinguishable
    from here: input on a desktop these hooks cannot see -- a UAC prompt, the
    lock screen, an RDP session that dropped -- advances the system's last-input
    time with no callback, exactly like a removed hook. The note says "may be
    missing" for that reason rather than naming a cause.
    """

    def start(self) -> None:
        """Install both hooks and begin pumping their thread's message queue.

        Both hooks are installed *by* the pump thread rather than handed to
        it afterwards: `SetWindowsHookExW` for a low-level hook is serviced
        only while the *installing* thread calls `GetMessageW`/`PeekMessageW`,
        so hook and pump have to share one thread from the start, the same
        constraint pyguitest's own `SetWinEventHook`-based window-events pump
        is built around.

        The interactive desktop is checked first, and not left to
        `unavailable_reason`: that one runs at *selection* time
        (`choose_backend`), which is the wrong moment for this question twice
        over. A backend can be selected on a real desktop and started on a
        session that has since gone away -- RDP dropping to a disconnected
        session is enough -- and nothing about this class requires that it was
        selected at all. A hook that installs off the interactive desktop
        succeeds and then sees nothing: capture that starts, reports no error,
        and records no input is the one failure with no symptom to read
        afterwards.
        """
        if sys.platform != "win32":
            raise CaptureUnavailable(unavailable_reason() or "not on Windows")
        lib = _user32()
        if lib is None:
            raise CaptureUnavailable("user32.dll did not load")
        reason = _off_desktop_reason()
        if reason is not None:
            raise CaptureUnavailable(reason)
        self._state.seed(lib)
        self._keyboard_proc = _HOOKPROC(self._on_keyboard_event)
        self._mouse_proc = _HOOKPROC(self._on_mouse_event)
        self._ready.clear()
        self._start_error = None
        # Reset rather than left from a previous start() on this same object:
        # `_watch_health` returns for good the first time it finds a gap, so a
        # stale value here would silently survive into a recording whose hooks
        # never missed a beat.
        self.hook_lost_seconds = None
        self._thread = threading.Thread(
            target=self._run, name="pyguitest-recorder-win32", daemon=True
        )
        self._thread.start()
        self._ready.wait()
        if self._start_error is not None:
            error, self._start_error = self._start_error, None
            self.stop()
            raise error

    def _run(self) -> None:
        """Install the hooks, signal readiness, then pump until told to stop.

        Everything from installing the first hook onward runs under one
        try/finally that unhooks whatever was installed -- a hook left
        dangling here has no other owner to remove it, the same rule
        `X11CaptureBackend.start`'s docstring states for its own connections.
        That outer `finally` has to wrap the *install* step too, not only the
        message loop: if the keyboard hook installs but the mouse hook then
        refuses, the keyboard hook is already live and this is the only place
        left that will ever remove it -- `start()` sees only the exception
        this method records, never gets a handle back to clean up itself.
        """
        lib = _user32()
        try:
            try:
                self._keyboard_hook = lib.SetWindowsHookExW(
                    WH_KEYBOARD_LL, self._keyboard_proc, None, 0
                )
                if not self._keyboard_hook:
                    self._start_error = CaptureUnavailable(
                        unavailable_reason()
                        or "SetWindowsHookExW refused the keyboard hook"
                    )
                    return
                self._mouse_hook = lib.SetWindowsHookExW(
                    WH_MOUSE_LL, self._mouse_proc, None, 0
                )
                if not self._mouse_hook:
                    self._start_error = CaptureUnavailable(
                        "SetWindowsHookExW refused the mouse hook"
                    )
                    return
                self._thread_id = _kernel32().GetCurrentThreadId()
                message = _MSG()
                pointer = ctypes.byref(message)
                # Forces this thread's message queue into existence before
                # `_ready` is signalled -- see pyguitest's own `PM_NOREMOVE`
                # docstring in `backends/_winapi.py` for why a
                # `PostThreadMessageW` sent the instant a caller decides to
                # stop must never race the pump's first real `GetMessageW`.
                lib.PeekMessageW(pointer, None, 0, 0, 0)
                # Started here rather than before the hooks, and for the reason
                # the hooks are installed here at all: a heartbeat with nothing
                # to watch would measure its own start-up. `_last_event` is
                # stamped at the same moment for the same reason -- both hooks
                # are live, so "no callback since" is a fact from now on.
                self._last_event = time.monotonic()
                self._health_stop.clear()
                self._health_thread = threading.Thread(
                    target=self._watch_health,
                    name="pyguitest-recorder-hook-health",
                    daemon=True,
                )
                self._health_thread.start()
            finally:
                self._ready.set()
            if self._start_error is not None:
                return
            while True:
                status = lib.GetMessageW(pointer, None, 0, 0)
                if status <= 0:
                    break
                lib.TranslateMessage(pointer)
                lib.DispatchMessageW(pointer)
        finally:
            # Asked to stop before the hooks come off, not after: the heartbeat
            # reads `_last_event`, and a heartbeat still polling through the
            # teardown would be measuring the gap the unhooking itself makes.
            self._health_stop.set()
            self._health_thread = None
            for hook in (self._keyboard_hook, self._mouse_hook):
                if hook:
                    lib.UnhookWindowsHookEx(hook)
            self._keyboard_hook = self._mouse_hook = None
            self._queue.put(_SENTINEL)

    def _watch_health(self) -> None:
        """Notice a hook Windows removed without saying so, and record the gap.

        Windows does not tell a process that its low-level hook was dropped for
        missing `LowLevelHooksTimeout`: the hook is simply no longer called, and
        the callback -- the only part of this process the platform ever speaks
        to -- has no way to notice its own absence. What the platform *does*
        publish is the session's own last-input time, which is kept whether or
        not any hook exists, and comparing it with the last time a callback ran
        is what turns "undetectable" into "detected".

        Stops after the first answer rather than repeating: what a recording is
        owed is the fact and a duration, not a log, and the recorder states it
        once (`recorder._hook_lost_note`).

        A thread of its own, and this is load-bearing rather than tidy: the pump
        thread is inside `GetMessageW`, which does not return for a heartbeat,
        and the callback is the one place the platform's timeout rule says must
        stay small. `stop()` sets `_health_stop`, so the wait is the only thing
        this ever blocks on.
        """
        while not self._health_stop.wait(HOOK_HEALTH_INTERVAL):
            if self.hook_lost_seconds is not None:
                return
            missed = self._unseen_input_seconds()
            if missed is not None and missed > HOOK_LOST_SLACK:
                self.hook_lost_seconds = missed
                return

    def _unseen_input_seconds(self) -> float | None:
        """How much input the system saw that these hooks did not, or None.

        Two *ages*, both read now, so the two clocks never have to agree about
        anything except a rate: `GetLastInputInfo`'s tick count -- milliseconds
        since boot, kept by the system for idle detection -- and this backend's
        monotonic stamp of its own last callback. A working hook trails the
        system by microseconds whatever the sample rate; one that has been
        removed trails it by everything since, and the difference between the
        two ages is exactly the input that went unrecorded.

        Answering None is "could not be asked", never "all is well": a `user32`
        that will not load here is the same answer `unavailable_reason` gives,
        and inventing a zero would report a healthy hook this call never
        checked.

        `GetTickCount` is 32-bit and wraps after 49.7 days of uptime, so the
        subtraction is masked to its own width; both sides wrap together, which
        is what makes that correct rather than lucky.
        """
        lib, kernel = _user32(), _kernel32()
        if lib is None or kernel is None:
            return None
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not lib.GetLastInputInfo(ctypes.byref(info)):
            return None
        # `int()` around both because `_kernel32()` and the struct fields are
        # `Any` by construction (ctypes), and this function's answer is a number
        # or nothing -- mypy is right to insist.
        elapsed_ms = int(kernel.GetTickCount()) - int(info.dwTime)
        since_input = (elapsed_ms & 0xFFFFFFFF) / 1000.0
        since_callback = time.monotonic() - self._last_event
        return since_callback - since_input

    def _on_keyboard_event(self, code: int, wparam: int, lparam: int) -> int:
        """`WH_KEYBOARD_LL`'s callback: read the structure, enqueue, return.

        Kept to exactly that -- Microsoft's own guidance for a hook this
        close to a removal timeout -- with one exception: `ToUnicodeEx` runs
        here rather than off-thread, because deferring it would mean
        deferring the keyboard-state update it depends on too, and this
        module's whole approach to shift/lock tracking is "the event stream
        is the only source of truth, read it in order." `ToUnicodeEx` itself
        is fast enough that this is not a problem: the whole callback,
        `ToUnicodeEx` included, measured 0.04ms at the median and 3ms at the
        worst of 20,000 key-downs on Windows 11, against the 300ms limit.
        """
        lib = _user32()
        if code == HC_ACTION:
            # The heartbeat's whole input, and the reason it is outside the
            # `suppress` below: this cannot raise, and a callback that was
            # reached at all is what it is reporting.
            self._last_event = time.monotonic()
            # Suppressed, and the reason is the person at the keyboard rather
            # than tidiness: an exception escaping a ctypes callback does not
            # propagate anywhere a caller can catch it -- ctypes prints the
            # traceback and returns 0 -- so `CallNextHookEx` below never runs
            # and the rest of the hook chain is skipped for that keystroke.
            # A recorder that drops the keystroke it was watching is worse
            # than one that misses an event, and `_text_for` alone makes four
            # user32 calls. `_KeyboardState.press` already guards its own
            # out-of-range case for this reason; this covers every step.
            with contextlib.suppress(Exception):
                self._record_key(lib, wparam, lparam)
        return int(lib.CallNextHookEx(None, code, wparam, lparam))

    def _record_key(self, lib: Any, wparam: int, lparam: int) -> None:
        """Read one `KBDLLHOOKSTRUCT` and enqueue the event it describes.

        Split out of `_on_keyboard_event` so the callback boundary has exactly
        one guarded call in it and `CallNextHookEx` is reached down every path
        -- see that method for why the suppression is not optional.
        """
        info = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
        vk_code = info.vkCode
        extended = bool(info.flags & LLKHF_EXTENDED)
        injected = bool(info.flags & LLKHF_INJECTED)
        pressed = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
        text = ""
        if vk_code == VK_PACKET:
            # Not a key `_KeyboardState` or `_KeyVocabulary` has any business
            # naming or tracking as held -- see VK_PACKET's own docstring.
            # `scanCode` already *is* the character, in UTF-16 code units, and
            # `_SurrogatePairs` is what turns the two halves of one outside the
            # BMP back into it.
            keysym = f"U+{info.scanCode:04X}"
            if pressed:
                text = self._surrogates.character(info.scanCode)
                if not text:
                    # A high half waiting for its pair, or a half that will
                    # never have one. `VK_PACKET` names no real key, and the
                    # normalizer turns any text-less, non-modifier press into a
                    # `KeyStroke` -- so enqueueing this produced
                    # `gui.tap_key("U+D83D")` ahead of the `type_text` for the
                    # very character it belonged to, a key name pyguitest
                    # rejects at replay. The pair's low half still carries the
                    # whole character.
                    return
        else:
            if vk_code == _VK_LCONTROL and info.scanCode == _ALTGR_FAKE_CONTROL_SCAN:
                # Half of AltGr, not a key anyone pressed -- see
                # `_ALTGR_FAKE_CONTROL_SCAN`. Tracked for `ToUnicodeEx` but
                # never enqueued, so the normalizer never sees a Ctrl.
                if pressed:
                    self._state.press(vk_code)
                    self._altgr_announced = True
                else:
                    self._state.release(vk_code)
                return
            keysym = self._vocabulary.name(vk_code, extended)
            if vk_code == _VK_RMENU:
                # The real half. It is AltGr when Windows just announced one,
                # and the release is named to match whichever the press was
                # -- the fake Control may already be up by then, so the
                # release cannot be told apart by looking for it again.
                if pressed:
                    self._altgr_down = self._altgr_down or self._altgr_announced
                    self._altgr_announced = False
                if self._altgr_down:
                    keysym = _ALTGR_KEYSYM
                if not pressed:
                    self._altgr_down = False
            if pressed:
                # A high half still waiting for its pair was never going to be
                # paired by this key.
                self._surrogates.forget()
                text = _text_for(lib, vk_code, info.scanCode, self._state)
                self._state.press(vk_code)
            else:
                self._state.release(vk_code)
        self._queue.put(
            RawEvent(
                kind="key_press" if pressed else "key_release",
                timestamp=_now(),
                keysym=keysym,
                text=text,
                injected=injected,
            )
        )

    def _on_mouse_event(self, code: int, wparam: int, lparam: int) -> int:
        """`WH_MOUSE_LL`'s callback: read the structure, enqueue, return."""
        lib = _user32()
        if code == HC_ACTION:
            # The heartbeat's reading, exactly as in `_on_keyboard_event`.
            self._last_event = time.monotonic()
            # Guarded for `_on_keyboard_event`'s reason: an escaping exception
            # would cost the pointer event the rest of its hook chain.
            with contextlib.suppress(Exception):
                self._record_mouse(wparam, lparam)
        return int(lib.CallNextHookEx(None, code, wparam, lparam))

    def _record_mouse(self, wparam: int, lparam: int) -> None:
        """Read one `MSLLHOOKSTRUCT` and enqueue the event it describes."""
        info = ctypes.cast(lparam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
        raw = self._translate_mouse(wparam, info)
        if raw is not None:
            self._queue.put(raw)

    def _translate_mouse(self, wparam: int, info: Any) -> RawEvent | None:
        """One `MSLLHOOKSTRUCT` -> a `RawEvent`, or None for an unmeant message."""
        now = _now()
        x, y, screen = info.pt.x, info.pt.y, self.screen
        injected = bool(info.flags & LLMHF_INJECTED)
        if wparam == WM_MOUSEMOVE:
            return RawEvent(
                kind="motion",
                timestamp=now,
                x=x,
                y=y,
                screen=screen,
                injected=injected,
            )
        if wparam in _BUTTON_DOWN:
            # A press ends a scroll gesture, and the fraction it had not yet
            # turned into a detent goes with it -- see `_WheelAccumulator`.
            self._wheel.discard()
            return RawEvent(
                kind="button_press",
                timestamp=now,
                x=x,
                y=y,
                screen=screen,
                button=_BUTTON_DOWN[wparam],
                injected=injected,
            )
        if wparam in _BUTTON_UP:
            return RawEvent(
                kind="button_release",
                timestamp=now,
                x=x,
                y=y,
                screen=screen,
                button=_BUTTON_UP[wparam],
                injected=injected,
            )
        if wparam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
            if wparam == WM_XBUTTONDOWN:
                # Ditto: a side button is a press like any other.
                self._wheel.discard()
            number = _XBUTTON_NUMBER.get(_high_word(info.mouseData))
            if number is None:
                return None
            return RawEvent(
                kind="button_press" if wparam == WM_XBUTTONDOWN else "button_release",
                timestamp=now,
                x=x,
                y=y,
                screen=screen,
                button=number,
                injected=injected,
            )
        if wparam in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            detents = self._wheel.add(wparam, _signed_high_word(info.mouseData), now)
            if detents == 0:
                # Less than a whole detent so far: pyguitest's scroll() takes
                # whole detents, so the fraction waits in the accumulator for
                # the rest of the gesture -- see `_WheelAccumulator`.
                return None
            dx, dy = (detents, 0) if wparam == WM_MOUSEHWHEEL else (0, detents)
            return RawEvent(
                kind="scroll",
                timestamp=now,
                x=x,
                y=y,
                screen=screen,
                dx=dx,
                dy=dy,
                injected=injected,
            )
        return None

    def events(self) -> Iterator[RawEvent]:
        """Yield captured events until `stop` is called.

        The wait is timed, and that is not a polling habit: on Windows a thread
        blocked in an untimed `queue.get()` is not woken by Ctrl-C, so the
        `KeyboardInterrupt` that ends a recording (and `--help` promises will)
        is held back until some unrelated event arrives. Measured here: a
        signal raised one second in was not delivered for the eight seconds it
        took a safety valve to release the wait, and delivered at one second
        with a quarter-second timeout. A typed Ctrl-C only *looked* fine,
        because its own keystrokes reach this hook and wake the queue;
        `Ctrl+Break`, or a signal sent by another tool, did not.
        """
        while True:
            try:
                item = self._queue.get(timeout=_INTERRUPT_POLL)
            except queue.Empty:
                continue
            if item is _SENTINEL:
                return
            if isinstance(item, Exception):
                raise CaptureUnavailable(str(item)) from item
            yield item

    def drain(self) -> Iterator[RawEvent]:
        """Yield what the hooks have already delivered, without waiting for more.

        `events` blocks, which is right for the recording loop and wrong at the
        end of it: an interrupted run has to be able to keep whatever arrived
        before the interrupt, and waiting for input that is not coming would
        throw it away instead.

        The queue is read once, up to the size it had when iteration began.
        Draining until it is empty would never return on a session that is
        still being used, and everything captured before the interrupt is what
        an interrupted recording is owed: anything after it belongs to whatever
        happens next.

        The end-of-stream marker is put back rather than swallowed, so a reader
        that calls `events` afterwards still terminates. An error from the pump
        is skipped: `events` raises it, but here the run is already over and
        this exists to salvage what arrived before the failure -- raising would
        throw away the events queued behind it, which are the point. The same
        contract as `X11CaptureBackend.drain`.
        """
        for _ in range(self._queue.qsize()):
            try:
                item = self._queue.get_nowait()
            except queue.Empty:  # pragma: no cover - qsize raced a consumer
                return
            if item is _SENTINEL:
                self._queue.put(_SENTINEL)
                return
            if isinstance(item, Exception):
                continue
            yield item

    def stop(self) -> None:
        """Post `WM_QUIT` to the pump thread and wait for it to unwind.

        Posting rather than closing anything directly: both hooks belong to
        the pump thread, and the only safe way to remove a hook is from the
        thread that installed it, inside its own message loop's own
        `finally` -- see `_run`. This call only asks that loop to end.
        """
        thread, self._thread = self._thread, None
        thread_id, self._thread_id = self._thread_id, None
        if thread_id is not None:
            lib = _user32()
            if lib is not None:
                with contextlib.suppress(Exception):
                    lib.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        if thread is not None and thread.is_alive():
            thread.join(timeout=_JOIN_TIMEOUT)
        self._queue.put(_SENTINEL)


def _high_word(value: int) -> int:
    """The upper 16 bits of a 32-bit `mouseData` value, unsigned."""
    return (value >> 16) & 0xFFFF


def _signed_high_word(value: int) -> int:
    """`mouseData`'s high word as the signed 16-bit value the wheel messages want.

    A raw or X-button word is never negative, but a wheel delta scrolled
    toward the user is, and an unsigned reading of it would turn "one detent
    down" into a number in the tens of thousands.
    """
    word = _high_word(value)
    return word - 0x10000 if word >= 0x8000 else word


_WHEEL_GESTURE_GAP = 0.5
"""Seconds without wheel input after which a leftover fraction is dropped.

Longer than the gap between one precision-touchpad report and the next inside a
gesture, and short enough that a fraction left over from one gesture does not
complete a detent for the next one a person makes."""


class _WheelAccumulator:
    """Whole detents out of wheel deltas that arrive in fractions of one.

    A precision touchpad reports a two-finger scroll as a stream of deltas well
    under one detent (`WHEEL_DELTA`, 120) apiece, and each was dropped on its
    own, so touchpad scrolling never reached a recording on Windows at all --
    only a mouse wheel's whole notches did. The deltas are summed per axis
    instead, and every whole detent the running total crosses is emitted, the
    fraction kept for the next report. A change of direction, or a pause longer
    than `_WHEEL_GESTURE_GAP`, starts the sum again, so a leftover fraction
    cannot turn into a detent the person did not scroll. A button press does
    too -- see `discard`.

    Truncation toward zero is `_whole_detents`'s, so the two directions still
    mirror each other.
    """

    def __init__(self) -> None:
        """Start with nothing carried on either axis."""
        self._rest: dict[int, int] = {}
        self._at: dict[int, float] = {}

    def discard(self) -> None:
        """Forget any carried fraction, for a gesture something else ended.

        A press is the interruption `_WHEEL_GESTURE_GAP` cannot see: the
        reports either side of a click are close enough together to look like
        one gesture, so half a detent from before the click completed a detent
        with the report after it -- and the scroll was recorded at the *later*
        coordinates, a point the person had not scrolled at. The button that
        was pressed is what ends that gesture, so the fraction goes with it
        rather than crossing into the next one.
        """
        self._rest.clear()
        self._at.clear()

    def add(self, axis: int, delta: int, now: float) -> int:
        """Add one report on `axis`; return the whole detents it completes."""
        rest = self._rest.get(axis, 0)
        if now - self._at.get(axis, now) > _WHEEL_GESTURE_GAP or rest * delta < 0:
            rest = 0
        total = rest + delta
        detents = _whole_detents(total)
        self._rest[axis] = total - detents * WHEEL_DELTA
        self._at[axis] = now
        return detents


def _whole_detents(delta: int) -> int:
    """A signed wheel delta as whole detents, rounded *toward zero*.

    Truncation rather than `//`, and the difference only shows up going one
    way: `//` floors, so a precision surface reporting half a detent gives 0
    scrolling up and -1 scrolling down. That turns the sub-detent guard above
    into a one-directional one -- a touchpad nudge upward is correctly dropped
    while the same nudge downward is recorded as a full detent nobody made --
    and it over-reports every partial beyond the first, -180 becoming two
    detents where +180 is one. Rounding toward zero makes the two directions
    mirror each other, which is what a caller replaying the script expects.
    """
    magnitude = abs(delta) // WHEEL_DELTA
    return -magnitude if delta < 0 else magnitude


def _now() -> float:
    """The capture clock: monotonic, so a clock adjustment cannot reorder events."""
    return time.monotonic()
