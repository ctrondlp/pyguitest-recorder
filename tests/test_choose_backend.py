"""`choose_backend`'s platform dispatch.

What "auto" means on each platform, and what naming a backend explicitly
does on the wrong one. Both real backends' own `unavailable_reason()` are
patched out here rather
than exercised -- this file is about the dispatch *decision*, which backend
gets asked at all, not about whether either one can actually capture on this
machine.
"""

import pytest

from pyguitest_recorder import recorder as recorder_module
from pyguitest_recorder.backends import win32 as win32_module
from pyguitest_recorder.backends.base import CaptureUnavailable
from pyguitest_recorder.config import Settings
from pyguitest_recorder.recorder import choose_backend


def _settings(**overrides):
    return Settings(**overrides)


class TestAutoOnLinux:
    def test_auto_picks_xrecord(self, monkeypatch):
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.x11.unavailable_reason", lambda: None
        )
        backend = choose_backend(_settings(backend="auto"))
        assert backend.name == "xrecord"

    def test_auto_still_reports_xrecords_own_unavailable_reason(self, monkeypatch):
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.x11.unavailable_reason",
            lambda: "no RECORD extension",
        )
        # With no wayland fallback to take (the machine running this test may
        # genuinely have readable /dev/input), the reason stays xrecord's own.
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.available", lambda: False
        )
        with pytest.raises(CaptureUnavailable, match="no RECORD extension"):
            choose_backend(_settings(backend="auto"))

    def test_naming_win32_explicitly_refuses_with_the_platform_named(self, monkeypatch):
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        with pytest.raises(CaptureUnavailable, match="native Windows process"):
            choose_backend(_settings(backend="win32"))

    def test_auto_prefers_xrecord_where_x_is_reachable(self, monkeypatch):
        # Unchanged behaviour on every existing machine: with an X server to
        # record, auto never substitutes the wayland backend.
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.x11.unavailable_reason", lambda: None
        )
        assert choose_backend(_settings(backend="auto")).name == "xrecord"

    def test_auto_falls_back_to_wayland_where_no_x_is_reachable(self, monkeypatch):
        # One configuration serves every machine: the pure-Wayland session
        # that used to end in xrecord's refusal records through evdev instead.
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.x11.unavailable_reason",
            lambda: "no X server",
        )
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.available", lambda: True
        )
        assert choose_backend(_settings(backend="auto")).name == "wayland-evdev"

    def test_auto_fallback_needs_wayland_to_be_available(self, monkeypatch):
        # Where neither can serve, the refusal is xrecord's own, not a second
        # one invented for the fallback.
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.x11.unavailable_reason",
            lambda: "no X server",
        )
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.available", lambda: False
        )
        with pytest.raises(CaptureUnavailable, match="no X server"):
            choose_backend(_settings(backend="auto"))

    def test_a_broken_xrecord_on_x11_never_falls_back_to_evdev(self, monkeypatch):
        # Reading /dev/input is machine-wide capture. An X11 session whose
        # XRecord is merely broken gets xrecord's own error -- it is never
        # silently switched to the global input reader, even with readable
        # devices right there.
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.x11.unavailable_reason",
            lambda: "no RECORD extension",
        )
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.available", lambda: True
        )
        with pytest.raises(CaptureUnavailable, match="no RECORD extension"):
            choose_backend(_settings(backend="auto"))

    def test_a_sessionless_shell_never_falls_back_to_evdev(self, monkeypatch):
        # A bare shell (a plain ssh login) advertises no session at all; auto
        # keeps xrecord's refusal there too. Recording over ssh asks for
        # `backend = "wayland"` explicitly, which this gate does not apply to.
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(
            "pyguitest_recorder.backends.x11.unavailable_reason",
            lambda: "no X server",
        )
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.available", lambda: True
        )
        with pytest.raises(CaptureUnavailable, match="no X server"):
            choose_backend(_settings(backend="auto"))

    def test_explicit_xrecord_never_falls_back(self, monkeypatch):
        # Naming a backend asks for exactly what was named.
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.x11.unavailable_reason",
            lambda: "no X server",
        )
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.available", lambda: True
        )
        with pytest.raises(CaptureUnavailable, match="no X server"):
            choose_backend(_settings(backend="xrecord"))

    def test_an_explicit_display_keeps_xrecords_refusal(self, monkeypatch):
        # display asks for an X server by name; substituting a backend that
        # has no displays would record something else than was configured.
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.x11.unavailable_reason",
            lambda: "no X server",
        )
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.available", lambda: True
        )
        with pytest.raises(CaptureUnavailable, match="no X server"):
            choose_backend(_settings(backend="auto", display=":1"))

    def test_naming_wayland_picks_it_when_devices_are_readable(self, monkeypatch):
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.available", lambda: True
        )
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.pick_devices", lambda: []
        )
        assert choose_backend(_settings(backend="wayland")).name == "wayland-evdev"

    def test_naming_wayland_refuses_with_the_permission_reason(self, monkeypatch):
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.available", lambda: False
        )
        with pytest.raises(CaptureUnavailable, match="input.*group"):
            choose_backend(_settings(backend="wayland"))

    def test_wayland_refuses_an_explicit_display_before_recording(self, monkeypatch):
        # A setting the backend cannot honour is stated up front, not ignored:
        # display names an X server and an evdev reader has none to choose.
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.available", lambda: True
        )
        with pytest.raises(CaptureUnavailable, match="display.*cannot honour"):
            choose_backend(_settings(backend="wayland", display=":1"))

    def test_wayland_carries_the_screen_tag_instead_of_refusing_it(self, monkeypatch):
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.available", lambda: True
        )
        backend = choose_backend(_settings(backend="wayland", screen=2))
        assert backend._screen == 2

    def test_wayland_has_no_cursor_source_by_default(self, monkeypatch):
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.available", lambda: True
        )
        backend = choose_backend(_settings(backend="wayland"))
        assert backend._cursor is None

    def test_screencast_cursor_opt_in_attaches_a_cursor_source(self, monkeypatch):
        # The opt-in builds a CursorSource and hands it to the backend; it is
        # only *started* (consent dialog) when the backend starts, not here.
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.wayland.available", lambda: True
        )
        backend = choose_backend(_settings(backend="wayland", screencast_cursor=True))
        assert backend._cursor is not None
        # Selection must be side-effect-free: nothing is spawned (and so no
        # consent dialog can appear) until the backend itself starts.
        assert backend._cursor._proc is None


@pytest.mark.skipif(
    not win32_module._pyguitest_win32_available(),
    reason=(
        "the installed pyguitest has no backends.win32, so the win32 backend "
        "cannot be constructed to be dispatched to"
    ),
)
class TestAutoOnWindows:
    """Skipped where pyguitest predates its own Windows support.

    These assert which backend `choose_backend` *returns*, so they build one
    -- and building one needs the key vocabulary that lives in pyguitest's
    own win32 backend. The dispatch decision is what is under test; the
    dependency is what makes the decision reachable.
    """

    def test_auto_picks_win32_rather_than_xrecord(self, monkeypatch):
        # The one judgment call this function makes: an X server genuinely
        # can be present on Windows (Xming, VcXsrv, WSLg), and picking it
        # anyway would record only the X clients drawing into that server --
        # a phantom-desktop recording with no error at all.
        monkeypatch.setattr(recorder_module.sys, "platform", "win32")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.win32.unavailable_reason", lambda: None
        )
        backend = choose_backend(_settings(backend="auto"))
        assert backend.name == "win32"

    def test_auto_reports_win32s_own_unavailable_reason_rather_than_falling_back(
        self, monkeypatch
    ):
        monkeypatch.setattr(recorder_module.sys, "platform", "win32")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.win32.unavailable_reason",
            lambda: "not on an interactive desktop",
        )
        with pytest.raises(CaptureUnavailable, match="not on an interactive desktop"):
            choose_backend(_settings(backend="auto"))

    def test_naming_xrecord_explicitly_is_still_honoured_on_windows(self, monkeypatch):
        # A deliberate override: the caller accepted the phantom-desktop risk
        # by naming xrecord outright, so this does not second-guess it.
        monkeypatch.setattr(recorder_module.sys, "platform", "win32")
        monkeypatch.setattr(
            "pyguitest_recorder.backends.x11.unavailable_reason", lambda: None
        )
        backend = choose_backend(_settings(backend="xrecord"))
        assert backend.name == "xrecord"


def test_an_unknown_backend_name_is_refused():
    with pytest.raises(CaptureUnavailable, match="unknown capture backend"):
        choose_backend(_settings(backend="nonesuch"))


class TestContextBackends:
    """Which pyguitest backends answer "which window, which element".

    The pair has to exist on the platform being recorded. Asking for
    `x11`/`atspi` on Windows cannot succeed -- no X server, no accessibility
    bus -- and the session then never opened, so every click in a real
    Windows recording came out `window: null, element: null`.
    """

    def _recorder(self, **overrides):
        from pyguitest_recorder.recorder import Recorder

        return Recorder(_settings(**overrides))

    def test_windows_names_win32_and_uia(self, monkeypatch):
        monkeypatch.setattr(recorder_module.sys, "platform", "win32")
        assert self._recorder()._context_backends() == ("win32", "uia")

    def test_every_other_platform_keeps_x11_and_atspi(self, monkeypatch):
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        assert self._recorder()._context_backends() == ("x11", "atspi")

    def test_wayland_names_the_window_tier_and_atspi(self, monkeypatch):
        # Native Wayland cannot be asked through x11 (it cannot drive the
        # session); the compositor's own window tier (`windows`) + atspi is the
        # pair, and `_on_wayland` is what selects it.
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        recorder = self._recorder(backend="wayland")
        assert recorder._on_wayland() is True
        assert recorder._context_backends() == ("windows", "atspi")

    def test_auto_is_not_wayland_before_a_backend_is_built(self, monkeypatch):
        # `auto` reads as xrecord by name; only a built wayland-evdev backend
        # makes `_on_wayland` true, so the pre-backend answer stays x11/atspi.
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        recorder = self._recorder(backend="auto")
        assert recorder._on_wayland() is False
        assert recorder._context_backends() == ("x11", "atspi")

    def test_the_window_backend_comes_first_on_both(self, monkeypatch):
        # Order is load-bearing: the window backend must keep every window
        # capability when the element backend composes onto it.
        for platform, window in (("win32", "win32"), ("linux", "x11")):
            monkeypatch.setattr(recorder_module.sys, "platform", platform)
            assert self._recorder()._context_backends()[0] == window

    def test_each_half_can_be_switched_off_independently(self, monkeypatch):
        monkeypatch.setattr(recorder_module.sys, "platform", "win32")
        assert self._recorder(element_context=False)._context_backends() == ("win32",)
        assert self._recorder(window_context=False)._context_backends() == ("uia",)
        assert (
            self._recorder(
                window_context=False, element_context=False
            )._context_backends()
            == ()
        )

    def test_the_failure_note_names_no_display_on_windows(self, monkeypatch):
        # `$DISPLAY` is an X11 idea that names nothing on Windows, and this
        # string goes into every generated script and session file.
        monkeypatch.setattr(recorder_module.sys, "platform", "win32")
        assert "DISPLAY" not in self._recorder()._session_locator("")

    def test_the_failure_note_still_names_the_display_elsewhere(self, monkeypatch):
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        assert self._recorder()._session_locator(":9") == "on :9"
        assert "$DISPLAY" in self._recorder()._session_locator("")
