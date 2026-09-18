"""Naming what a platform has, in text a reader of the output will see.

Naming another platform's mechanism is worse than saying nothing: a Windows
reader told a control "offered AT-SPI no click action" goes looking for an
accessibility bus their machine has never had.
"""

from pyguitest_recorder.platforms import (
    element_api,
    foreign_element_reason,
    foreign_focus_reason,
    is_windows,
)


class TestIsWindows:
    def test_a_recorded_windows_session_is_recognised(self):
        # The str() of pyguitest's own enum member, which is what
        # Environment.session_type stores.
        assert is_windows("SessionType.WIN32")

    def test_other_session_types_are_not(self):
        for session in ("SessionType.X11", "SessionType.WAYLAND", "xwayland", ""):
            assert not is_windows(session)

    def test_the_match_is_case_insensitive(self):
        assert is_windows("sessiontype.win32")

    def test_none_asks_about_this_machine(self, monkeypatch):
        import pyguitest_recorder.platforms as platforms

        monkeypatch.setattr(platforms.sys, "platform", "win32")
        assert is_windows()
        monkeypatch.setattr(platforms.sys, "platform", "linux")
        assert not is_windows()


class TestElementApi:
    def test_windows_publishes_elements_through_ui_automation(self):
        assert element_api("SessionType.WIN32") == "UI Automation"

    def test_everywhere_else_is_atspi(self):
        assert element_api("SessionType.X11") == "AT-SPI"


class TestForeignReasons:
    def test_windows_never_mentions_an_x_display_or_the_bus(self):
        for reason in (
            foreign_focus_reason("SessionType.WIN32"),
            foreign_element_reason("SessionType.WIN32"),
        ):
            assert "X display" not in reason
            assert "accessibility bus" not in reason
            assert "UI Automation" in reason

    def test_linux_keeps_the_explanation_that_is_true_there(self):
        for reason in (
            foreign_focus_reason("SessionType.X11"),
            foreign_element_reason("SessionType.X11"),
        ):
            assert "X display" in reason
