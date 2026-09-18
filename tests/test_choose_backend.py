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
        with pytest.raises(CaptureUnavailable, match="no RECORD extension"):
            choose_backend(_settings(backend="auto"))

    def test_naming_win32_explicitly_refuses_with_the_platform_named(self, monkeypatch):
        monkeypatch.setattr(recorder_module.sys, "platform", "linux")
        with pytest.raises(CaptureUnavailable, match="native Windows process"):
            choose_backend(_settings(backend="win32"))


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
