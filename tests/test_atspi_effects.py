"""The AT-SPI effect log's query logic, without a bus.

The listener half needs a live accessibility bus and is exercised by the live
Wayland validation (docs/developers/status.md); the `nearest` matcher is pure
and is pinned here by populating the log directly.
"""

from __future__ import annotations

import pytest

from pyguitest_recorder.windows.atspi_effects import AtspiEffect, AtspiEffectLog


def _put(log, **kw):
    base = {
        "at": 1000.0,
        "name": "Save",
        "role": "push button",
        "pid": 77,
        "toplevel": "Target",
    }
    base.update(kw)
    log._effects.append(AtspiEffect(**base))


def test_nearest_binds_an_effect_just_after_the_press():
    log = AtspiEffectLog()
    _put(log, at=1000.3, name="Choosers", role="page tab")
    got = log.nearest(after=1000.2, within=0.6, toplevel="Target")
    assert got is not None and got.name == "Choosers"


def test_nearest_takes_the_earliest_in_the_window():
    # The first reaction to the press is the target; later effects are its
    # consequences (a tab selected, then its first child focused).
    log = AtspiEffectLog()
    _put(log, at=1000.5, name="Child", role="entry")
    _put(log, at=1000.2, name="Choosers", role="page tab")
    got = log.nearest(after=1000.0, within=0.6, toplevel="Target")
    assert got is not None and got.name == "Choosers"


def test_nearest_ignores_effects_before_the_press():
    log = AtspiEffectLog()
    _put(log, at=999.0, name="Earlier")
    assert log.nearest(after=1000.0, within=0.6, toplevel="Target") is None


def test_nearest_ignores_effects_past_the_window():
    log = AtspiEffectLog()
    _put(log, at=1002.0, name="Later")
    assert log.nearest(after=1000.0, within=0.6, toplevel="Target") is None


def test_nearest_is_scoped_to_the_toplevel():
    log = AtspiEffectLog()
    _put(log, at=1000.3, toplevel="Other")
    assert log.nearest(after=1000.0, within=0.6, toplevel="Target") is None


def test_nearest_skips_a_window_role_source():
    # A frame/dialog that took focus names no widget; it is not a click target.
    log = AtspiEffectLog()
    _put(log, at=1000.3, name="Target", role="frame")
    assert log.nearest(after=1000.0, within=0.6, toplevel="Target") is None


def test_nearest_skips_a_message_box_or_file_dialog_that_took_focus():
    # The two toplevel roles GTK and Qt publish besides frame/window/dialog
    # (pyguitest 0.21.0's `Role.WINDOW_ROLES`): a Save dialog taking focus
    # is no more a click target than a frame is.
    for role in ("alert", "file chooser"):
        log = AtspiEffectLog()
        _put(log, at=1000.3, name="Target", role=role)
        assert log.nearest(after=1000.0, within=0.6, toplevel="Target") is None, role


class _Node:
    """The slice of `Atspi.Accessible` `_toplevel_name` walks."""

    def __init__(self, role, name, parent=None):
        self._role, self._name, self._parent = role, name, parent

    def get_role_name(self):
        return self._role

    def get_name(self):
        return self._name

    def get_parent(self):
        return self._parent


def test_an_effect_in_a_file_dialog_is_tied_to_the_dialog():
    # Before `file chooser` and `alert` counted as toplevels the walk went
    # straight through a Save dialog to the application and named no
    # window, so `nearest(toplevel="Save")` never matched the effect.
    from pyguitest_recorder.windows.atspi_effects import AtspiEffectLog as Log

    app = _Node("application", "census")
    for role in ("file chooser", "alert"):
        dialog = _Node(role, "Save", parent=app)
        button = _Node("push button", "Cancel", parent=_Node("panel", "", dialog))
        assert Log._toplevel_name(button) == "Save", role
    unknown = _Node("push button", "Cancel", parent=_Node("panel", "", app))
    assert Log._toplevel_name(unknown) == ""


def test_nearest_skips_a_nameless_source():
    log = AtspiEffectLog()
    _put(log, at=1000.3, name="", role="list item")
    assert log.nearest(after=1000.0, within=0.6, toplevel="Target") is None


def test_start_degrades_to_false_when_atspi_cannot_initialise(monkeypatch):
    """A dead accessibility bus must leave clicks on coordinates, not raise.

    `Recorder.start` calls `AtspiEffectLog.start` with no try/except around it,
    so an `Atspi.init()` that throws (a session with no a11y bus) has to come
    back as a quiet False -- the whole effect-log is best-effort naming.
    """
    pytest.importorskip("gi")
    import gi

    try:
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError):
        pytest.skip("Atspi typelib not available")

    monkeypatch.setattr(Atspi, "init", lambda: (_ for _ in ()).throw(RuntimeError()))
    log = AtspiEffectLog()
    assert log.start() is False
    assert log._thread is None
