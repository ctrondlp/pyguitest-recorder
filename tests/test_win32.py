"""The win32 capture backend, driven against fakes: no Windows machine here.

`X11CaptureBackend` needs a live X server only to *capture*; everything after
the bytes arrive -- decoding, keysym resolution, coming apart cleanly -- is
tested with synthetic structures and stand-in connections (`test_x11.py`).
This backend has no cross-platform protocol layer underneath it the way
XRecord does: every one of its calls is a real Win32 API, so the fakes here
stand in for `user32`/`kernel32` themselves, the same shape
`tests/test_win32_backend.py` in pyguitest fakes `user32`/`gdi32` for its own
Windows backend, and for the same reason -- a message layout or a flag pair
can be checked with no DLL in sight, and what a real Windows box still has to
confirm is a different, smaller list.

Every fake is installed through pytest's own `monkeypatch`, never by hand, so
a failing assertion still leaves the module's real `_user32`/`_kernel32`
accessors unpatched for the next test.
"""

import ctypes
import threading

import pytest

from pyguitest_recorder.backends import win32 as win32_module
from pyguitest_recorder.backends.base import CaptureBackend, CaptureUnavailable
from pyguitest_recorder.backends.win32 import (
    _KBDLLHOOKSTRUCT,
    _MSLLHOOKSTRUCT,
    _POINT,
    HC_ACTION,
    LLKHF_EXTENDED,
    LLKHF_INJECTED,
    LLMHF_INJECTED,
    WH_KEYBOARD_LL,
    WH_MOUSE_LL,
    WHEEL_DELTA,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MBUTTONDOWN,
    WM_MOUSEHWHEEL,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    WM_QUIT,
    WM_RBUTTONDOWN,
    WM_SYSKEYDOWN,
    WM_XBUTTONDOWN,
    WM_XBUTTONUP,
    Win32CaptureBackend,
    _high_word,
    _KeyboardState,
    _KeyVocabulary,
    _signed_high_word,
    available,
    unavailable_reason,
)

pytestmark = pytest.mark.skipif(
    not win32_module._pyguitest_win32_available(),
    reason=(
        "the installed pyguitest has no backends.win32, so there is no key "
        "vocabulary to build a capture backend against -- see "
        "win32._NO_PYGUITEST_WIN32"
    ),
)
"""Skipped whole where pyguitest predates its own Windows support.

Almost every test here constructs `Win32CaptureBackend`, which builds its key
vocabulary from `pyguitest.backends.win32` -- so on an older pyguitest they
fail at construction for a reason that is about the *dependency*, not about
this backend. CI installs released pyguitest and hit exactly that.

Skipping rather than loosening: these tests are worth nothing if they cannot
build the thing they test, and a skip says which upgrade turns them back on.
"""


# -- fakes ---------------------------------------------------------------


class FakeUser32:
    """Enough of user32 to drive the hook callbacks and the pump thread.

    `GetMessageW` really blocks, on a `threading.Event` rather than a real
    message queue -- `start()`/`stop()` run a genuine background thread in
    these tests, and this is what lets that thread behave the way
    `PostThreadMessageW(WM_QUIT, ...)` documents without a live desktop
    under it.
    """

    def __init__(self, hook_fails=False, text="", layout=0xABCD):
        self.hook_fails = hook_fails
        self.text = text
        self.layout = layout
        self.hooked = []
        self.unhooked = []
        self.next_calls = []
        self.tounicode_flags = []
        self._next_handle = 1
        self.quit_event = threading.Event()

    def SetWindowsHookExW(self, id_hook, proc, _hmod, _thread_id):
        if self.hook_fails:
            return 0
        handle = self._next_handle
        self._next_handle += 1
        self.hooked.append((id_hook, proc, handle))
        return handle

    def hook_proc(self, id_hook):
        """The callback registered for `id_hook`, for a test to call directly."""
        for hooked_id, proc, _handle in self.hooked:
            if hooked_id == id_hook:
                return proc
        raise AssertionError(f"no hook registered for id {id_hook}")

    def UnhookWindowsHookEx(self, handle):
        self.unhooked.append(handle)
        return 1

    def CallNextHookEx(self, _hhk, code, wparam, lparam):
        self.next_calls.append((code, wparam, lparam))
        return 0

    def GetForegroundWindow(self):
        return 0x1234

    def GetWindowThreadProcessId(self, _window, _pid_ptr):
        return 42

    def GetKeyboardLayout(self, _thread_id):
        return self.layout

    def ToUnicodeEx(self, _vk, _scan, _state, buffer, _size, flags, _layout):
        self.tounicode_flags.append(flags)
        if not self.text:
            return 0
        buffer.value = self.text
        return len(self.text)

    def GetMessageW(self, _msg_ptr, _hwnd, _min, _max):
        self.quit_event.wait(timeout=5.0)
        return 0

    def PeekMessageW(self, *_args):
        return 0

    def TranslateMessage(self, *_args):
        return 1

    def DispatchMessageW(self, *_args):
        return 0

    def PostThreadMessageW(self, _thread_id, message, _wparam, _lparam):
        if message == WM_QUIT:
            self.quit_event.set()
        return 1


class FakeKernel32:
    def GetCurrentThreadId(self):
        return 4321


def patch_windows(monkeypatch, fake_user32=None, fake_kernel32=None, stub_desktop=True):
    """Patch `sys.platform`, `_user32` and `_kernel32` for one test.

    `stub_desktop` also answers the interactive-window-station question, so a
    test about hooks is not also a test of the machine it runs on: the real
    `_off_desktop_reason` asks pyguitest, and on a Windows box reached over
    SSH that correctly answers "off the desktop" and would fail every probe
    test here for the one reason that is not a defect. The class that is
    *about* that check passes False and exercises the real thing.
    """
    monkeypatch.setattr(win32_module.sys, "platform", "win32")
    monkeypatch.setattr(win32_module, "_user32", lambda: fake_user32 or FakeUser32())
    monkeypatch.setattr(
        win32_module, "_kernel32", lambda: fake_kernel32 or FakeKernel32()
    )
    if stub_desktop:
        monkeypatch.setattr(win32_module, "_off_desktop_reason", lambda: None)


def mouse_info(x=100, y=200, mouse_data=0, flags=0):
    return _MSLLHOOKSTRUCT(
        pt=_POINT(x, y), mouseData=mouse_data, flags=flags, time=0, dwExtraInfo=None
    )


def keyboard_lparam(vk_code, scan_code=0, flags=0):
    """A `KBDLLHOOKSTRUCT` in memory, and the address a hook receives for it.

    The struct is returned too and must be kept alive by the caller: once it
    is garbage collected, the address a callback casts back to a pointer
    points at freed memory.
    """
    info = _KBDLLHOOKSTRUCT(
        vkCode=vk_code, scanCode=scan_code, flags=flags, time=0, dwExtraInfo=None
    )
    return ctypes.addressof(info), info


def mouse_lparam(x=1, y=2, mouse_data=0, flags=0):
    info = mouse_info(x=x, y=y, mouse_data=mouse_data, flags=flags)
    return ctypes.addressof(info), info


def drain(made):
    """Every `RawEvent` currently sitting in `made`'s internal queue."""
    events = []
    while not made._queue.empty():
        events.append(made._queue.get_nowait())
    return events


# -- pure arithmetic, no DLL involved -----------------------------------------


class TestHighWords:
    def test_high_word_reads_the_upper_16_bits(self):
        assert _high_word(0x0002_0078) == 2

    def test_signed_high_word_is_positive_below_the_midpoint(self):
        assert _signed_high_word(1 << 16) == 1

    def test_signed_high_word_sign_extends_at_and_above_the_midpoint(self):
        # 0xFFFF in the high word is -1 detent, scrolled toward the user --
        # an unsigned reading would answer 65535, wrong by five orders of
        # magnitude for "one detent down".
        assert _signed_high_word(0xFFFF << 16) == -1


# -- mouse translation, no DLL involved ---------------------------------------


class TestTranslateMouse:
    """`_translate_mouse` needs no DLL: it reads only the structure it is given."""

    def test_motion_carries_the_absolute_point_and_the_screen_tag(self):
        made = Win32CaptureBackend(screen=2)
        raw = made._translate_mouse(WM_MOUSEMOVE, mouse_info(x=7, y=8))
        assert raw.kind == "motion"
        assert (raw.x, raw.y, raw.screen) == (7, 8, 2)

    @pytest.mark.parametrize(
        "message,button",
        [(WM_LBUTTONDOWN, 1), (WM_MBUTTONDOWN, 2), (WM_RBUTTONDOWN, 3)],
    )
    def test_primary_buttons_use_pyguitests_own_numbering(self, message, button):
        made = Win32CaptureBackend()
        raw = made._translate_mouse(message, mouse_info())
        assert raw.kind == "button_press"
        assert raw.button == button

    def test_a_button_release_is_reported_as_such(self):
        made = Win32CaptureBackend()
        raw = made._translate_mouse(WM_LBUTTONUP, mouse_info())
        assert raw.kind == "button_release"
        assert raw.button == 1

    def test_xbutton1_is_pyguitests_button_8(self):
        made = Win32CaptureBackend()
        raw = made._translate_mouse(WM_XBUTTONDOWN, mouse_info(mouse_data=1 << 16))
        assert (raw.kind, raw.button) == ("button_press", 8)

    def test_xbutton2_is_pyguitests_button_9(self):
        made = Win32CaptureBackend()
        raw = made._translate_mouse(WM_XBUTTONUP, mouse_info(mouse_data=2 << 16))
        assert (raw.kind, raw.button) == ("button_release", 9)

    def test_an_unknown_xbutton_number_is_dropped_rather_than_guessed(self):
        made = Win32CaptureBackend()
        info = mouse_info(mouse_data=9 << 16)
        assert made._translate_mouse(WM_XBUTTONDOWN, info) is None

    def test_a_vertical_wheel_detent_up_is_positive_dy(self):
        made = Win32CaptureBackend()
        info = mouse_info(mouse_data=WHEEL_DELTA << 16)
        raw = made._translate_mouse(WM_MOUSEWHEEL, info)
        assert (raw.kind, raw.dx, raw.dy) == ("scroll", 0, 1)

    def test_a_vertical_wheel_detent_down_is_negative_dy(self):
        made = Win32CaptureBackend()
        data = (-WHEEL_DELTA & 0xFFFF) << 16
        raw = made._translate_mouse(WM_MOUSEWHEEL, mouse_info(mouse_data=data))
        assert raw.dy == -1

    def test_a_horizontal_wheel_detent_right_is_positive_dx(self):
        made = Win32CaptureBackend()
        info = mouse_info(mouse_data=WHEEL_DELTA << 16)
        raw = made._translate_mouse(WM_MOUSEHWHEEL, info)
        assert (raw.dx, raw.dy) == (1, 0)

    def test_less_than_one_detent_is_dropped_not_rounded_to_zero(self):
        # pyguitest's own scroll() takes whole detents; a fractional report
        # from a precision surface has nothing there to replay.
        made = Win32CaptureBackend()
        raw = made._translate_mouse(WM_MOUSEWHEEL, mouse_info(mouse_data=40 << 16))
        assert raw is None

    def test_less_than_one_detent_downward_is_dropped_the_same_way(self):
        # The guard above has to work in both directions. Flooring made it
        # one-directional: -40 // 120 is -1, so a touchpad nudge toward the
        # user was recorded as a whole detent nobody made, while the identical
        # nudge away from the user was correctly dropped.
        made = Win32CaptureBackend()
        data = (-40 & 0xFFFF) << 16
        raw = made._translate_mouse(WM_MOUSEWHEEL, mouse_info(mouse_data=data))
        assert raw is None

    def test_a_partial_detent_beyond_the_first_rounds_toward_zero_either_way(self):
        # 180 is one and a half detents. Flooring reported one going up and
        # *two* going down, so a scripted scroll came back asymmetric.
        made = Win32CaptureBackend()
        up = made._translate_mouse(WM_MOUSEWHEEL, mouse_info(mouse_data=180 << 16))
        down = made._translate_mouse(
            WM_MOUSEWHEEL, mouse_info(mouse_data=(-180 & 0xFFFF) << 16)
        )
        assert (up.dy, down.dy) == (1, -1)

    def test_an_injected_click_is_marked(self):
        made = Win32CaptureBackend()
        info = mouse_info(flags=LLMHF_INJECTED)
        raw = made._translate_mouse(WM_LBUTTONDOWN, info)
        assert raw.injected is True

    def test_an_ordinary_click_is_not_marked_injected(self):
        made = Win32CaptureBackend()
        raw = made._translate_mouse(WM_LBUTTONDOWN, mouse_info())
        assert raw.injected is False

    def test_an_injected_motion_is_marked(self):
        # The motion branch spelled its event out with five arguments and no
        # sixth, so the one event a replay generates most of came back in the
        # recording claiming to be the user's -- beside clicks that said
        # otherwise.
        made = Win32CaptureBackend()
        info = mouse_info(flags=LLMHF_INJECTED)
        raw = made._translate_mouse(WM_MOUSEMOVE, info)
        assert raw.injected is True

    def test_an_ordinary_motion_is_not_marked_injected(self):
        made = Win32CaptureBackend()
        raw = made._translate_mouse(WM_MOUSEMOVE, mouse_info())
        assert raw.injected is False

    def test_an_unrecognised_message_is_none(self):
        made = Win32CaptureBackend()
        assert made._translate_mouse(0x9999, mouse_info()) is None


# -- keyboard state tracking, no DLL involved ---------------------------------


class TestKeyboardState:
    def test_pressing_a_key_sets_its_own_byte(self):
        state = _KeyboardState()
        state.press(0x41)  # 'A'
        assert state._state[0x41] & 0x80

    def test_pressing_left_shift_also_sets_the_generic_shift_byte(self):
        state = _KeyboardState()
        state.press(0xA0)  # VK_LSHIFT
        assert state._state[0x10] & 0x80  # VK_SHIFT

    def test_releasing_the_only_held_side_clears_the_generic_byte_too(self):
        state = _KeyboardState()
        state.press(0xA0)
        state.release(0xA0)
        assert not state._state[0x10] & 0x80

    def test_releasing_one_side_while_the_other_is_held_keeps_generic_set(self):
        # Left and right Shift held together, then only the left released --
        # ToUnicodeEx must still see Shift as down, or a real chord (both
        # Shifts plus a key, unusual but real) would look unshifted the
        # instant either side lifted.
        state = _KeyboardState()
        state.press(0xA0)  # VK_LSHIFT
        state.press(0xA1)  # VK_RSHIFT
        state.release(0xA0)
        assert state._state[0x10] & 0x80

    def test_capslock_toggles_on_every_press(self):
        state = _KeyboardState()
        state.press(0x14)  # VK_CAPITAL
        assert state._state[0x14] & 0x01
        state.release(0x14)
        state.press(0x14)
        assert not state._state[0x14] & 0x01

    def test_array_reflects_the_current_state(self):
        state = _KeyboardState()
        state.press(0x41)
        buffer = state.array()
        assert buffer[0x41] & 0x80

    def test_array_is_a_fresh_copy_each_call(self):
        state = _KeyboardState()
        first = state.array()
        state.press(0x41)
        assert not (first[0x41] & 0x80)

    def test_an_out_of_range_code_is_ignored_rather_than_raising(self):
        # Every documented VK_* constant fits one byte, but nothing upstream
        # enforces that on a code read out of a hook structure -- and an
        # uncaught IndexError inside a hook callback would abort the rest of
        # that call, CallNextHookEx included, dropping the keystroke from
        # the application it was meant for as well as from this recording.
        state = _KeyboardState()
        state.press(300)
        state.release(300)
        state.press(-1)


# -- the keysym vocabulary, no DLL involved -----------------------------------


class TestKeyVocabulary:
    def test_a_named_key_gets_x11s_real_spelling(self):
        vocab = _KeyVocabulary()
        assert vocab.name(vocab._vk["VK_BACK"], extended=False) == "BackSpace"
        assert vocab.name(vocab._vk["VK_LCONTROL"], extended=False) == "Control_L"
        assert vocab.name(vocab._vk["VK_LWIN"], extended=False) == "Super_L"

    def test_function_keys_are_capitalised(self):
        vocab = _KeyVocabulary()
        assert vocab.name(vocab._vk["VK_F5"], extended=False) == "F5"

    def test_the_main_return_is_return(self):
        vocab = _KeyVocabulary()
        assert vocab.name(vocab._vk["VK_RETURN"], extended=False) == "Return"

    def test_the_extended_return_is_the_numeric_keypads_enter(self):
        # Only the scan code's extended bit tells the two apart -- both
        # share VK_RETURN, the one virtual key with no single physical key.
        vocab = _KeyVocabulary()
        assert vocab.name(vocab._vk["VK_RETURN"], extended=True) == "KP_Enter"

    def test_a_letter_falls_back_to_pyguitests_own_table(self):
        vocab = _KeyVocabulary()
        assert vocab.name(vocab._vk["VK_A"], extended=False) == "a"

    def test_an_unnamed_code_falls_back_to_hex(self):
        vocab = _KeyVocabulary()
        assert vocab.name(0xFE, extended=False) == "0xfe"

    def test_modifier_names_match_what_normalize_pys_modifiers_dict_expects(self):
        # The one thing this table exists for: analyzer/normalize.py's
        # MODIFIERS dict is keyed by X11's real, mixed-case spelling, and a
        # lower-cased "control_l" (pyguitest's own internal spelling) would
        # make every Windows-recorded Ctrl-chord invisible to it.
        from pyguitest_recorder.analyzer.normalize import MODIFIERS

        vocab = _KeyVocabulary()
        pairs = [
            ("VK_LCONTROL", "ctrl"),
            ("VK_RCONTROL", "ctrl"),
            ("VK_LSHIFT", "shift"),
            ("VK_RSHIFT", "shift"),
            ("VK_LMENU", "alt"),
            ("VK_RMENU", "alt"),
            ("VK_LWIN", "meta"),
            ("VK_RWIN", "meta"),
        ]
        for vk_name, expected in pairs:
            keysym = vocab.name(vocab._vk[vk_name], extended=False)
            assert MODIFIERS.get(keysym) == expected, keysym


# -- availability --------------------------------------------------------


class TestAvailability:
    def test_off_windows_the_reason_names_the_platform(self, monkeypatch):
        monkeypatch.setattr(win32_module.sys, "platform", "linux")
        assert "not a native Windows process" in unavailable_reason()
        assert available() is False

    def test_a_missing_user32_is_its_own_reason(self, monkeypatch):
        monkeypatch.setattr(win32_module.sys, "platform", "win32")
        monkeypatch.setattr(win32_module, "_user32", lambda: None)
        assert "user32.dll did not load" in unavailable_reason()

    def test_a_refused_hook_install_is_reported(self, monkeypatch):
        fake = FakeUser32(hook_fails=True)
        patch_windows(monkeypatch, fake_user32=fake)
        assert "SetWindowsHookExW refused" in unavailable_reason()

    def test_a_real_probe_installs_and_removes_the_hook(self, monkeypatch):
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake)
        assert unavailable_reason() is None
        assert fake.unhooked, "the probe hook must be removed, not merely installed"


# -- the hook callbacks, through a fake user32 --------------------------------


class TestThePyguitestDependency:
    """An older pyguitest is a reason, not a ModuleNotFoundError.

    This backend reads its key vocabulary -- `VK` and
    `key_name_for_virtual_key` -- from `pyguitest.backends.win32`, which
    arrived with pyguitest's own Windows support and is in no release before
    it. Without this guard the miss surfaced as a bare `ModuleNotFoundError`
    four frames inside a backend constructor, naming nothing a reader could
    act on. It is also what CI hits: the suite installs released pyguitest.
    """

    def test_a_pyguitest_without_win32_is_reported_as_the_reason(self, monkeypatch):
        patch_windows(monkeypatch)
        monkeypatch.setattr(win32_module, "_pyguitest_win32_available", lambda: False)
        reason = unavailable_reason()
        assert reason is not None
        assert "pyguitest" in reason
        assert "upgrade" in reason.lower()
        assert not available()

    def test_the_reason_is_given_before_the_window_station_is_probed(self, monkeypatch):
        # Checked ahead of user32 deliberately: it is equally true on a
        # machine where the desktop is perfect, and reporting the hook
        # failure instead would send a reader looking at their session.
        fake = FakeUser32(hook_fails=True)
        patch_windows(monkeypatch, fake_user32=fake)
        monkeypatch.setattr(win32_module, "_pyguitest_win32_available", lambda: False)
        assert "pyguitest" in unavailable_reason()
        assert not fake.hooked, "no hook should be installed to answer this"

    def test_the_vocabulary_raises_a_typed_error_rather_than_an_import_error(
        self, monkeypatch
    ):
        # The paths that reach the import anyway still owe a caller a typed
        # error -- CaptureUnavailable is what every other refusal here uses.
        def _missing(_name):
            raise ImportError("no pyguitest.backends.win32 here")

        monkeypatch.setattr(win32_module.importlib.util, "find_spec", _missing)
        assert not win32_module._pyguitest_win32_available()


class TestCallbackIsolation:
    """An exception inside a hook must never cost the chain its event.

    A Python exception escaping a `ctypes` callback does not reach any caller:
    ctypes prints the traceback and returns 0, so `CallNextHookEx` is skipped
    and the remaining hooks never see that keystroke. The person at the
    keyboard loses the key they pressed -- a far worse failure than the
    recorder missing one event.
    """

    def test_a_raising_keyboard_step_still_reaches_call_next_hook(self, monkeypatch):
        fake = FakeUser32(text="a")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        monkeypatch.setattr(
            made, "_record_key", lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        lparam, _info = keyboard_lparam(0x41)
        made._on_keyboard_event(HC_ACTION, WM_KEYDOWN, lparam)
        assert fake.next_calls, "CallNextHookEx was skipped, so the key was swallowed"

    def test_a_raising_mouse_step_still_reaches_call_next_hook(self, monkeypatch):
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        monkeypatch.setattr(
            made,
            "_record_mouse",
            lambda *a: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        lparam, _info = mouse_lparam()
        made._on_mouse_event(HC_ACTION, WM_MOUSEMOVE, lparam)
        assert fake.next_calls, "CallNextHookEx was skipped, so the move was swallowed"

    def test_a_non_action_code_is_passed_straight_through(self, monkeypatch):
        # The platform's own rule for every hook procedure: any nCode other
        # than HC_ACTION must reach CallNextHookEx untouched.
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0x41)
        made._on_keyboard_event(-1, WM_KEYDOWN, lparam)
        assert fake.next_calls == [(-1, WM_KEYDOWN, lparam)]
        assert drain(made) == []


class TestKeyboardCallback:
    def test_a_keydown_enqueues_a_key_press_with_text(self, monkeypatch):
        fake = FakeUser32(text="a")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0x41)
        made._on_keyboard_event(HC_ACTION, WM_KEYDOWN, lparam)
        (raw,) = drain(made)
        assert raw.kind == "key_press"
        assert raw.keysym == "a"
        assert raw.text == "a"

    def test_the_layout_is_read_without_consuming_its_dead_key_state(self, monkeypatch):
        # Bit 2 of wFlags, and the reason it is not a detail: with no flags
        # ToUnicodeEx *consumes* the pending dead key, which is the layout's
        # own kernel-mode state, not this process's. This hook runs ahead of
        # the application the keystroke is going to, so without the flag a
        # person typing `'` then `e` on an international layout gets `'e` in
        # their editor instead of `é` -- the recorder altering exactly what it
        # exists to observe.
        from pyguitest_recorder.backends.win32 import (
            _TOUNICODE_NO_KEYBOARD_STATE_CHANGE,
        )

        fake = FakeUser32(text="a")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0x41)
        made._on_keyboard_event(HC_ACTION, WM_KEYDOWN, lparam)
        assert fake.tounicode_flags == [_TOUNICODE_NO_KEYBOARD_STATE_CHANGE]

    def test_a_non_printing_key_carries_no_text(self, monkeypatch):
        fake = FakeUser32(text="")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0x0D)  # VK_RETURN
        made._on_keyboard_event(HC_ACTION, WM_KEYDOWN, lparam)
        (raw,) = drain(made)
        assert raw.keysym == "Return"
        assert raw.text == ""

    def test_a_keyup_carries_no_text_even_if_toUnicodeEx_would_answer(
        self, monkeypatch
    ):
        # Only key-down calls ToUnicodeEx; a release is not text, and calling
        # it twice per physical keystroke would double the dead-key risk for
        # no consumer -- _on_key_release in normalize.py never reads
        # raw.text at all.
        fake = FakeUser32(text="a")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0x41)
        made._on_keyboard_event(HC_ACTION, WM_KEYUP, lparam)
        (raw,) = drain(made)
        assert raw.kind == "key_release"
        assert raw.text == ""

    def test_a_syskeydown_is_still_a_key_press(self, monkeypatch):
        fake = FakeUser32(text="")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0x09)  # VK_TAB, held with Alt
        made._on_keyboard_event(HC_ACTION, WM_SYSKEYDOWN, lparam)
        (raw,) = drain(made)
        assert raw.kind == "key_press"
        assert raw.keysym == "Tab"

    def test_an_injected_keystroke_is_marked(self, monkeypatch):
        fake = FakeUser32(text="")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0x1B, flags=LLKHF_INJECTED)  # VK_ESCAPE
        made._on_keyboard_event(HC_ACTION, WM_KEYDOWN, lparam)
        (raw,) = drain(made)
        assert raw.injected is True

    def test_the_extended_flag_picks_the_numeric_keypads_enter(self, monkeypatch):
        fake = FakeUser32(text="")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0x0D, flags=LLKHF_EXTENDED)
        made._on_keyboard_event(HC_ACTION, WM_KEYDOWN, lparam)
        (raw,) = drain(made)
        assert raw.keysym == "KP_Enter"

    def test_a_vk_packet_keydown_carries_the_injected_character_as_text(
        self, monkeypatch
    ):
        # Found live: a real win32 capture run recording `type_text("Ada")`
        # produced a raw `key_press "0xe7"` with no text at all, and the
        # generated script replayed `gui.tap_key("0xe7")` three times instead
        # of typing anything. VK_PACKET (0xE7) is what SendInput's
        # KEYEVENTF_UNICODE arrives as -- IME composition, an on-screen
        # keyboard, and remote-input tools all go through it too, not only a
        # synthetic probe -- and `ToUnicodeEx` cannot translate it: it maps a
        # virtual key through the keyboard layout, and no layout defines
        # VK_PACKET. The character was never missing, just unread: it sits in
        # `scanCode` verbatim. `text=""` on the fake proves this does not
        # route through `ToUnicodeEx` at all for this vk.
        fake = FakeUser32(text="")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0xE7, scan_code=ord("A"))
        made._on_keyboard_event(HC_ACTION, WM_KEYDOWN, lparam)
        (raw,) = drain(made)
        assert raw.kind == "key_press"
        assert raw.text == "A"
        assert fake.tounicode_flags == []

    def test_a_vk_packet_keyup_carries_no_text(self, monkeypatch):
        fake = FakeUser32(text="")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0xE7, scan_code=ord("A"))
        made._on_keyboard_event(HC_ACTION, WM_KEYUP, lparam)
        (raw,) = drain(made)
        assert raw.kind == "key_release"
        assert raw.text == ""

    def test_a_character_outside_the_bmp_arrives_as_two_packets_and_as_one_text(
        self, monkeypatch
    ):
        # Anything above U+FFFF is two UTF-16 code units, and SendInput sends
        # each as a VK_PACKET keystroke of its own. `chr()` on a half is a
        # lone surrogate -- not the character, and not something Python can
        # write: `"\uD83D" + "\uDE00"` is two unpaired halves rather than one
        # code point, so the generated script carried an ill-formed
        # `gui.type_text(...)` and `--regenerate` died with UnicodeEncodeError
        # writing the file it had just built.
        units = "\U0001f600".encode("utf-16-le")
        high = int.from_bytes(units[:2], "little")
        low = int.from_bytes(units[2:], "little")
        fake = FakeUser32(text="")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        for unit in (high, low):
            lparam, _info = keyboard_lparam(0xE7, scan_code=unit)
            made._on_keyboard_event(HC_ACTION, WM_KEYDOWN, lparam)
        presses = drain(made)
        # The high half is held back until it has a pair to become, and is not
        # enqueued as a press of its own: the low half carries the character.
        assert [raw.kind for raw in presses] == ["key_press"]
        assert [raw.text for raw in presses] == ["\U0001f600"]

    def test_a_surrogate_pair_is_one_text_input_and_no_keystroke(self, monkeypatch):
        # Found by review, not by a live run -- which typed no emoji: the high
        # half was enqueued with no text, and the normalizer turns any
        # text-less, non-modifier press into a KeyStroke. The recording then
        # carried `KeyStroke("U+D83D")` ahead of the TextInput, and the script
        # `gui.tap_key("U+D83D")`, a key name pyguitest rejects at replay. The
        # backend tests above never reached the normalizer, which is why a
        # test pinned the half-press and nothing noticed.
        import dataclasses

        from pyguitest_recorder.analyzer import Normalizer
        from pyguitest_recorder.model import KeyStroke, TextInput
        from pyguitest_recorder.windows import NullResolver

        units = "\U0001f600".encode("utf-16-le")
        fake = FakeUser32(text="")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        for index in (0, 2):
            unit = int.from_bytes(units[index : index + 2], "little")
            lparam, _info = keyboard_lparam(0xE7, scan_code=unit)
            made._on_keyboard_event(HC_ACTION, WM_KEYDOWN, lparam)
            made._on_keyboard_event(HC_ACTION, WM_KEYUP, lparam)
        normalizer = Normalizer(resolver=NullResolver(), started=0.0)
        events = []
        for step, raw in enumerate(drain(made)):
            events += normalizer.feed(dataclasses.replace(raw, timestamp=0.05 * step))
        events += normalizer.flush()
        assert [type(event) for event in events] == [TextInput]
        assert events[0].text == "\U0001f600"
        assert not any(isinstance(event, KeyStroke) for event in events)

    def test_a_high_half_with_no_pair_is_dropped_rather_than_typed(self, monkeypatch):
        # A half of a pair is not a character, and passing one on is what made
        # the script unwritable. The ordinary key that follows says the pair
        # was never coming -- an IME or an emoji picker sends both halves
        # together, in one SendInput call.
        fake = FakeUser32(text="A")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0xE7, scan_code=0xD83D)
        made._on_keyboard_event(HC_ACTION, WM_KEYDOWN, lparam)
        lparam, _info = keyboard_lparam(0x41)
        made._on_keyboard_event(HC_ACTION, WM_KEYDOWN, lparam)
        presses = drain(made)
        # Neither the dropped half nor anything else stands in for it.
        assert [raw.text for raw in presses] == ["A"]

    def test_a_low_half_on_its_own_is_not_text(self, monkeypatch):
        fake = FakeUser32(text="")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0xE7, scan_code=0xDE00)
        made._on_keyboard_event(HC_ACTION, WM_KEYDOWN, lparam)
        # Not text, and not a key either: nothing is enqueued for it.
        assert drain(made) == []

    def test_call_next_hook_ex_always_runs(self, monkeypatch):
        # The one rule every hook procedure on the platform follows, and the
        # one this callback must not skip even on an ordinary keystroke: the
        # message still has to reach the application it was meant for.
        fake = FakeUser32(text="")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0x41)
        made._on_keyboard_event(HC_ACTION, WM_KEYDOWN, lparam)
        assert fake.next_calls == [(HC_ACTION, WM_KEYDOWN, lparam)]

    def test_a_negative_ncode_is_passed_through_untranslated(self, monkeypatch):
        fake = FakeUser32(text="a")
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = keyboard_lparam(0x41)
        made._on_keyboard_event(-1, WM_KEYDOWN, lparam)
        assert made._queue.empty()
        assert len(fake.next_calls) == 1


class TestMouseCallback:
    def test_a_click_reaches_the_queue_and_call_next_hook_ex(self, monkeypatch):
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = mouse_lparam(x=5, y=6)
        made._on_mouse_event(HC_ACTION, WM_LBUTTONDOWN, lparam)
        (raw,) = drain(made)
        assert (raw.kind, raw.x, raw.y) == ("button_press", 5, 6)
        assert len(fake.next_calls) == 1

    def test_a_message_this_backend_carries_no_meaning_for_enqueues_nothing(
        self, monkeypatch
    ):
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = mouse_lparam(mouse_data=99 << 16)
        made._on_mouse_event(HC_ACTION, 0x9999, lparam)
        assert made._queue.empty()

    def test_a_negative_ncode_is_passed_through_untranslated(self, monkeypatch):
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        lparam, _info = mouse_lparam()
        made._on_mouse_event(-1, WM_LBUTTONDOWN, lparam)
        assert made._queue.empty()
        assert len(fake.next_calls) == 1


# -- the real pump thread, start/stop/events end to end -----------------------


class TestStartStopEvents:
    def test_a_failed_hook_install_raises_and_leaks_nothing(self, monkeypatch):
        fake = FakeUser32(hook_fails=True)
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        with pytest.raises(CaptureUnavailable):
            made.start()
        assert fake.unhooked == []  # nothing was ever installed to unhook

    def test_a_mouse_hook_failure_unhooks_the_keyboard_hook_that_installed(
        self, monkeypatch
    ):
        # The regression this guards: the first draft of _run only unhooked
        # inside the message loop's own finally, so a keyboard hook that
        # installed fine while the mouse hook failed right after it was
        # never removed at all.
        fake = FakeUser32()
        real_install = fake.SetWindowsHookExW
        calls = {"n": 0}

        def flaky_install(id_hook, proc, hmod, thread_id):
            calls["n"] += 1
            if id_hook == WH_MOUSE_LL:
                return 0
            return real_install(id_hook, proc, hmod, thread_id)

        fake.SetWindowsHookExW = flaky_install
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        with pytest.raises(CaptureUnavailable):
            made.start()
        assert fake.unhooked == [1]  # the keyboard hook that did install
        assert calls["n"] == 2  # stopped after the mouse hook failed

    def test_start_events_stop_round_trips_one_click(self, monkeypatch):
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        made.start()
        try:
            proc = fake.hook_proc(WH_MOUSE_LL)
            lparam, _info = mouse_lparam(x=10, y=20)
            # Deliver the click straight to the installed callback, exactly
            # as Windows would from inside GetMessageW -- the fake's own
            # GetMessageW just blocks, it never generates input on its own.
            proc(HC_ACTION, WM_LBUTTONDOWN, lparam)
        finally:
            made.stop()
        events = list(made.events())
        assert [e.kind for e in events] == ["button_press"]
        assert (events[0].x, events[0].y) == (10, 20)

    def test_stop_before_start_does_not_raise(self):
        made = Win32CaptureBackend()
        made.stop()  # never started; must be a no-op, not an AttributeError

    def test_events_ends_once_stop_completes(self, monkeypatch):
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        made.start()
        made.stop()
        assert list(made.events()) == []

    def test_both_hooks_are_installed_with_no_dll_and_thread_zero(self, monkeypatch):
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        made.start()
        made.stop()
        ids = {hook_id for hook_id, _proc, _handle in fake.hooked}
        assert ids == {WH_KEYBOARD_LL, WH_MOUSE_LL}

    def test_both_hooks_are_unhooked_on_a_clean_stop(self, monkeypatch):
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        made.start()
        made.stop()
        assert sorted(fake.unhooked) == [1, 2]


class TestTheCaptureBackendProtocol:
    """This backend offers everything `Recorder` asks a capture backend for."""

    def test_it_is_a_capture_backend(self):
        # `CaptureBackend` grew `drain` and `stop_pressed_at` with the work that
        # made a run that fell behind live input recoverable, and this backend
        # was not given them: an interrupted recording on Windows would have died
        # with an AttributeError after it had been made. Nothing on Linux could
        # see it, because type-checking there skips the `sys.platform == "win32"`
        # branch that builds this class. The protocol is runtime-checkable for
        # exactly this, so whatever is added to it next is checked here without
        # anyone having to remember to.
        assert isinstance(Win32CaptureBackend(), CaptureBackend)

    def test_it_does_not_recognise_the_stop_chord_at_capture(self):
        # Said out loud rather than left as an absence: the consumer's own
        # recogniser ends a recording here, and `Recorder` reads this to decide
        # which note a recording that fell behind should carry.
        assert Win32CaptureBackend().stop_pressed_at is None


class TestDrain:
    """What an interrupted run is owed: whatever the hooks already delivered."""

    def test_returns_what_the_hooks_already_delivered_without_waiting(
        self, monkeypatch
    ):
        # Delivered through the installed callbacks, as Windows would from inside
        # `GetMessageW`, and drained while the backend is still running -- which
        # is the state an interrupted recording is in. `events` would block here
        # waiting for input that is not coming.
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        made.start()
        try:
            proc = fake.hook_proc(WH_MOUSE_LL)
            for x, message in ((10, WM_LBUTTONDOWN), (10, WM_LBUTTONUP)):
                lparam, _info = mouse_lparam(x=x, y=20)
                proc(HC_ACTION, message, lparam)
            drained = list(made.drain())
        finally:
            made.stop()
        assert [e.kind for e in drained] == ["button_press", "button_release"]

    def test_a_backend_with_nothing_buffered_yields_nothing_and_returns(self):
        assert list(Win32CaptureBackend().drain()) == []

    def test_takes_only_what_was_queued_when_it_was_asked(self, monkeypatch):
        # A session that keeps being used must not be able to extend the drain:
        # what is owed is what had already arrived, not what arrives next.
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        made.start()
        try:
            proc = fake.hook_proc(WH_MOUSE_LL)
            lparam, _info = mouse_lparam(x=1, y=2)
            proc(HC_ACTION, WM_LBUTTONDOWN, lparam)
            assert [e.kind for e in made.drain()] == ["button_press"]
            proc(HC_ACTION, WM_LBUTTONUP, lparam)
            assert [e.kind for e in made.drain()] == ["button_release"]
        finally:
            made.stop()

    def test_leaves_the_end_of_stream_marker_for_a_reader(self):
        # Swallowing the sentinel would leave a later `events` call blocked
        # forever on a queue nothing else will ever fill.
        made = Win32CaptureBackend()
        made.stop()
        assert list(made.drain()) == []
        assert list(made.events()) == []

    def test_skips_the_pumps_own_error_and_keeps_what_is_behind_it(self, monkeypatch):
        # `events` raises the pump's failure, which is right while it is running.
        # At the end of a run there is nothing left to raise it for, and the
        # events queued behind it are the whole reason to drain.
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake)
        made = Win32CaptureBackend()
        made.start()
        try:
            made._queue.put(RuntimeError("the hook thread died"))
            proc = fake.hook_proc(WH_MOUSE_LL)
            lparam, _info = mouse_lparam(x=3, y=4)
            proc(HC_ACTION, WM_LBUTTONDOWN, lparam)
            drained = list(made.drain())
        finally:
            made.stop()
        assert [e.kind for e in drained] == ["button_press"]


class TestTheInteractiveDesktopCheck:
    """Installing a hook is not evidence that it can observe anything.

    A hook is scoped to the window station and desktop of the thread that
    installs it, so a process off the interactive desktop installs one
    against a desktop nobody is using. Measured over SSH on Windows 11: the
    probe installed cleanly and `--doctor` printed "ready to record" for a
    process that could not capture a thing.
    """

    def _reason(self, monkeypatch, interactive):
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake, stub_desktop=False)

        class _Env:
            is_interactive_desktop = interactive

        class _Pyguitest:
            @staticmethod
            def detect():
                return _Env()

        monkeypatch.setitem(__import__("sys").modules, "pyguitest", _Pyguitest)
        return unavailable_reason()

    def test_an_installed_hook_off_the_desktop_is_still_a_refusal(self, monkeypatch):
        reason = self._reason(monkeypatch, interactive=False)
        assert reason is not None
        assert "interactive window station" in reason
        assert not available()

    def test_the_interactive_desktop_can_record(self, monkeypatch):
        assert self._reason(monkeypatch, interactive=True) is None

    def test_a_probe_that_cannot_tell_does_not_refuse(self, monkeypatch):
        # detect() reports True where it cannot tell, and a recorder refusing
        # on "cannot tell" would be worse than one that tries.
        patch_windows(monkeypatch, fake_user32=FakeUser32(), stub_desktop=False)

        class _Broken:
            @staticmethod
            def detect():
                raise RuntimeError("probe exploded")

        monkeypatch.setitem(__import__("sys").modules, "pyguitest", _Broken)
        assert unavailable_reason() is None

    def test_a_pyguitest_without_the_field_does_not_refuse(self, monkeypatch):
        # The field arrived with pyguitest's own Windows support and is in no
        # release yet, so an installed pyguitest can be missing it while being
        # otherwise perfectly usable -- and so can the type a checker reads off
        # the floor this package declares, which is how this surfaced: as a
        # lint failure rather than as a recording that refused. A field that is
        # not there is "cannot tell", the same answer as a probe that raises.
        patch_windows(monkeypatch, fake_user32=FakeUser32(), stub_desktop=False)

        class _OlderPyguitest:
            @staticmethod
            def detect():
                return object()

        monkeypatch.setitem(__import__("sys").modules, "pyguitest", _OlderPyguitest)
        assert unavailable_reason() is None

    def test_start_asks_the_question_too_and_not_only_the_selector(self, monkeypatch):
        # `unavailable_reason` answers at *selection* time, and a backend can
        # be selected on a real desktop and started somewhere else: RDP
        # dropping to a disconnected session is enough, and nothing about
        # `Win32CaptureBackend` requires that anything selected it at all. A
        # hook that installs off the interactive desktop succeeds and then
        # reports not one keystroke, so capture that starts and cannot capture
        # is the one failure with no symptom to read afterwards.
        fake = FakeUser32()
        patch_windows(monkeypatch, fake_user32=fake, stub_desktop=False)
        monkeypatch.setattr(
            win32_module,
            "_off_desktop_reason",
            lambda: "this process is not attached to the interactive window station",
        )
        made = Win32CaptureBackend()
        with pytest.raises(CaptureUnavailable, match="interactive window station"):
            made.start()
        assert fake.hooked == []  # nothing was installed to see nothing with
