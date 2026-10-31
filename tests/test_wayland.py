"""The Wayland/evdev capture path, with most of it driven on any machine.

Like tests/test_macos.py, the decode half -- turning a kernel event into a
`RawEvent`, naming a key by its keysym, accumulating relative motion, dropping
autorepeat -- needs no hardware and runs everywhere `evdev` imports. Only the
round-trip tests need real device nodes (a virtual `uinput` device read back
through `/dev/input`), and they skip when the `input`-group permission tier is
absent, the way the formatter-dependent tests skip without `ruff`.

The keysym table test reaches across repositories: a recording's key names are
only useful if the pyguitest that replays them can press them.
"""

from __future__ import annotations

import contextlib
import glob
import os
import subprocess
import sys
import textwrap
import time

import pytest

# `importorskip` only catches ImportError, but FreeBSD ships an `evdev` whose
# `ecodes` raises KeyError at import (a partial build), and this whole file is
# Linux-only -- so skip the module on *any* evdev failure, including the import
# of `_evdev_keysyms`, which does its own module-scope `from evdev import`.
try:
    import evdev  # noqa: F401
    from evdev import ecodes as e

    from pyguitest_recorder.backends._evdev_keysyms import keysym_and_text
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"evdev is not usable here: {exc}", allow_module_level=True)

from pyguitest_recorder.backends import wayland as wayland_module  # noqa: E402
from pyguitest_recorder.backends.base import CaptureBackend  # noqa: E402
from pyguitest_recorder.backends.wayland import WaylandCaptureBackend  # noqa: E402


class _FakeEvent:
    """A kernel input_event the decoder can read, with no device behind it."""

    def __init__(self, type_, code, value, ts=1000.0):
        self.type, self.code, self.value, self._ts = type_, code, value, ts

    def timestamp(self):
        return self._ts


def _drain(backend):
    return list(backend.drain())


def _feed(backend, *events):
    for ev in events:
        backend._decode(e, ev)


class TestProtocol:
    def test_satisfies_capture_backend_protocol(self):
        assert isinstance(WaylandCaptureBackend([]), CaptureBackend)

    def test_name_is_stable(self):
        assert WaylandCaptureBackend([]).name == "wayland-evdev"


class TestKeyDecode:
    def test_letter_key_press_carries_keysym_and_text(self):
        b = WaylandCaptureBackend([])
        _feed(b, _FakeEvent(e.EV_KEY, e.KEY_H, 1))
        (ev,) = _drain(b)
        assert ev.kind == "key_press"
        assert ev.keysym == "h"
        assert ev.text == "h"

    def test_release_has_no_text(self):
        b = WaylandCaptureBackend([])
        _feed(b, _FakeEvent(e.EV_KEY, e.KEY_H, 1), _FakeEvent(e.EV_KEY, e.KEY_H, 0))
        press, release = _drain(b)
        assert release.kind == "key_release"
        assert release.keysym == "h"
        assert release.text == ""

    def test_held_shift_uppercases_text_and_keeps_keysym(self):
        b = WaylandCaptureBackend([])
        _feed(
            b,
            _FakeEvent(e.EV_KEY, e.KEY_LEFTSHIFT, 1),
            _FakeEvent(e.EV_KEY, e.KEY_2, 1),
        )
        shift, two = _drain(b)
        assert shift.keysym == "Shift_L"
        assert two.keysym == "2"
        assert two.text == "@", "shift+2 is the character @"

    def test_named_keys_have_keysym_but_no_text(self):
        b = WaylandCaptureBackend([])
        _feed(
            b, _FakeEvent(e.EV_KEY, e.KEY_ENTER, 1), _FakeEvent(e.EV_KEY, e.KEY_ESC, 1)
        )
        enter, esc = _drain(b)
        assert (enter.keysym, enter.text) == ("Return", "")
        assert (esc.keysym, esc.text) == ("Escape", "")

    def test_autorepeat_is_dropped(self):
        b = WaylandCaptureBackend([])
        _feed(
            b,
            _FakeEvent(e.EV_KEY, e.KEY_A, 1),
            _FakeEvent(e.EV_KEY, e.KEY_A, 2),  # autorepeat
            _FakeEvent(e.EV_KEY, e.KEY_A, 2),
        )
        assert [ev.kind for ev in _drain(b)] == ["key_press"]


class TestPointerDecode:
    def test_buttons_map_to_x_numbers(self):
        b = WaylandCaptureBackend([])
        _feed(
            b,
            _FakeEvent(e.EV_KEY, e.BTN_LEFT, 1),
            _FakeEvent(e.EV_KEY, e.BTN_RIGHT, 1),
            _FakeEvent(e.EV_KEY, e.BTN_MIDDLE, 1),
        )
        assert [ev.button for ev in _drain(b)] == [1, 3, 2]

    def test_relative_motion_accumulates_and_flushes_on_syn(self):
        b = WaylandCaptureBackend([])
        _feed(
            b,
            _FakeEvent(e.EV_REL, e.REL_X, 10),
            _FakeEvent(e.EV_REL, e.REL_Y, -4),
            _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0),
        )
        (ev,) = _drain(b)
        assert ev.kind == "motion"
        assert (ev.dx, ev.dy) == (10, -4)

    def test_absolute_motion_reports_x_y(self):
        b = WaylandCaptureBackend([])
        _feed(
            b,
            _FakeEvent(e.EV_ABS, e.ABS_X, 300),
            _FakeEvent(e.EV_ABS, e.ABS_Y, 200),
            _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0),
        )
        (ev,) = _drain(b)
        assert ev.kind == "motion"
        assert (ev.x, ev.y) == (300, 200)

    def test_wheel_is_a_scroll(self):
        b = WaylandCaptureBackend([])
        _feed(
            b,
            _FakeEvent(e.EV_REL, e.REL_WHEEL, 1),
            _FakeEvent(e.EV_REL, e.REL_WHEEL, -1),
        )
        assert [ev.dy for ev in _drain(b)] == [1, -1]

    def test_a_hi_res_wheel_detent_is_not_counted_twice(self):
        # A modern mouse emits BOTH codes for one physical detent: the classic
        # code with value 1 and the _HI_RES code with value 120. Honouring
        # both recorded -- and replayed -- a 121-detent scroll per notch.
        b = WaylandCaptureBackend([])
        _feed(
            b,
            _FakeEvent(e.EV_REL, e.REL_WHEEL_HI_RES, 120),
            _FakeEvent(e.EV_REL, e.REL_WHEEL, 1),
            _FakeEvent(e.EV_REL, e.REL_HWHEEL_HI_RES, -120),
            _FakeEvent(e.EV_REL, e.REL_HWHEEL, -1),
        )
        assert [(ev.dx, ev.dy) for ev in _drain(b)] == [(0, 1), (-1, 0)]

    def test_an_unmapped_button_code_is_dropped_not_decoded_as_a_key(self):
        # BTN_TOUCH, BTN_TOOL_FINGER and friends have no X button mapping;
        # running them through the keysym decoder fabricated key_press events
        # with garbage keysyms ("0x0", "BTN_TOUCH") that generate
        # un-replayable tap_key calls.
        b = WaylandCaptureBackend([])
        _feed(
            b,
            _FakeEvent(e.EV_KEY, e.BTN_TOUCH, 1),
            _FakeEvent(e.EV_KEY, e.BTN_TOOL_FINGER, 1),
            _FakeEvent(e.EV_KEY, e.BTN_TOUCH, 0),
        )
        assert _drain(b) == []


class TestClockRebase:
    def test_timestamps_are_rebased_by_the_offset_start_captures(self):
        # The kernel stamps evdev events with CLOCK_REALTIME; the pipeline's
        # clock is time.monotonic (`recorder._now`). The offset start()
        # captures moves every stamp into that domain -- without it the
        # analyzer's offsets, --regenerate trimming and the capture-lag
        # accounting (the password focus-lag gate included) all read epoch
        # seconds against a monotonic start and break.
        b = WaylandCaptureBackend([])
        b._clock_offset = -5.0
        _feed(b, _FakeEvent(e.EV_KEY, e.KEY_H, 1, ts=1000.0))
        (ev,) = _drain(b)
        assert ev.timestamp == 995.0

    def test_fabricated_timestamps_pass_through_before_start(self):
        # The offset is captured in start(); a backend that never started (how
        # every decode test here drives it) must not shift fed timestamps.
        b = WaylandCaptureBackend([])
        _feed(b, _FakeEvent(e.EV_KEY, e.KEY_H, 1, ts=1000.0))
        (ev,) = _drain(b)
        assert ev.timestamp == 1000.0

    def test_a_device_stamped_monotonically_by_the_kernel_is_not_rebased(self):
        # Where EVIOCSCLOCKID took, the kernel already stamps in the
        # pipeline's clock and adding the offset would shift it right back
        # out of that domain.
        b = WaylandCaptureBackend([])
        b._clock_offset = -5.0
        b._monotonic_fds.add(7)
        b._decode(e, _FakeEvent(e.EV_KEY, e.KEY_H, 1, ts=1000.0), 7)
        (ev,) = _drain(b)
        assert ev.timestamp == 1000.0

    def test_a_device_the_ioctl_failed_on_still_takes_the_offset(self):
        # The fallback has to stay per-device: one refusing driver must not
        # take the rebase away from the devices that do answer, nor get it
        # applied twice.
        b = WaylandCaptureBackend([])
        b._clock_offset = -5.0
        b._monotonic_fds.add(7)
        b._decode(e, _FakeEvent(e.EV_KEY, e.KEY_H, 1, ts=1000.0), 8)
        (ev,) = _drain(b)
        assert ev.timestamp == 995.0

    def test_asking_a_non_device_for_monotonic_stamps_is_survived(self):
        # An ENOTTY from something that is not an input device must leave the
        # offset path in place rather than escaping into start().
        r, w = os.pipe()
        try:
            b = WaylandCaptureBackend([])
            b._use_monotonic_clock(_FakeDevice({}, fd=r))
            assert b._monotonic_fds == set()
        finally:
            os.close(r)
            os.close(w)


class TestDroppedEvents:
    """SYN_DROPPED: the kernel's own admission that it lost events."""

    _GONE_FD = 4242
    """A descriptor nothing owns, so the EVIOCGKEY in `_end_resync` fails with
    EBADF and the reconciliation takes its cannot-be-asked path -- the shape
    every test here wants except the two that monkeypatch it."""

    def test_events_inside_the_dropped_window_are_discarded(self):
        # The kernel threw events away; what is left of the packet is not a
        # shorter truth but a corrupt one, so it goes on the floor through to
        # the SYN_REPORT that closes it.
        b = WaylandCaptureBackend([], screen_size=(1920, 1080))
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_DROPPED, 0), self._GONE_FD)
        b._decode(e, _FakeEvent(e.EV_KEY, e.KEY_H, 1), self._GONE_FD)
        b._decode(e, _FakeEvent(e.EV_REL, e.REL_X, 50), self._GONE_FD)
        assert _drain(b) == []
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0), self._GONE_FD)
        assert _drain(b) == []

    def test_a_half_accumulated_packet_never_becomes_motion(self):
        # The deltas banked before the overrun belong to a packet the kernel
        # truncated; flushing them later stamps a coordinate the pointer was
        # never at.
        b = WaylandCaptureBackend([], screen_size=(1920, 1080))
        b._decode(e, _FakeEvent(e.EV_REL, e.REL_X, 10), self._GONE_FD)
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_DROPPED, 0), self._GONE_FD)
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0), self._GONE_FD)
        assert _drain(b) == []
        assert b._pending == {}

    def test_the_stream_resumes_after_the_closing_syn_report(self):
        b = WaylandCaptureBackend([])
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_DROPPED, 0), self._GONE_FD)
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0), self._GONE_FD)
        _drain(b)
        b._decode(e, _FakeEvent(e.EV_KEY, e.KEY_H, 1), self._GONE_FD)
        (ev,) = _drain(b)
        assert ev.kind == "key_press"

    def test_the_recording_is_told_that_events_were_dropped(self):
        # A gap a recording does not admit to is the failure mode this
        # backend must never have: the note is the whole point.
        b = WaylandCaptureBackend([])
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_DROPPED, 0), self._GONE_FD)
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0), self._GONE_FD)
        assert any("SYN_DROPPED" in note for note in b.notes)

    def test_a_key_whose_release_was_dropped_is_released(self):
        # The harm that makes this worth doing: a lost Shift release holds the
        # modifier down over every key for the rest of the recording.
        b = WaylandCaptureBackend([])
        b._decode(e, _FakeEvent(e.EV_KEY, e.KEY_LEFTSHIFT, 1), 9)
        _drain(b)
        b._kernel_keys_down = lambda _e, _fd: set()  # the kernel: nothing held
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_DROPPED, 0), 9)
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0), 9)
        (ev,) = _drain(b)
        assert ev.kind == "key_release"
        assert ev.keysym == keysym_and_text(e.KEY_LEFTSHIFT, False)[0]
        assert b._held_keys[9] == set()

    def test_a_key_held_through_the_overrun_gets_the_press_that_was_lost(self):
        # Reconstructed so the stream stays internally consistent: the
        # normalizer builds holds out of press/release pairs, and a release
        # with no press is not a shape it can read.
        b = WaylandCaptureBackend([])
        b._kernel_keys_down = lambda _e, _fd: {e.KEY_LEFTCTRL}
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_DROPPED, 0), 9)
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0), 9)
        (ev,) = _drain(b)
        assert ev.kind == "key_press"
        assert e.KEY_LEFTCTRL in b._held_keys[9]

    def test_a_stuck_pointer_button_is_reconciled_too(self):
        # EVIOCGKEY covers the BTN range, and a button left logically down is
        # as damaging as a modifier -- every later motion reads as a drag.
        b = WaylandCaptureBackend([])
        b._decode(e, _FakeEvent(e.EV_KEY, e.BTN_LEFT, 1), 9)
        _drain(b)
        b._kernel_keys_down = lambda _e, _fd: set()
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_DROPPED, 0), 9)
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0), 9)
        (ev,) = _drain(b)
        assert (ev.kind, ev.button) == ("button_release", 1)

    def test_a_key_held_on_another_device_is_not_released(self):
        # EVIOCGKEY answers for one device. Held state is tracked per
        # descriptor so a key down on a second keyboard cannot look like one
        # this device lost.
        b = WaylandCaptureBackend([])
        b._decode(e, _FakeEvent(e.EV_KEY, e.KEY_LEFTSHIFT, 1), 11)
        _drain(b)
        b._kernel_keys_down = lambda _e, _fd: set()
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_DROPPED, 0), 9)
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0), 9)
        assert _drain(b) == []
        assert b._held_keys[11] == {e.KEY_LEFTSHIFT}

    def test_held_state_is_left_alone_when_the_kernel_cannot_be_asked(self):
        # No descriptor to ask: inventing releases from a bitmap that was
        # never read would be a guess, so the held set stands.
        b = WaylandCaptureBackend([])
        _feed(b, _FakeEvent(e.EV_KEY, e.KEY_LEFTSHIFT, 1))
        _drain(b)
        _feed(b, _FakeEvent(e.EV_SYN, e.SYN_DROPPED, 0))
        _feed(b, _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0))
        assert _drain(b) == []
        assert b._held_keys[None] == {e.KEY_LEFTSHIFT}

    def test_a_dropped_code_is_never_reconstructed(self):
        # BTN_TOUCH and the stylus tool codes are dropped by `_decode_key`, so
        # tracking them would have `_end_resync` fabricate a press for an
        # event this backend does not emit at all.
        b = WaylandCaptureBackend([])
        b._decode(e, _FakeEvent(e.EV_KEY, e.BTN_TOUCH, 1), 9)
        assert _drain(b) == []
        assert b._held_keys.get(9, set()) == set()


class _FlakyEvdev:
    """An `evdev` module stand-in whose InputDevice refuses certain paths."""

    def __init__(self, refuse):
        self._refuse = set(refuse)

    def InputDevice(self, path):  # noqa: N802 - mirrors evdev's own spelling
        if path in self._refuse:
            raise OSError(13, "Permission denied")
        return _FakeDevice({}, fd=100 + len(path))


class TestPartialDeviceOpen:
    def test_one_unreadable_device_does_not_cost_the_others(self):
        # Hotplug churn, a partial udev ACL, or a uinput device whose creator
        # exited between the pick and the open: capture with a keyboard and no
        # mouse beats no capture at all.
        b = WaylandCaptureBackend([])
        devices, failures = b._open(
            _FlakyEvdev(["/dev/input/event1"]),
            ["/dev/input/event0", "/dev/input/event1"],
        )
        assert len(devices) == 1
        assert len(failures) == 1

    def test_a_skipped_device_is_named_in_the_notes(self):
        # A device missing from a recording is invisible in it.
        b = WaylandCaptureBackend([])
        b._open(
            _FlakyEvdev(["/dev/input/event1"]),
            ["/dev/input/event0", "/dev/input/event1"],
        )
        (note,) = b.notes
        assert "event1" in note
        assert "1 of 2" in note

    def test_nothing_openable_is_still_a_clean_refusal(self, monkeypatch):
        fake = _FlakyEvdev(["/dev/input/event0"])
        monkeypatch.setattr(wayland_module, "_evdev", lambda: fake)
        b = WaylandCaptureBackend(["/dev/input/event0"])
        with pytest.raises(wayland_module.CaptureUnavailable, match="cannot open any"):
            b.start()

    def test_a_total_refusal_writes_no_note(self, monkeypatch):
        # The refusal carries the reason itself; a note on a recording that
        # was never made has nowhere to go.
        fake = _FlakyEvdev(["/dev/input/event0"])
        monkeypatch.setattr(wayland_module, "_evdev", lambda: fake)
        b = WaylandCaptureBackend(["/dev/input/event0"])
        with contextlib.suppress(wayland_module.CaptureUnavailable):
            b.start()
        assert b.notes == []


class TestDescriptorReuse:
    def test_closing_a_device_forgets_everything_keyed_by_its_descriptor(self):
        # The kernel reissues a descriptor number as soon as it is free, so a
        # device the rescan picks up can inherit a departed one's number --
        # and with it the wrong clock, a stranger's absinfo range, or a key
        # held by a device that is gone.
        b = WaylandCaptureBackend([])
        b._monotonic_fds.add(9)
        b._dropped_fds.add(9)
        b._abs_ranges[9] = (0, 32767, 0, 32767)
        b._padlike_fds.add(9)
        b._held_keys[9] = {e.KEY_LEFTSHIFT}
        b._forget_fd(9)
        assert 9 not in b._monotonic_fds
        assert 9 not in b._dropped_fds
        assert 9 not in b._abs_ranges
        assert 9 not in b._padlike_fds
        assert 9 not in b._held_keys


class _FakeAbsInfo:
    def __init__(self, value, min_, max_):
        self.value, self.min, self.max = value, min_, max_


class _FakeDevice:
    """Just enough of evdev.InputDevice for `_learn_abs_range`."""

    def __init__(self, caps, fd=9):
        self._caps, self._fd = caps, fd

    def capabilities(self):
        if isinstance(self._caps, Exception):
            raise self._caps
        return self._caps

    def fileno(self):
        return self._fd


class TestTouchpadAbs:
    def _pad_caps(self):
        return {
            e.EV_KEY: [e.BTN_LEFT, e.BTN_TOUCH, e.BTN_TOOL_FINGER],
            e.EV_ABS: [
                (e.ABS_X, _FakeAbsInfo(500, 0, 1215)),
                (e.ABS_Y, _FakeAbsInfo(300, 0, 680)),
            ],
        }

    def test_a_touchpads_pad_local_abs_is_not_scaled_to_the_screen(self):
        # A touchpad reports ABS_X/Y too, but they are finger positions in
        # pad-local units the compositor turns into *relative* motion --
        # scaling them to pixels stamps coordinates unrelated to the cursor.
        # BTN_TOOL_FINGER is the tool code every touchpad reports and no
        # screen-mapped device (VM tablet, drawing tablet) does.
        b = WaylandCaptureBackend([], screen_size=(1920, 1080))
        b._learn_abs_range(e, _FakeDevice(self._pad_caps(), fd=9))
        assert 9 in b._padlike_fds
        assert 9 not in b._abs_ranges
        assert b._pos is None, "a finger position must not seed the pointer"
        b._decode(e, _FakeEvent(e.EV_ABS, e.ABS_X, 500), 9)
        b._decode(e, _FakeEvent(e.EV_ABS, e.ABS_Y, 300), 9)
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0), 9)
        assert _drain(b) == [], "pad-local coordinates must not become motion"

    def test_a_true_absolute_device_still_registers_and_seeds(self):
        caps = self._pad_caps()
        caps[e.EV_KEY] = [e.BTN_LEFT]  # a tablet: no touchpad tool code
        b = WaylandCaptureBackend([], screen_size=(1920, 1080))
        b._learn_abs_range(e, _FakeDevice(caps, fd=9))
        assert 9 in b._abs_ranges
        assert b._pos is not None, "seeded from the device's current value"

    def test_a_device_that_vanishes_during_the_ioctl_is_skipped(self):
        # The rescan calls this on the reader thread, where an escaped
        # OSError would end capture for the rest of the recording.
        b = WaylandCaptureBackend([])
        b._learn_abs_range(e, _FakeDevice(OSError(19, "No such device")))
        assert b._abs_ranges == {}

    def test_a_gamepads_stick_never_seeds_the_pointer(self):
        # A gamepad exposes ABS axes with no pointer button; its stick's
        # resting value is not a cursor position, and explicitly supplied
        # paths bypass the picker, so the guard has to live here too.
        caps = self._pad_caps()
        caps[e.EV_KEY] = [e.BTN_SOUTH]
        b = WaylandCaptureBackend([], screen_size=(1920, 1080))
        b._learn_abs_range(e, _FakeDevice(caps, fd=9))
        assert b._abs_ranges == {}
        assert b._pos is None

    def test_a_gamepads_abs_events_never_become_motion(self):
        # Explicit paths bypass `_wants_device`, and with no registered range
        # `_scale_abs` passes raw device units through -- so the stick's fd
        # has to join the ignore set or its wiggles emit garbage motion.
        caps = self._pad_caps()
        caps[e.EV_KEY] = [e.BTN_SOUTH]
        b = WaylandCaptureBackend([], screen_size=(1920, 1080))
        b._learn_abs_range(e, _FakeDevice(caps, fd=9))
        b._decode(e, _FakeEvent(e.EV_ABS, e.ABS_X, 17000), 9)
        b._decode(e, _FakeEvent(e.EV_ABS, e.ABS_Y, 12000), 9)
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0), 9)
        assert _drain(b) == []


class TestDeviceEligibility:
    """`_wants_device`: what the auto-picker counts as a keyboard or pointer."""

    def test_a_keyboard_is_recognised_by_key_a(self):
        assert wayland_module._wants_device(e, {e.EV_KEY: [e.KEY_A, e.KEY_B]})

    def test_a_relative_mouse_qualifies_by_its_axes(self):
        caps = {e.EV_KEY: [e.BTN_LEFT], e.EV_REL: [e.REL_X, e.REL_Y]}
        assert wayland_module._wants_device(e, caps)

    def test_a_wheel_only_device_still_qualifies(self):
        assert wayland_module._wants_device(e, {e.EV_REL: [e.REL_WHEEL]})

    def test_an_absolute_tablet_qualifies_by_its_buttons(self):
        caps = {e.EV_KEY: [e.BTN_LEFT], e.EV_ABS: [e.ABS_X, e.ABS_Y]}
        assert wayland_module._wants_device(e, caps)

    def test_a_gamepad_is_not_a_pointer(self):
        # ABS sticks + gamepad buttons: picking it captured joystick noise as
        # motion and let the stick's resting value seed the cursor position.
        caps = {e.EV_KEY: [e.BTN_SOUTH, e.BTN_EAST], e.EV_ABS: [e.ABS_X, e.ABS_Y]}
        assert not wayland_module._wants_device(e, caps)

    def test_an_accelerometer_is_not_a_pointer(self):
        assert not wayland_module._wants_device(e, {e.EV_ABS: [e.ABS_X, e.ABS_Y]})


class TestCoordinates:
    """Clicks must carry the screen position the resolver needs.

    Absolute devices scale their own coordinate space (the VirtualBox tablet
    is 0..32767) to screen pixels; relative mice accumulate from the screen's
    centre; a click with no position information says (0, 0) rather than
    inventing one.
    """

    def _abs_backend(self):
        b = WaylandCaptureBackend([], screen_size=(1280, 800))
        b._abs_ranges[7] = (0, 32767, 0, 32767)  # the VBox USB Tablet's range
        return b

    def test_abs_device_units_scale_to_screen_pixels(self):
        b = self._abs_backend()
        b._decode(e, _FakeEvent(e.EV_ABS, e.ABS_X, 32767), 7)
        b._decode(e, _FakeEvent(e.EV_ABS, e.ABS_Y, 0), 7)
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0), 7)
        (motion,) = _drain(b)
        assert (motion.x, motion.y) == (1279, 0)

    def test_click_is_stamped_with_the_scaled_position(self):
        b = self._abs_backend()
        b._decode(e, _FakeEvent(e.EV_ABS, e.ABS_X, 16384), 7)  # ~ mid-screen
        b._decode(e, _FakeEvent(e.EV_ABS, e.ABS_Y, 16384), 7)
        b._decode(e, _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0), 7)
        b._decode(e, _FakeEvent(e.EV_KEY, e.BTN_LEFT, 1), 7)
        _motion, click = _drain(b)
        assert click.kind == "button_press"
        assert (click.x, click.y) == (640, 400)

    def test_relative_motion_accumulates_from_screen_centre(self):
        b = WaylandCaptureBackend([], screen_size=(1280, 800))
        _feed(
            b,
            _FakeEvent(e.EV_REL, e.REL_X, 10),
            _FakeEvent(e.EV_REL, e.REL_Y, -10),
            _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0),
            _FakeEvent(e.EV_KEY, e.BTN_LEFT, 1),
        )
        motion, click = _drain(b)
        assert (motion.x, motion.y) == (650, 390)
        assert (click.x, click.y) == (650, 390)

    def test_relative_position_clamps_to_the_screen(self):
        b = WaylandCaptureBackend([], screen_size=(1280, 800))
        _feed(
            b,
            _FakeEvent(e.EV_REL, e.REL_X, 99999),
            _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0),
        )
        (motion,) = _drain(b)
        assert (motion.x, motion.y) == (1279, 400)

    def test_click_with_no_position_information_says_origin(self):
        b = WaylandCaptureBackend([])  # no screen, no motion seen
        _feed(b, _FakeEvent(e.EV_KEY, e.BTN_LEFT, 1))
        (click,) = _drain(b)
        assert (click.x, click.y) == (0, 0)

    def test_screen_tag_is_carried_on_every_event(self):
        b = WaylandCaptureBackend([], screen_size=(1280, 800), screen=3)
        _feed(
            b,
            _FakeEvent(e.EV_REL, e.REL_X, 1),
            _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0),
            _FakeEvent(e.EV_KEY, e.BTN_LEFT, 1),
            _FakeEvent(e.EV_REL, e.REL_WHEEL, 1),
        )
        assert all(ev.screen == 3 for ev in _drain(b))


class TestKeysymVocabulary:
    """Key names this backend emits must be ones pyguitest can replay."""

    def test_named_keysyms_are_pressable_by_pyguitest_uinput(self):
        pg = pytest.importorskip("pyguitest.backends.uinput")
        # pyguitest resolves a keysym to an evdev code as
        # KEY_<_KEYSYM_NAMES.get(name.lower(), name.upper())>; a keysym we emit
        # for a non-text key is replayable iff that code exists. This is the
        # cross-repo contract: recorded key names must be pressable.
        names = pg._KEYSYM_NAMES
        for code in (
            e.KEY_ENTER,
            e.KEY_TAB,
            e.KEY_ESC,
            e.KEY_BACKSPACE,
            e.KEY_DELETE,
            e.KEY_INSERT,
            e.KEY_LEFT,
            e.KEY_RIGHT,
            e.KEY_UP,
            e.KEY_DOWN,
            e.KEY_HOME,
            e.KEY_END,
            e.KEY_PAGEUP,
            e.KEY_PAGEDOWN,
            e.KEY_LEFTCTRL,
            e.KEY_RIGHTSHIFT,
            e.KEY_LEFTALT,
            e.KEY_LEFTMETA,
            e.KEY_F1,
            e.KEY_F12,
        ):
            keysym, _ = keysym_and_text(code, False)
            suffix = names.get(keysym.lower(), keysym.upper())
            assert hasattr(e, f"KEY_{suffix}"), (
                f"keysym {keysym!r} -> KEY_{suffix} is not a real evdev code; "
                "pyguitest could not press it"
            )

    def test_unmapped_code_falls_back_to_evdev_name_not_crash(self):
        keysym, text = keysym_and_text(e.KEY_KPPLUS, False)
        assert keysym and text == ""


def _can_round_trip():
    # The round-trip WRITES a virtual device to /dev/uinput and READS its
    # events back from /dev/input -- a box can have one without the other
    # (uinput writable but no `input` group, as on a stock COSMIC session,
    # which would make the harness AttributeError on a None device rather than
    # skip). Check access to the nodes directly instead of creating a probe
    # UInput: the event node a UInput creates appears asynchronously via udev,
    # so UInput.device is momentarily None on a cold start even on a fully
    # permissioned box, and a probe would then skip a runnable test. os.access
    # honours the ACLs logind puts on these nodes, so it is both stable and
    # correct.
    return os.access("/dev/uinput", os.W_OK) and any(
        os.access(path, os.R_OK) for path in glob.glob("/dev/input/event*")
    )


@pytest.mark.skipif(
    os.environ.get("PGT_LIVE_INPUT_TESTS") != "1",
    reason="creates real uinput devices and injects real kernel input into "
    "them; `_isolate` grabs each one so the events do not reach the session, "
    "and the tests skip rather than type into the desktop if that grab is "
    "refused -- but this still manipulates the box's input stack, so set "
    "PGT_LIVE_INPUT_TESTS=1 only where that is safe",
)
@pytest.mark.skipif(
    not _can_round_trip(),
    reason="needs /dev/uinput write + /dev/input read (input group)",
)
class TestHardwareRoundTrip:
    """Inject into a virtual device, capture it back through the real backend."""

    @staticmethod
    def _open_device(caps, name="pg-wl-roundtrip"):
        # The event node udev creates for a fresh uinput device can lag the
        # device's creation, and UInput resolves `.device` once at construction
        # without retrying -- so on a cold start it can come back None on a box
        # that is in fact fully permissioned (observed on COSMIC). Recreate
        # until the node appears rather than AttributeError on `ui.device.path`;
        # `_can_round_trip` has already established the permissions, so this
        # only ever waits out udev, never a real denial.
        deadline = time.monotonic() + 2.0
        while True:
            ui = evdev.UInput(caps, name=name)
            if ui.device is not None:
                return ui
            ui.close()
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "uinput device node did not appear within 2s "
                    "(udev not creating /dev/input nodes?)"
                )
            time.sleep(0.05)

    # Virtual devices these tests create are named with this prefix, and only
    # a device carrying it may ever be grabbed -- see `_isolate`.
    _PREFIX = "pg-wl-"

    @classmethod
    def _isolate(cls, backend, path):
        """Stop the injected input reaching the live session.

        EVIOCGRAB gives the grabbing descriptor *exclusive* delivery: while it
        is held, the compositor's own descriptor for this device receives
        nothing. Grabbing the backend's descriptor therefore keeps the whole
        round trip intact -- real uinput, real kernel, real capture -- while
        the keystrokes and the button click stop landing in whatever happens
        to have focus. Without it this class typed "hi" into the live
        desktop's search bar and clicked wherever the pointer sat, which is
        both destructive and a privacy problem on a box someone is using.

        Only ever grabs a device this class created: grabbing the box's real
        keyboard or mouse would take the session's input away from the person
        at it. Events written before the grab go to everyone, so this must be
        called before the first injection.
        """
        dev = next((d for d in list(backend._devices) if d.path == path), None)
        if dev is None:
            raise AssertionError(f"backend never opened {path}")
        if not dev.name.startswith(cls._PREFIX):
            # Refuse rather than silently skip: a real device turning up under
            # a path this class injects into means the test is confused about
            # which device it is driving, and grabbing it would freeze the
            # user's own keyboard.
            raise AssertionError(
                f"refusing to grab {dev.path} ({dev.name!r}) -- not a test device"
            )
        try:
            dev.grab()
        except OSError as exc:
            pytest.skip(
                f"cannot grab {dev.path} to keep injected input out of the "
                f"live session ({exc}); refusing to type into the desktop"
            )
        return dev

    @staticmethod
    def _teardown(backend, ui, grabbed=None):
        """Stop capture and destroy the device without leaking input.

        The order satisfies two separate constraints, and getting either
        wrong is invisible until something downstream is measured.

        The device must be destroyed while the grab still holds. The kernel
        synthesizes a release for every key still held when a device goes
        away and delivers it to every reader -- measured on COSMIC escaping
        to the live session as a stray `KEY_H` and `BTN_LEFT` release, one
        for each test that pressed without releasing. Destroying inside the
        grab contains them, so a future test that forgets a release cannot
        leak either.

        And the reader must be stopped *before* that, with its descriptor
        left open: a reader that notices the device die runs `_forget_fd`,
        which clears the per-descriptor state -- the monotonic-clock set
        among it -- that these tests assert on afterwards. So the thread is
        stopped directly rather than through `stop()`, which would close the
        descriptor and release the grab with it.
        """
        backend._stop.set()
        if backend._thread is not None:
            backend._thread.join(timeout=3)
        with contextlib.suppress(Exception):
            ui.close()
        if grabbed is not None:
            with contextlib.suppress(OSError):
                grabbed.ungrab()
        backend.stop()

    def _run(self, caps, inject, keep=None):
        ui = self._open_device(caps)
        time.sleep(0.3)
        backend = WaylandCaptureBackend([ui.device.path])
        backend.start()
        time.sleep(0.2)
        grabbed = self._isolate(backend, ui.device.path)
        try:
            inject(ui)
            time.sleep(0.4)
        finally:
            if keep is not None:
                keep.append(backend)
            self._teardown(backend, ui, grabbed)
        return list(backend.drain())

    def test_the_kernel_accepts_the_monotonic_clock_on_a_real_device(self):
        # The one assertion no fake can make: that EVIOCSCLOCKID is the right
        # request number and a real driver answers it. Everything else about
        # the clock is arithmetic a unit test can drive -- whether the kernel
        # agrees is hardware's to say, and if it ever stops agreeing the
        # offset fallback hides it, so it is asserted here rather than
        # inferred from timestamps that look plausible either way.
        def inject(ui):
            ui.write(e.EV_KEY, e.KEY_H, 1)
            ui.syn()
            # Released, not left held: the kernel synthesizes a release for a
            # held key when the device is destroyed, and a test should not
            # manufacture the stuck-key state this backend exists to fix.
            ui.write(e.EV_KEY, e.KEY_H, 0)
            ui.syn()

        kept = []
        evs = self._run({e.EV_KEY: [e.KEY_H]}, inject, keep=kept)
        (backend,) = kept
        assert backend._monotonic_fds, "EVIOCSCLOCKID was refused by the kernel"
        # And with it set, the stamps need no rebasing to already be in the
        # pipeline's clock -- the offset is captured but must go unused.
        assert evs
        assert all(abs(ev.timestamp - time.monotonic()) < 60 for ev in evs)

    def test_typed_text_round_trips(self):
        def inject(ui):
            for c in (e.KEY_H, e.KEY_I):
                ui.write(e.EV_KEY, c, 1)
                ui.syn()
                ui.write(e.EV_KEY, c, 0)
                ui.syn()
                time.sleep(0.02)

        evs = self._run({e.EV_KEY: [e.KEY_H, e.KEY_I]}, inject)
        text = "".join(ev.text for ev in evs if ev.kind == "key_press")
        assert text == "hi"
        # Live proof of the clock rebase: a started backend's stamps are in
        # time.monotonic's domain, not the kernel's CLOCK_REALTIME epoch.
        assert all(abs(ev.timestamp - time.monotonic()) < 60 for ev in evs)

    def test_click_round_trips(self):
        def inject(ui):
            ui.write(e.EV_KEY, e.BTN_LEFT, 1)
            ui.syn()
            time.sleep(0.02)
            ui.write(e.EV_KEY, e.BTN_LEFT, 0)
            ui.syn()

        evs = self._run({e.EV_KEY: [e.BTN_LEFT]}, inject)
        kinds = [(ev.kind, ev.button) for ev in evs if ev.kind.startswith("button")]
        assert kinds == [("button_press", 1), ("button_release", 1)]

    def test_click_without_motion_is_stamped_from_the_devices_position(self):
        # Regression (caught in the KDE night run): a click with no motion
        # before it must carry the pointer's actual position, not (0, 0). An
        # absolute device already reports where it is -- seed from that.
        from evdev import AbsInfo

        caps = {
            e.EV_KEY: [e.BTN_LEFT],
            e.EV_ABS: [
                (
                    e.ABS_X,
                    AbsInfo(
                        value=16384, min=0, max=32767, fuzz=0, flat=0, resolution=0
                    ),
                ),
                (
                    e.ABS_Y,
                    AbsInfo(
                        value=16384, min=0, max=32767, fuzz=0, flat=0, resolution=0
                    ),
                ),
            ],
        }
        ui = self._open_device(caps, name="pg-wl-seated")
        time.sleep(0.3)
        backend = WaylandCaptureBackend([ui.device.path], screen_size=(1280, 800))
        backend.start()
        time.sleep(0.2)
        # This one builds its own backend rather than going through `_run`,
        # so it needs the grab explicitly -- and needs it most: an absolute
        # pointing device seeded mid-range is the one the compositor warps
        # the real cursor for, and the click below then lands wherever that
        # left it. Ungrabbed on Debian GNOME that meant a click into the
        # top-left hot corner, which opens the Activities overview -- the
        # search bar that the next test's "hi" was then typed into.
        grabbed = self._isolate(backend, ui.device.path)
        try:
            ui.write(e.EV_KEY, e.BTN_LEFT, 1)  # click, no motion first
            ui.syn()
            # Released for the same reason as in the clock test: a button left
            # held becomes a synthetic release to every reader on destroy.
            ui.write(e.EV_KEY, e.BTN_LEFT, 0)
            ui.syn()
            time.sleep(0.4)
        finally:
            self._teardown(backend, ui, grabbed)
        clicks = [ev for ev in backend.drain() if ev.kind == "button_press"]
        assert clicks, "no click captured"
        # 16384 of 32767 across 1280/800 -> the screen's rough middle
        assert clicks[0].x == round(16384 / 32767 * 1279)
        assert clicks[0].y == round(16384 / 32767 * 799)

    def test_a_device_created_after_capture_started_is_still_captured(self):
        # The hotplug case: a mouse plugged in mid-recording, or a replay tool
        # creating its uinput device after capture began. Auto-picked capture
        # rescans and must not silently miss it.
        backend = WaylandCaptureBackend()  # auto-pick + rescan
        backend._RESCAN_SECONDS = 0.3
        backend.start()
        time.sleep(0.4)
        # KEY_A too: pick_devices filters for keyboards (KEY_A) or pointers,
        # and a one-key gadget is deliberately not worth watching.
        ui = self._open_device({e.EV_KEY: [e.KEY_F9, e.KEY_A]}, name="pg-wl-hotplug")
        grabbed = None
        try:
            # Wait for the rescan to have actually taken the device, rather
            # than sleeping a rescan interval and assuming it did. A fresh
            # uinput node is not openable the instant it appears -- udev has
            # to apply the input-group permissions, and pick_devices skips
            # what it cannot open yet -- and pick_devices opens and queries
            # every device on the box. Measured on Debian, the registration
            # lands over a second after creation even with _RESCAN_SECONDS at
            # 0.3, so a fixed 1.0s wait injected into a device nobody was
            # watching yet and lost the keystroke about half the time while
            # the backend was behaving correctly. `_rescan` registers with
            # the selector *before* appending, so seeing the path here means
            # the reader is already watching it.
            deadline = time.monotonic() + 15.0
            while ui.device.path not in {d.path for d in list(backend._devices)}:
                assert time.monotonic() < deadline, (
                    "the rescan never registered the hotplugged device"
                )
                time.sleep(0.05)
            # Keep F9 out of the live session -- see `_isolate`. Safe to grab
            # only now: before registration the backend has no descriptor to
            # grab with, which is also why the poll above has to come first.
            grabbed = self._isolate(backend, ui.device.path)
            ui.write(e.EV_KEY, e.KEY_F9, 1)
            ui.syn()
            ui.write(e.EV_KEY, e.KEY_F9, 0)
            ui.syn()
            time.sleep(0.4)
        finally:
            self._teardown(backend, ui, grabbed)
        f9 = [
            ev for ev in backend.drain() if ev.kind == "key_press" and ev.keysym == "F9"
        ]
        assert f9, "the hotplugged device's keystroke was not captured"


def test_importing_the_backend_without_evdev_is_a_clean_refusal():
    """`import ...backends.wayland` must not raise when python-evdev is absent.

    The whole point of the lazy `_evdev()` import is that a pure-Wayland box
    without the `[wayland]` extra gets an actionable `CaptureUnavailable`, not
    a bare `ImportError` traceback out of `choose_backend`. A past regression
    imported `_evdev_keysyms` (which does `from evdev import ecodes`) at module
    top, which re-broke exactly that. This runs in a subprocess with `evdev`
    blocked at the import machinery, so the check is real rather than mocked;
    `available()` must come back False via the clean refusal path.
    """
    code = textwrap.dedent(
        """
        import importlib, sys

        class _Block:
            def find_spec(self, name, path=None, target=None):
                if name == "evdev" or name.startswith("evdev."):
                    raise ImportError("evdev blocked for this test")

        sys.meta_path.insert(0, _Block())
        sys.modules.pop("evdev", None)
        sys.modules.pop("pyguitest_recorder.backends._evdev_keysyms", None)
        mod = importlib.import_module("pyguitest_recorder.backends.wayland")
        assert mod.available() is False, "available() should be False, not raise"
        print("CLEAN")
        """
    )
    # The child must import pyguitest_recorder the way this process can,
    # whether the package is pip-installed or merely on pytest's import path:
    # the gate runs the suite from `src/` without installing it, so a bare
    # `sys.executable -c` child -- which inherits none of pytest's path setup
    # -- would fail to import it and the test would fail for a reason that is
    # not the refusal it checks. Hand the child our own import roots.
    roots = [p for p in sys.path if p]
    if os.environ.get("PYTHONPATH"):
        roots.append(os.environ["PYTHONPATH"])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(roots)}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    assert "CLEAN" in result.stdout


class _FakeCursor:
    """Stand-in for the ScreenCast CursorSource: a fixed position, no stream."""

    def __init__(self, pos=None):
        self._pos = pos
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.stopped = True

    def position(self):
        return self._pos


class TestCursorSourceStamping:
    """The `screencast_cursor` opt-in: its position wins over delta tracking."""

    def test_a_click_takes_the_cursor_source_position(self):
        b = WaylandCaptureBackend([], cursor_source=_FakeCursor((500, 300)))
        _feed(b, _FakeEvent(e.EV_KEY, e.BTN_LEFT, 1))
        (click,) = _drain(b)
        assert (click.x, click.y) == (500, 300)

    def test_motion_takes_the_cursor_source_position(self):
        b = WaylandCaptureBackend(
            [], screen_size=(1280, 800), cursor_source=_FakeCursor((700, 450))
        )
        _feed(
            b, _FakeEvent(e.EV_REL, e.REL_X, 10), _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0)
        )
        (motion,) = _drain(b)
        assert (motion.x, motion.y) == (700, 450)

    def test_it_falls_back_to_delta_tracking_before_a_position_arrives(self):
        # A source that has emitted nothing yet (position() is None) leaves the
        # centre-seeded delta tracking in place -- same as having no source.
        b = WaylandCaptureBackend(
            [], screen_size=(1280, 800), cursor_source=_FakeCursor(None)
        )
        _feed(
            b,
            _FakeEvent(e.EV_REL, e.REL_X, 10),
            _FakeEvent(e.EV_REL, e.REL_Y, -10),
            _FakeEvent(e.EV_SYN, e.SYN_REPORT, 0),
            _FakeEvent(e.EV_KEY, e.BTN_LEFT, 1),
        )
        _motion, click = _drain(b)
        assert (click.x, click.y) == (650, 390)  # 640,400 centre + (10, -10)

    def test_stop_stops_the_cursor_source(self):
        cur = _FakeCursor()
        WaylandCaptureBackend([], cursor_source=cur).stop()
        assert cur.stopped

    def test_no_cursor_source_is_the_unchanged_default(self):
        # Without a source, a click with no motion still says the origin -- the
        # behaviour every non-opt-in recording keeps.
        b = WaylandCaptureBackend([])
        _feed(b, _FakeEvent(e.EV_KEY, e.BTN_LEFT, 1))
        (click,) = _drain(b)
        assert (click.x, click.y) == (0, 0)
