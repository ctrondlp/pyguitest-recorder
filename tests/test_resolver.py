"""Turning a recorded coordinate into a window, an element, or a fallback.

The failure side is most of it: which window a click is attributed to when
hit-testing misses, what a geometry read that raises leaves behind, the
bounded retry that covers a window still animating in, and the recorder's own
window never being the answer.
"""

import os
import sys

import pytest

from pyguitest_recorder.model import ElementRef
from pyguitest_recorder.platforms import (
    foreign_element_reason,
    foreign_focus_reason,
)
from pyguitest_recorder.windows import DesktopResolver, NullResolver
from pyguitest_recorder.windows import resolver as resolver_module


class FakeClock:
    """The clock a remembered window lookup ages by, moved by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Frozen for every test here, so counting lookups never depends on speed.

    A remembered answer expires after a fraction of a second of real time, which
    is longer than any of these runs take and shorter than a stalled CI machine
    is willing to promise. Tests about that expiry ask for this and move it.
    """
    made = FakeClock()
    monkeypatch.setattr(resolver_module, "_now", made)
    return made


class FakeWindow:
    def __init__(self, title="Example", app_id="org.example.App", pid=1234):
        self.title = title
        self.app_id = app_id
        self.pid = pid


class FakeSession:
    """Stands in for a pyguitest Session, without touching a desktop."""

    def __init__(self, window=None, geometry=(100, 50, 800, 600), fail=()):
        self.window = window or FakeWindow()
        self._geometry = geometry
        self.fail = set(fail)

    def window_at(self, x, y, screen=0):
        if "window_at" in self.fail:
            raise RuntimeError("no WINDOW_AT_POINT on this backend")
        return self.window

    def active_window(self):
        if "active_window" in self.fail:
            raise RuntimeError("nope")
        return self.window

    def geometry(self, window):
        if "geometry" in self.fail:
            raise RuntimeError("no WINDOW_GEOMETRY")
        return self._geometry


class CountingSession(FakeSession):
    """A session that counts the two expensive questions separately.

    A window lookup on this stack is a window list with a geometry read per
    window -- see `resolve_window` -- so a test that only counted calls would
    not be able to tell one lookup from one geometry read, which is the
    difference the motion path lives on.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.window_lookups = 0
        self.geometry_reads = 0

    def window_at(self, x, y, screen=0):
        self.window_lookups += 1
        return super().window_at(x, y, screen)

    def geometry(self, window):
        self.geometry_reads += 1
        return super().geometry(window)


class StackedSession:
    """A desktop of overlapping windows, with a hit test that honours the stack.

    `stack` is bottom to top, the order pyguitest's own `windows()` returns, and
    the last window covering a point is the one under it -- which is what
    `Session.window_at` answers. The single-window fakes above cannot say
    whether a lookup found the window that is *on top*, which is the whole of
    what a cached answer has to get right.
    """

    def __init__(self, *stack):
        self.stack = list(stack)
        self.window_lookups = 0

    def window_at(self, x, y, screen=0):
        self.window_lookups += 1
        found = None
        for window, (wx, wy, width, height) in self.stack:
            if wx <= x < wx + width and wy <= y < wy + height:
                found = window
        return found

    def active_window(self):
        return self.stack[-1][0]

    def geometry(self, window):
        return dict(self.stack)[window]


def desktop_with_an_app_on_it():
    """A full-screen desktop with an application window drawn over part of it."""
    desktop = FakeWindow(title="Desktop", app_id="org.example.Desktop", pid=1)
    app = FakeWindow(title="App", app_id="org.example.App", pid=2)
    return StackedSession(
        (desktop, (0, 0, 1000, 800)),
        (app, (200, 100, 400, 300)),
    )


def resolver(session, **kwargs):
    return DesktopResolver(session=session, elements=False, **kwargs)


def test_null_resolver_returns_bare_coordinates():
    target = NullResolver().resolve(5, 6)
    assert (target.x, target.y) == (5, 6)
    assert target.window is None and target.element is None


def test_null_resolver_resolves_a_window_only_point_the_same_way():
    # This floor has no windows to offer either way, so it is the same answer
    # the full resolve gives -- but it has to exist, because the recorder asks
    # for it whenever it records motion.
    target = NullResolver().resolve_window(5, 6)
    assert (target.x, target.y) == (5, 6)
    assert target.window is None and target.element is None


def test_window_and_geometry_are_captured():
    target = resolver(FakeSession()).resolve(420, 315)
    assert target.window.app_id == "org.example.App"
    assert target.window.geometry == (100, 50, 800, 600)
    assert target.relative == (320, 265)


def test_hit_test_failure_falls_back_to_the_active_window():
    # A point the focused window actually covers; the geometry is
    # (100, 50, 800, 600), and a point outside it is a different test below.
    target = resolver(FakeSession(fail={"window_at"})).resolve(150, 100)
    assert target.window is not None
    assert target.window.title == "Example"


def test_geometry_failure_still_yields_a_window():
    target = resolver(FakeSession(fail={"geometry"})).resolve(1, 2)
    assert target.window is not None
    assert target.window.geometry is None
    assert target.relative is None


def test_total_window_failure_degrades_to_coordinates():
    target = resolver(FakeSession(fail={"window_at", "active_window"})).resolve(7, 8)
    assert target.window is None
    assert (target.x, target.y) == (7, 8)


def test_a_transient_total_miss_is_retried_and_can_still_resolve(monkeypatch):
    # Seen live on KDE: dismissing GNOME Text Editor's own in-window "Discard
    # changes?" sheet -- two clicks close together in time -- came back with
    # no window attribution at all, every time, even though the window was
    # real and already active moments later. Most likely a slow kdotool
    # subprocess round trip racing a fast second click, not a genuinely
    # missing window -- a short, bounded retry on a total miss should
    # recover from that instead of giving up on the first empty answer.
    monkeypatch.setattr(resolver_module.time, "sleep", lambda seconds: None)
    session = FakeSession(fail={"window_at"})
    attempts = []
    real_active_window = session.active_window

    def flaky_active_window():
        attempts.append(None)
        if len(attempts) < 2:
            raise RuntimeError("kdotool timed out")
        return real_active_window()

    session.active_window = flaky_active_window
    target = resolver(session).resolve(150, 100)
    assert target.window is not None
    assert target.window.title == "Example"
    assert len(attempts) == 2


def test_the_retry_gives_up_after_a_bounded_number_of_attempts(monkeypatch):
    sleeps = []
    monkeypatch.setattr(resolver_module.time, "sleep", sleeps.append)
    target = resolver(FakeSession(fail={"window_at", "active_window"})).resolve(7, 8)
    assert target.window is None
    # One initial attempt plus two retries -- not retried forever.
    assert sleeps == [0.05, 0.05]


def test_no_session_means_no_window_context():
    target = resolver(None).resolve(7, 8)
    assert target.window is None


def test_the_recorders_own_window_is_never_the_target():
    session = FakeSession(window=FakeWindow(pid=os.getpid()))
    assert resolver(session).resolve(1, 2).window is None


def test_extra_ignored_pids_are_honoured():
    session = FakeSession(window=FakeWindow(pid=4321))
    assert resolver(session, ignore_pids={4321}).resolve(1, 2).window is None


class ListingSession(FakeSession):
    """A session that can also list every window, which `windows()` needs."""

    def __init__(self, windows, **kwargs):
        super().__init__(window=windows[0], **kwargs)
        self._windows = windows

    def windows(self):
        return list(self._windows)


class LeaderSession(FakeSession):
    """pluma under marco: the hit test names the window, the active window is a leader.

    GTK maps a 1x1, untitled, pid-less leader window beside every application,
    and here `_NET_ACTIVE_WINDOW` names it, a pixel from the real window's corner.
    """

    def __init__(self):
        self.real = FakeWindow(
            title="Unsaved Document 1 - Pluma", app_id="Pluma", pid=12163
        )
        self.leader = FakeWindow(title="", app_id="", pid=None)
        super().__init__(window=self.real)
        self.rects = {
            id(self.real): (315, 165, 650, 500),
            id(self.leader): (314, 164, 1, 1),
        }

    def active_window(self):
        return self.leader

    def geometry(self, window):
        return self.rects[id(window)]


def test_a_one_pixel_active_window_is_not_preferred_over_the_hit_test():
    # Found live: the File menu of pluma under marco -- 20px from the window's
    # corner, inside the decoration slack of the 1x1 leader window that was the
    # "active" one -- resolved to a window with no title and no size, so the
    # first click of the recording became an absolute coordinate and the
    # window looked as if it had only just opened.
    target = resolver(LeaderSession()).resolve(334, 177)
    assert target.window is not None
    assert target.window.title == "Unsaved Document 1 - Pluma"
    assert target.window.geometry == (315, 165, 650, 500)


def test_the_terminal_the_recorder_runs_in_is_never_the_target(monkeypatch):
    # The recorder has no window of its own: it runs in a terminal, and that
    # terminal's pid is what the window carries. Seen live on KDE -- typing
    # into Text Editor was attributed to the Konsole the recorder was running
    # in, and the script then waited for a window titled after that terminal's
    # foreground process, which reads differently at replay.
    terminal = FakeWindow(title="pyguitest-recorder : bash", pid=4649)
    monkeypatch.setattr(resolver_module, "_ancestor_pids", lambda *a, **k: [4649])
    made = resolver(ListingSession([terminal]))
    assert 4649 in made.ignore_pids
    assert made.resolve(1, 2).window is None


def test_the_walk_stops_at_the_terminal_and_never_reaches_the_shell(monkeypatch):
    # Walking the whole ancestry would reach the session's own shell, and
    # ignoring plasmashell/gnome-shell would blind the recorder to the panels
    # and menus it most needs to see.
    terminal = FakeWindow(title="Konsole", pid=4649)
    shell = FakeWindow(title="plasmashell", pid=3131)
    monkeypatch.setattr(resolver_module, "_ancestor_pids", lambda *a, **k: [4649, 3131])
    made = resolver(ListingSession([terminal, shell]))
    assert 4649 in made.ignore_pids
    assert 3131 not in made.ignore_pids


def test_an_ancestry_owning_no_window_is_ignored_no_further(monkeypatch):
    # The recorder driven over SSH has no terminal on this desktop at all.
    window = FakeWindow(title="Example", pid=999)
    monkeypatch.setattr(
        resolver_module, "_ancestor_pids", lambda *a, **k: [111, 222, 333]
    )
    made = resolver(ListingSession([window]))
    assert made.ignore_pids == {os.getpid()}
    assert made.resolve(1, 2).window is not None


def test_a_console_hosted_outside_the_process_tree_is_still_the_terminal(monkeypatch):
    """A console's owner is ignored even when it is in none of the ancestry.

    Measured on Windows 11 with a console started through Explorer: the
    launching chain was python -> cmd.exe -> explorer.exe -> svchost.exe, and
    the window that hosted the console belonged to WindowsTerminal.exe, which
    is in none of it -- Windows Terminal is launched by the system, not by the
    process that asked for a console. The ancestry walk found no terminal, and
    the recorder recorded its own console window.
    """
    terminal = FakeWindow(title=r"C:\WINDOWS\system32\cmd.exe", pid=13492)
    other = FakeWindow(title="Example", pid=999)
    monkeypatch.setattr(resolver_module, "_ancestor_pids", lambda *a, **k: [3660, 8088])
    monkeypatch.setattr(resolver_module, "_console_owner_pid", lambda: 13492)
    made = resolver(ListingSession([terminal, other]))
    assert 13492 in made.ignore_pids
    assert 999 not in made.ignore_pids


def test_the_console_owner_and_an_ancestor_terminal_are_both_ignored(monkeypatch):
    """A terminal that is an ancestor keeps being found by the walk.

    The console answer adds to it rather than replacing it.
    """
    editor = FakeWindow(title="Editor", pid=4649)
    terminal = FakeWindow(title="Terminal", pid=13492)
    monkeypatch.setattr(resolver_module, "_ancestor_pids", lambda *a, **k: [4649])
    monkeypatch.setattr(resolver_module, "_console_owner_pid", lambda: 13492)
    made = resolver(ListingSession([editor, terminal]))
    assert {4649, 13492} <= made.ignore_pids


def test_a_console_owner_with_no_listed_window_is_not_ignored(monkeypatch):
    """A console owner is held to the rule an ancestor is: it must own a window.

    A pid counts only if it owns a window this session lists. A console answer
    naming some other process must not put an arbitrary pid on the ignore list.
    """
    window = FakeWindow(title="Example", pid=999)
    monkeypatch.setattr(resolver_module, "_ancestor_pids", lambda *a, **k: [])
    monkeypatch.setattr(resolver_module, "_console_owner_pid", lambda: 77777)
    made = resolver(ListingSession([window]))
    assert made.ignore_pids == {os.getpid()}


def test_no_console_answer_changes_nothing(monkeypatch):
    """No console owner leaves the ignore set as the ancestry alone made it.

    That is a GUI launcher, a service, or a platform with no such thing.
    """
    terminal = FakeWindow(title="Konsole", pid=4649)
    monkeypatch.setattr(resolver_module, "_ancestor_pids", lambda *a, **k: [4649])
    monkeypatch.setattr(resolver_module, "_console_owner_pid", lambda: None)
    made = resolver(ListingSession([terminal]))
    assert made.ignore_pids == {os.getpid(), 4649}


def test_the_console_owner_is_none_off_windows(monkeypatch):
    """Off Windows there is no console owner to ask about."""
    monkeypatch.setattr(resolver_module.sys, "platform", "linux")
    assert resolver_module._console_owner_pid() is None


@pytest.mark.skipif(sys.platform != "win32", reason="asks the real Windows API")
def test_the_console_owner_call_is_safe_on_a_real_windows_process():
    """Asking the real Windows API gives a pid or None, never an exception.

    Whatever this process's console is -- a real one, a pseudoconsole, or none
    at all under a CI runner or an IDE.
    """
    answer = resolver_module._console_owner_pid()
    assert answer is None or (isinstance(answer, int) and answer > 0)


def test_app_id_shared_by_another_open_window_is_flagged_ambiguous():
    # Seen live on KDE: a desktop shell's own desktop, panels, and popups can
    # all report the same app_id ("plasmashell"), so finding a window by that
    # alone can silently land on the wrong one of several.
    popup = FakeWindow(title="", app_id="plasmashell", pid=555)
    panel = FakeWindow(title="plasmashell", app_id="plasmashell", pid=555)
    target = resolver(ListingSession([popup, panel])).resolve(1, 2)
    assert target.window.app_id_ambiguous is True


def test_app_id_unique_among_open_windows_is_not_ambiguous():
    window = FakeWindow(app_id="org.example.App", pid=555)
    other = FakeWindow(title="Other", app_id="org.other.App", pid=777)
    target = resolver(ListingSession([window, other])).resolve(1, 2)
    assert target.window.app_id_ambiguous is False


def test_app_id_ambiguity_ignores_windows_owned_by_ignored_pids():
    # The recorder's own terminal (or another ignored process) sharing an
    # app_id with the real target should not taint the target as ambiguous.
    window = FakeWindow(app_id="konsole", pid=555)
    own_terminal = FakeWindow(title="recorder", app_id="konsole", pid=os.getpid())
    target = resolver(ListingSession([window, own_terminal])).resolve(1, 2)
    assert target.window.app_id_ambiguous is False


def test_windows_listing_failure_during_ambiguity_check_defaults_to_not_ambiguous():
    class NoListing(FakeSession):
        def windows(self):
            raise RuntimeError("no WINDOW_LIST on this backend")

    target = resolver(NoListing()).resolve(1, 2)
    assert target.window.app_id_ambiguous is False


def test_a_drifting_title_is_marked_unstable():
    session = FakeSession()
    context = resolver(session)
    first = context.resolve(1, 2)
    assert first.window.title_stable is True

    session.window.title = "notes.txt - Example"
    second = context.resolve(1, 2)
    assert second.window.title_stable is False


def test_a_steady_title_stays_stable():
    context = resolver(FakeSession())
    context.resolve(1, 2)
    assert context.resolve(3, 4).window.title_stable is True


def test_a_title_is_stripped_of_surrounding_whitespace():
    # Seen live on KDE: the same window's title came back with a trailing
    # space from one backend and without one from another, so a recorded
    # "Desktop @ QRect(0,0 1920x1080) " never matched the identical-looking
    # window `wait_for_window` found at replay -- a meaningless whitespace
    # difference should not be able to break a title match.
    session = FakeSession(window=FakeWindow(title="  Desktop @ QRect(0,0 1920x1080) "))
    window = resolver(session).resolve(1, 2).window
    assert window.title == "Desktop @ QRect(0,0 1920x1080)"


def test_whitespace_only_title_changes_do_not_count_as_drift():
    session = FakeSession(window=FakeWindow(title="Example"))
    context = resolver(session)
    first = context.resolve(1, 2)
    assert first.window.title_stable is True

    session.window.title = "  Example  "
    second = context.resolve(3, 4)
    assert second.window.title_stable is True
    assert second.window.title == "Example"


# -- session leakage ---------------------------------------------------------


def leak_resolver(
    element_pid, window_pid, window_app_id="org.example.App", path=(), **kwargs
):
    """A resolver whose window and element deliberately may not agree.

    `window_app_id` is the window's class name as Windows reports it, which is
    what `_in_a_frame_host` reads -- "ApplicationFrameWindow" for a Store app
    and anything else for an ordinary one. `path` is the element's own
    ancestry, empty by default like an element recorded before that field
    existed.
    """
    session = FakeSession(
        window=FakeWindow(title="Target", app_id=window_app_id, pid=window_pid)
    )
    made = DesktopResolver(session=session, elements=False, **kwargs)
    made._resolves_elements = True
    made._element = lambda x, y: ElementRef(
        role="push button", name="Save", pid=element_pid, path=path
    )
    return made


def test_an_element_from_another_process_is_refused(monkeypatch):
    # The accessibility bus is session-scoped, not display-scoped, so a
    # recorder capturing a private X server is answered about applications on
    # every other one -- and both were asked about the same coordinate, so the
    # wrong answer is indistinguishable from the right one.
    #
    # Pinned to Linux, because that reasoning is Linux's. On Windows a pid
    # mismatch is what a correctly resolved UWP widget looks like -- see
    # TestTheUwpProcessSplit -- so unpinned this failed there for no defect.
    import pyguitest_recorder.platforms as platforms

    monkeypatch.setattr(platforms.sys, "platform", "linux")
    made = leak_resolver(element_pid=4242, window_pid=77)
    target = made.resolve(100, 100)
    assert target.element is None
    assert target.window is not None
    assert any("pid 4242" in warning for warning in made.warnings)


def test_the_mismatch_is_warned_about_once_not_per_event(monkeypatch):
    # Linux, for the reason the test above pins it.
    import pyguitest_recorder.platforms as platforms

    monkeypatch.setattr(platforms.sys, "platform", "linux")
    made = leak_resolver(element_pid=4242, window_pid=77)
    for _ in range(5):
        made.resolve(100, 100)
    assert len(made.warnings) == 1


def stacked_resolver(windows, element_pid, **kwargs):
    """A resolver over listed windows, whose hit test answers for `element_pid`.

    The first window is the one under the pointer; the element is from whichever
    process `element_pid` names, which is what a hit test with no stacking order
    does when a smaller widget from a window underneath wins.
    """
    made = DesktopResolver(session=ListingSession(windows), elements=False, **kwargs)
    made._resolves_elements = True
    made._element = lambda x, y: ElementRef(
        role="push button", name="Save", pid=element_pid
    )
    return made


def test_an_element_from_a_window_stacked_underneath_is_not_blamed_on_a_session(
    monkeypatch,
):
    # Found live, two overlapping windows on one private X server: the note
    # said the element "came from another session", and it had come from the
    # window next door. The accessible tree has no stacking order, so the hit
    # test answers for whichever overlapping window has the smaller widget
    # there. The refusal is right; the explanation was not.
    import pyguitest_recorder.platforms as platforms

    monkeypatch.setattr(platforms.sys, "platform", "linux")
    top = FakeWindow(title="Top", pid=77)
    beneath = FakeWindow(title="Underneath", pid=4242)
    made = stacked_resolver([top, beneath], element_pid=4242)
    target = made.resolve(100, 100)
    assert target.element is None
    assert target.window is not None and target.window.title == "Top"
    (warning,) = made.warnings
    assert "pid 4242" in warning
    assert "'Underneath'" in warning
    assert "stacked underneath" in warning
    assert "another session" not in warning


def test_priming_takes_exactly_one_window_list(monkeypatch):
    """Its docstring promises one, and the ambiguity check used to ask again.

    `_identify` calls `_app_id_ambiguous`, which lists windows to see whether
    anything else shares the app id -- so priming N windows cost N+1 lists,
    not the one claimed, and on a desktop with many windows that is real
    startup latency for an answer already in hand.
    """
    import pyguitest_recorder.platforms as platforms

    monkeypatch.setattr(platforms.sys, "platform", "linux")
    windows = [
        FakeWindow(title="One", pid=11, app_id="shell"),
        FakeWindow(title="Two", pid=12, app_id="shell"),
        FakeWindow(title="Three", pid=13, app_id="editor"),
    ]
    made = stacked_resolver(windows, element_pid=11)
    calls = []
    original = made.session.windows

    def counted():
        calls.append(1)
        return original()

    made.session.windows = counted
    made.prime()
    assert len(calls) == 1, f"one window list, not {len(calls)}"
    # And the answer is still right: two windows share "shell", one does not.
    assert made._identify(windows[0]).app_id_ambiguous is True
    assert made._identify(windows[2]).app_id_ambiguous is False


def test_an_element_from_a_process_with_no_window_here_names_both_causes(
    monkeypatch,
):
    """The window list tells the stacking cause apart; it cannot tell these two.

    A process the window list does not mention is off the recorded display,
    and there are two ways to be: a native Wayland window in this same
    session -- invisible to XRecord and to the X window list, while publishing
    to the same accessibility bus -- or a genuinely separate login session.
    Nothing available here distinguishes them, and the note used to assert the
    second, which on a Wayland desktop is usually the wrong one. Recording
    gedit on GNOME Shell 51.rc raised it for GNOME Shell's own widgets, from
    the session the recording was being made in.
    """
    import pyguitest_recorder.platforms as platforms

    monkeypatch.setattr(platforms.sys, "platform", "linux")
    made = stacked_resolver([FakeWindow(title="Top", pid=77)], element_pid=4242)
    assert made.resolve(100, 100).element is None
    (warning,) = made.warnings
    assert "native Wayland" in warning
    assert "another session" in warning
    assert "stacked underneath" not in warning


def test_the_recorders_own_terminal_is_not_called_a_window_underneath(monkeypatch):
    # A window the recorder deliberately ignores is not one of the recording's
    # windows, so an element from it is not "stacked underneath" anything.
    import pyguitest_recorder.platforms as platforms

    monkeypatch.setattr(platforms.sys, "platform", "linux")
    terminal = FakeWindow(title="Terminal", pid=4242)
    made = stacked_resolver(
        [FakeWindow(title="Top", pid=77), terminal],
        element_pid=4242,
        ignore_pids={4242},
    )
    assert made.resolve(100, 100).element is None
    (warning,) = made.warnings
    assert "stacked underneath" not in warning


def test_an_element_from_the_same_process_is_kept():
    target = leak_resolver(element_pid=77, window_pid=77).resolve(100, 100)
    assert target.element is not None
    assert target.element.name == "Save"


def test_a_same_process_element_from_a_different_toplevel_is_refused():
    """A pid match is not a window match -- one process owns several at once.

    Found live: mate-calc's Help > About opened a second toplevel almost the
    same size as 'Calculator' and at the same origin, both pid 6700 -- the
    window list correctly told them apart, but the click still resolved to
    the Calculator's own '=' button, because AT-SPI's hit test has no idea a
    dialog is stacked above its parent. The element's own `path` is what
    settles it: its toplevel ancestor is named 'Calculator', not the 'About
    MATE Calculator' the window list resolved for the same point.
    """
    made = leak_resolver(
        element_pid=77,
        window_pid=77,
        path=(("frame", "Calculator"),),
    )
    made.session.window.title = "About MATE Calculator"
    target = made.resolve(100, 100)
    assert target.element is None
    assert target.window is not None and target.window.title == "About MATE Calculator"
    (warning,) = made.warnings
    assert "'Calculator'" in warning
    assert "'About MATE Calculator'" in warning


def test_an_open_popups_item_is_kept_though_its_path_names_the_parent_frame():
    """The exemption without which the toplevel check undoes `_popup_at`.

    A GTK menu is an override-redirect window of its own, which the window
    list names after the process -- while its items stay published as
    descendants of the frame owning the menu bar. So every item in an open
    menu has a path naming the parent frame and a window under the point
    naming the popup: a disagreement in form only. Caught live, the same
    afternoon the check was written, when `gui.menu_item("Open").click()`
    degraded to a bare coordinate.
    """
    item_path = (("frame", "Probe Window"), ("menu bar", ""), ("menu", "File"))
    made = leak_resolver(element_pid=77, window_pid=77, path=item_path)
    made.session.window.title = "gtk_probe_window.py"

    def from_open_popup(x, y):
        """Stand in for `_element` answering out of `_popup_at`.

        That path is what sets `_from_popup`, and carries the rectangle the
        item had while the popup was open.
        """
        made._from_popup = True
        return ElementRef(
            role="menu item",
            name="Open",
            pid=77,
            path=item_path,
            extents=(10, 20, 80, 24),
        )

    made._element = from_open_popup
    target = made.resolve(15, 25)
    assert target.element is not None
    assert target.element.name == "Open"
    assert not made.warnings


def test_a_same_process_element_with_no_path_is_kept():
    # An element recorded before `path` existed, or a bridge that never
    # populates it -- no evidence of a mismatch, so none is assumed.
    target = leak_resolver(element_pid=77, window_pid=77, path=()).resolve(100, 100)
    assert target.element is not None


def test_a_same_process_element_whose_path_agrees_is_kept():
    made = leak_resolver(
        element_pid=77,
        window_pid=77,
        path=(("frame", "Target"),),
    )
    target = made.resolve(100, 100)
    assert target.element is not None


def test_a_same_process_element_is_kept_when_the_window_has_no_title():
    made = leak_resolver(
        element_pid=77,
        window_pid=77,
        path=(("frame", "Something Else"),),
    )
    made.session.window.title = ""
    target = made.resolve(100, 100)
    assert target.element is not None


def test_a_same_process_element_is_kept_once_the_window_title_has_drifted():
    # VITAL -- keep this test. `window.title` is anchored to the *first*
    # title a window was seen with (see `_identify`), but an element's own
    # `path` is read live at click time -- so once a title has drifted (a
    # text editor gaining an unsaved-changes marker, GNOME Text Editor
    # renaming itself the moment it has content), the two are no longer
    # comparable, and a same-window click would otherwise be misread as
    # landing in a different toplevel and downgraded to a bare coordinate.
    made = leak_resolver(
        element_pid=77,
        window_pid=77,
        path=(("frame", "Untitled Document 1 - gedit"),),
    )
    made.session.window.title = "Untitled Document 1 - gedit"
    first = made.resolve(100, 100)
    assert first.element is not None

    made._element = lambda x, y: ElementRef(
        role="push button",
        name="Save",
        pid=77,
        path=(("frame", "*Untitled Document 1 - gedit"),),
    )
    made.session.window.title = "*Untitled Document 1 - gedit"
    second = made.resolve(100, 100)
    assert second.element is not None
    assert second.window is not None
    assert second.window.title == "Untitled Document 1 - gedit"  # still anchored
    assert not second.window.title_stable


def test_a_same_process_element_is_still_refused_after_the_window_stays_stable():
    # The drift exemption above must not swallow a genuine mismatch: with no
    # drift ever observed, title_stable stays True and the check still runs.
    made = leak_resolver(
        element_pid=77,
        window_pid=77,
        path=(("frame", "Calculator"),),
    )
    made.session.window.title = "About MATE Calculator"
    target = made.resolve(100, 100)
    assert target.element is None


def test_the_nearest_toplevel_ancestor_is_checked_not_the_outermost():
    # `_best_owner` reads `path` nearest-to-element first, for the same
    # reason this does: a dialog nested under another toplevel (plausible on
    # Qt/KDE, unlike GTK's flatter sibling toplevels) must be judged by the
    # one the element is actually inside. A path naming the right outer
    # frame first and a mismatched dialog second, closest to the element,
    # must still be refused.
    made = leak_resolver(
        element_pid=77,
        window_pid=77,
        path=(("frame", "Target"), ("dialog", "About")),
    )
    target = made.resolve(100, 100)
    assert target.element is None
    (warning,) = made.warnings
    assert "'About'" in warning


def test_an_element_with_no_pid_is_kept_rather_than_lost():
    # Plenty of bridges publish no process id, and refusing every element on
    # those desktops would cost far more than the mismatch it prevents.
    target = leak_resolver(element_pid=None, window_pid=77).resolve(100, 100)
    assert target.element is not None


def test_the_check_is_skipped_when_the_window_has_no_pid():
    target = leak_resolver(element_pid=4242, window_pid=None).resolve(100, 100)
    assert target.element is not None


def test_an_element_with_no_window_to_corroborate_it_is_refused():
    # A window lookup that ran and found nothing is evidence, not silence: on
    # a display with no window under the pointer, an accessible under that
    # same point came from some other session's applications.
    made = DesktopResolver(session=FakeSession(window=None), elements=False)
    made.session.window = None
    made._resolves_elements = True
    made._element = lambda x, y: ElementRef(role="panel", name="", pid=4242)
    target = made.resolve(100, 100)
    assert target.window is None
    assert target.element is None
    # The refusal is what this test is about, not the sentence: the reason is
    # worded per platform (see platforms.foreign_element_reason), so matching
    # the Linux phrasing here failed the suite on Windows for no defect.
    assert any("ignored accessible elements" in warning for warning in made.warnings)
    assert any(foreign_element_reason() in warning for warning in made.warnings)


def test_without_window_context_at_all_the_element_is_kept():
    # Nothing to verify against, and one display is the normal case.
    made = DesktopResolver(session=None, elements=False)
    made._resolves_elements = True
    made._element = lambda x, y: ElementRef(role="push button", name="Save")
    assert made.resolve(100, 100).element is not None


def fitting_resolver(extents, geometry=(0, 0, 310, 263), scale=1.0):
    """A resolver whose window and element rectangles may not be compatible."""
    session = FakeSession(
        window=FakeWindow(title="Target", pid=None), geometry=geometry
    )
    session.screens = lambda: [type("S", (), {"scale": scale})()]
    made = DesktopResolver(session=session, elements=False)
    made._resolves_elements = True
    made._element = lambda x, y: ElementRef(role="panel", name="", extents=extents)
    return made


def test_an_element_bigger_than_its_window_is_refused():
    # 1920x1080 extents reported for a widget in a 310x263 window: the two
    # answers describe different screens. Seen live, recording a private X
    # server alongside a real desktop.
    made = fitting_resolver(extents=(0, 0, 1920, 1080))
    target = made.resolve(100, 100)
    assert target.element is None
    assert any("different screen" in warning for warning in made.warnings)


def test_an_element_inside_its_window_is_kept():
    target = fitting_resolver(extents=(10, 90, 100, 30)).resolve(100, 100)
    assert target.element is not None


def test_client_side_decoration_slack_is_allowed():
    target = fitting_resolver(extents=(-4, -4, 316, 269)).resolve(100, 100)
    assert target.element is not None


def test_the_fit_check_is_skipped_on_a_scaled_screen():
    # AT-SPI extents and window geometry are not reliably in the same units
    # there, and a false rejection costs every named element on the desktop.
    target = fitting_resolver(extents=(0, 0, 1920, 1080), scale=2.0).resolve(100, 100)
    assert target.element is not None


def test_a_matching_pid_settles_it_without_consulting_geometry():
    session = FakeSession(window=FakeWindow(title="Target", pid=77))
    made = DesktopResolver(session=session, elements=False)
    made._resolves_elements = True
    made._element = lambda x, y: ElementRef(
        role="panel", name="", pid=77, extents=(0, 0, 9999, 9999)
    )
    assert made.resolve(100, 100).element is not None


def hit_resolver(extents, scale=1.0):
    """A resolver whose AT-SPI hit-testing may be answering nonsense."""
    session = FakeSession(window=FakeWindow(title="Target", pid=77))
    session.screens = lambda: [type("S", (), {"scale": scale})()]
    made = DesktopResolver(session=session, elements=False)
    made._resolves_elements = True
    made._element = lambda x, y: ElementRef(
        role="label", name="Recorder Check", pid=77, extents=extents
    )
    return made


def test_an_element_that_does_not_cover_the_point_is_refused():
    # A GTK4 dialog reporting every widget at the origin returns the same
    # label for every point in the window. Believing it generates a click on
    # a label the pointer was nowhere near -- seen live.
    made = hit_resolver(extents=(0, 0, 252, 25))
    target = made.resolve(155, 131)
    assert target.element is None
    assert any("cannot be trusted" in warning for warning in made.warnings)


def test_an_element_that_covers_the_point_is_kept():
    assert hit_resolver(extents=(0, 100, 252, 60)).resolve(155, 131).element is not None


def test_the_cover_check_allows_the_same_decoration_slack():
    assert hit_resolver(extents=(0, 0, 252, 128)).resolve(155, 131).element is not None


def test_the_cover_check_is_skipped_on_a_scaled_screen():
    made = hit_resolver(extents=(0, 0, 252, 25), scale=2.0)
    assert made.resolve(155, 131).element is not None


def test_an_element_with_no_extents_is_still_trusted():
    made = hit_resolver(extents=None)
    assert made.resolve(155, 131).element is not None


def test_the_active_window_fallback_must_contain_the_point():
    # A click that misses every window resolved, through this fallback, to the
    # focused one -- and every coordinate under it came out relative to the
    # wrong origin. Seen live at (760, 500) against a window at (-160, 0).
    session = FakeSession(
        window=FakeWindow(title="Focused", pid=7),
        geometry=(-160, 0, 310, 263),
        fail=["window_at"],
    )
    made = DesktopResolver(session=session, elements=False)
    target = made.resolve(760, 500)
    assert target.window is None
    assert any("does not cover" in warning for warning in made.warnings)


def test_the_active_window_fallback_is_kept_where_it_does_contain_the_point():
    session = FakeSession(
        window=FakeWindow(title="Focused", pid=7),
        geometry=(0, 0, 800, 600),
        fail=["window_at"],
    )
    target = DesktopResolver(session=session, elements=False).resolve(100, 100)
    assert target.window is not None
    assert target.window.title == "Focused"


def test_the_fallback_is_trusted_when_there_is_no_geometry_to_check_it():
    session = FakeSession(
        window=FakeWindow(title="Focused", pid=7), fail=["window_at", "geometry"]
    )
    target = DesktopResolver(session=session, elements=False).resolve(760, 500)
    assert target.window is not None


def test_a_hit_test_answer_is_not_second_guessed():
    # window_at is a hit test; by construction it contains the point, and
    # doubting it would cost the one authoritative answer available.
    session = FakeSession(
        window=FakeWindow(title="Hit", pid=7), geometry=(-160, 0, 310, 263)
    )
    target = DesktopResolver(session=session, elements=False).resolve(760, 500)
    assert target.window is not None


# -- elements, now asked of the session --------------------------------------


class FakeCapability:
    """Stands in for one member of pyguitest's Capability enum."""

    def __init__(self, name):
        self.name = name


class FakeElement:
    def __init__(
        self,
        role,
        name,
        description="",
        pid=None,
        parent=None,
        children=(),
        text=None,
        checked=None,
        checkable=False,
        actions=(),
        expanded=None,
        selectable=None,
    ):
        self.role = role
        self.name = name
        self.description = description
        self.pid = pid
        self.parent = parent
        self.children = list(children)
        self.text = text
        self.checked = checked
        self.checkable = checkable
        self.actions = list(actions)
        self.expanded = expanded
        self.selectable = selectable


class ElementSession(FakeSession):
    """A session that answers about elements as well as windows."""

    def __init__(
        self,
        element=None,
        capabilities=("WINDOW_LIST", "ELEMENT_GEOMETRY"),
        extents=(120, 120, 60, 24),
        tree_error=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        frame = FakeElement("frame", "Target")
        self.element = element or FakeElement(
            "push button", "Save", description="Save the file", pid=77, parent=frame
        )
        self._capabilities = capabilities
        self._extents = extents
        self._tree_error = tree_error

    @property
    def capabilities(self):
        return {FakeCapability(name) for name in self._capabilities}

    def root_element(self):
        if self._tree_error is not None:
            raise self._tree_error
        return FakeElement("desktop frame", "main")

    def element_at(self, x, y):
        if "element_at" in self.fail:
            raise RuntimeError("the tree went stale")
        return self.element

    def extents(self, element):
        if "extents" in self.fail:
            raise RuntimeError("no rectangle")
        return self._extents


def element_resolver(**kwargs):
    session = ElementSession(window=FakeWindow(title="Target", pid=77), **kwargs)
    return DesktopResolver(session=session, elements=True)


def test_resolving_a_window_only_never_asks_the_tree_for_an_element():
    # A move is rendered from its window and its coordinates and nothing else,
    # so this lookup has no buyer -- and it is the expensive half. One
    # accessibility round trip per motion event under `record_motion` is what
    # left a live recording consuming 20x slower than the hand making it.
    made = element_resolver()
    asked = []
    made.session.element_at = lambda x, y: asked.append((x, y))
    target = made.resolve_window(130, 130)
    assert asked == []
    assert target.window is not None
    assert target.element is None


def test_an_element_is_described_from_what_the_session_says():
    target = element_resolver().resolve(130, 130)
    assert target.element.role == "push button"
    assert target.element.name == "Save"
    assert target.element.description == "Save the file"
    assert target.element.pid == 77
    assert target.element.extents == (120, 120, 60, 24)


CLOSED_ITEM = (-2147483648, -2147483648, 233, 25)
"""Where AT-SPI puts a menu item whose popup is not open, as pluma reports it.
The position is `G_MININT` and the size is *kept* -- mate-calc's closed items are
1x1 instead -- which is what made a test on size alone call them all showing."""

MENU_BAR_ENTRY = (20, 10, 40, 20)
TOOLBAR_BUTTON = (100, 190, 300, 40)
NEW_ITEM = (100, 200, 100, 24)
OPEN_ITEM = (100, 230, 100, 24)
RECENT_ENTRY = (100, 260, 100, 24)


@pytest.fixture(autouse=True)
def no_popup_wait(monkeypatch):
    """Do not wait for a popup that these tests have already decided is closed."""
    monkeypatch.setattr(resolver_module, "POPUP_WAIT_SECONDS", 0)


class PopupSession(ElementSession):
    """A File menu whose popup is stacked over a toolbar button, as in pluma.

    The hit test cannot see into the popup, so over the popup it answers for the
    toolbar button beneath -- which is the whole trap. `elements(within=...)`
    does find the popup's items, and they have a rectangle only while it is open.
    """

    def __init__(self, popup_open=True):
        super().__init__(window=FakeWindow(title="Target", pid=77))
        frame = FakeElement("frame", "Target")
        self.popup_open = popup_open
        self.menu = FakeElement("menu", "File", pid=77, parent=frame)
        self.new = FakeElement("menu item", "New", pid=77, parent=self.menu)
        self.opened = FakeElement("menu item", "Open...", pid=77, parent=self.menu)
        # An entry that opens a submenu is a `menu` in GTK3, not a `menu item`.
        self.recent = FakeElement("menu", "Recent", pid=77, parent=self.menu)
        self.menu.children = [self.new, self.opened, self.recent]
        self.beneath = FakeElement(
            "button", "Open", pid=77, parent=frame, actions=["click"]
        )
        self.rects = {
            id(self.menu): MENU_BAR_ENTRY,
            id(self.beneath): TOOLBAR_BUTTON,
        }
        self.popup_open = popup_open
        self._place_popup()

    def _place_popup(self):
        for item, rect in (
            (self.new, NEW_ITEM),
            (self.opened, OPEN_ITEM),
            (self.recent, RECENT_ENTRY),
        ):
            self.rects[id(item)] = rect if self.popup_open else CLOSED_ITEM

    def close_popup(self):
        """What choosing an item does: every item loses its rectangle at once."""
        self.popup_open = False
        self._place_popup()

    def element_at(self, x, y):
        for element in (self.menu, self.beneath):
            rect = self.rects[id(element)]
            if rect[0] <= x < rect[0] + rect[2] and rect[1] <= y < rect[1] + rect[3]:
                return element
        return None

    def elements(self, within=None, predicate=None, **kwargs):
        if within is not self.menu:
            return []
        return [
            item for item in self.menu.children if predicate is None or predicate(item)
        ]

    def extents(self, element):
        return self.rects[id(element)]


def popup_resolver(**kwargs):
    # Pinned to a non-Windows recording, because the popup handling is Linux's:
    # unpinned, `windows=None` asks the host, and on the Windows CI job every one
    # of these would skip the logic they exist to test.
    return DesktopResolver(session=PopupSession(**kwargs), elements=True, windows=False)


def test_a_click_in_an_open_popup_is_named_for_the_item_not_the_widget_under_it():
    # Found live on MATE: File -> New in pluma was recorded as
    # `gui.button("Open").click()`. The toolbar button is the same process as the
    # window and its rectangle contains the point, which is everything the
    # resolver checks, so a script that opens a file dialog instead of making a
    # document came out clean.
    made = popup_resolver()
    assert made.resolve(30, 20).element.name == "File"
    target = made.resolve(150, 212)
    assert (target.element.role, target.element.name) == ("menu item", "New")
    assert target.element.extents == NEW_ITEM


def test_a_point_between_popup_items_is_a_coordinate_never_what_lies_beneath():
    # Inside the popup and on no item -- a separator. The toolbar button under it
    # contains the point, and is not what was clicked.
    made = popup_resolver()
    made.resolve(30, 20)
    assert made.resolve(150, 227).element is None
    assert made._menu_owner is not None, "the popup is still open"


def test_a_click_outside_the_popup_hit_tests_normally_and_forgets_the_menu():
    made = popup_resolver()
    made.resolve(30, 20)
    target = made.resolve(350, 210)
    assert (target.element.role, target.element.name) == ("button", "Open")
    assert made._menu_owner is None


def test_a_menu_that_is_closed_claims_no_points():
    # Its items report a rectangle at the far corner of the screen, so nothing
    # has to be told that the menu closed.
    made = popup_resolver(popup_open=False)
    made.resolve(30, 20)
    target = made.resolve(150, 212)
    assert (target.element.role, target.element.name) == ("button", "Open")
    assert made._menu_owner is None


def test_a_press_consumed_after_its_popup_closed_is_still_named_for_the_item():
    # The live failure the first version of this shipped with. Choosing an item
    # closes the menu, the recorder handles the press after it happened, and a
    # closed item has no rectangle -- so looking at the popup as it stood named
    # the toolbar button again, in every real recording, while every test that
    # left the popup open passed.
    made = popup_resolver()
    made.resolve(30, 20)
    made.session.close_popup()
    target = made.resolve(150, 212)
    assert (target.element.role, target.element.name) == ("menu item", "New")


def test_a_rest_on_the_menu_entry_consumed_late_does_not_forget_the_popup():
    # The second live failure, in the order events are actually consumed: the
    # press that opens the menu, then the rest the pointer sat in on that entry
    # -- consumed once the popup has already closed again -- then the press that
    # chooses from it. A rest outside the popup dismisses nothing, and finding
    # the same menu entry again must not replace the layout with nothing.
    made = popup_resolver()
    made.resolve(30, 20)
    made.session.close_popup()
    assert made.resolve_hover(30, 20).element.name == "File"
    assert made.resolve(150, 212).element.name == "New"


def test_a_rest_on_another_menu_entry_takes_over_when_its_popup_is_showing():
    # The pointer sliding along the menu bar with a menu open: GTK swaps the
    # popup as it goes, with no press.
    made = popup_resolver(popup_open=False)
    made.resolve_hover(30, 20)
    assert made._menu_owner is None
    made.session.popup_open = True
    made.session._place_popup()
    made.resolve_hover(30, 20)
    assert made._menu_owner is made.session.menu


def test_the_press_that_chose_an_item_uses_the_popup_up():
    # Or a later click on the toolbar button that was underneath, inside the
    # same rectangle, would be answered with a menu item that no longer exists.
    made = popup_resolver()
    made.resolve(30, 20)
    made.session.close_popup()
    assert made.resolve(150, 212).element.name == "New"
    assert made.resolve(150, 212).element.name == "Open"
    assert made._menu_owner is None


def test_a_rest_beside_the_press_does_not_use_the_popup_up():
    # The normalizer resolves the rest the pointer was in when the press arrives,
    # then the press itself, and both after the popup has closed. The rest is not
    # what chose anything.
    made = popup_resolver()
    made.resolve(30, 20)
    made.session.close_popup()
    assert made.resolve_hover(150, 212).element.name == "New"
    assert made.resolve(150, 212).element.name == "New"


def test_a_check_does_not_use_the_popup_up_either():
    made = popup_resolver()
    made.resolve(30, 20)
    made.inspect(150, 212)
    assert made._menu_owner is not None


def test_a_press_on_a_submenu_entry_leaves_the_popup_in_play():
    # It opens more menu rather than choosing anything, so the item picked from
    # it next is still inside the popup this layout describes.
    made = popup_resolver()
    made.resolve(30, 20)
    made.session.close_popup()
    assert made.resolve(150, 272).element.name == "Recent"
    assert made.resolve(150, 212).element.name == "New"


def test_a_remembered_popup_expires(clock):
    made = popup_resolver()
    made.resolve(30, 20)
    made.session.close_popup()
    clock.advance(resolver_module.POPUP_MEMORY_SECONDS + 1)
    assert made.resolve(150, 212).element.name == "Open"


def test_a_menu_that_went_stale_mid_read_falls_back_to_what_was_remembered():
    made = popup_resolver()
    made.resolve(30, 20)

    def gone(**kwargs):
        raise RuntimeError("no such object path")

    made.session.elements = gone
    assert made.resolve(150, 212).element.name == "New"


@pytest.mark.parametrize(
    "closed",
    [
        (-2147483648, -2147483648, 233, 25),  # pluma: sentinel position, real size
        (-2147483648, -2147483648, 1, 1),  # mate-calc: sentinel position, 1x1
    ],
)
def test_a_closed_item_is_recognised_by_its_position_not_its_size(closed):
    # Found live by tracing the real recorder: pluma's nine closed File items
    # kept their 233x25, so a size test called them all showing, their bounds
    # came out as the far corner of the screen, and a press inside the popup was
    # taken to be outside it and dismissed the layout it needed.
    made = popup_resolver()
    made.resolve(30, 20)
    made.session.close_popup()
    for item in (made.session.new, made.session.opened, made.session.recent):
        made.session.rects[id(item)] = closed
    assert made.resolve(150, 212).element.name == "New"


def test_a_menu_that_was_never_seen_open_has_nothing_to_remember():
    # Resting the pointer over a menu-bar entry, say: nothing opened.
    made = popup_resolver(popup_open=False)
    made.resolve_hover(30, 20)
    assert made.resolve(150, 212).element.name == "Open"


def test_a_check_recorded_on_a_popup_item_reads_the_item(monkeypatch):
    # The check re-reads what is under the point, so it has to look where the
    # click looked -- or it reports the element "changed between being named and
    # being read" for every check made inside a menu.
    made = popup_resolver()
    made.resolve(30, 20)
    observation = made.inspect(150, 212)
    assert observation.target.element.name == "New"
    assert not any("changed between" in warning for warning in made.warnings)


def test_windows_is_left_to_ui_automation_which_sees_popups_itself():
    made = DesktopResolver(session=PopupSession(), elements=True, windows=True)
    made.resolve(30, 20)
    assert made.resolve(150, 212).element.name == "Open"


def test_windows_never_pays_for_a_popup_it_will_not_use(monkeypatch):
    # The lookup was skipped there from the start, but tracking the menu and
    # waiting for its popup was not: every press on a menu would have cost up to
    # POPUP_WAIT_SECONDS and a subtree read over UI Automation, and been thrown
    # away. Nothing about Linux is asked for on Windows.
    monkeypatch.setattr(resolver_module, "POPUP_WAIT_SECONDS", 5)
    session = PopupSession(popup_open=False)
    reads = []
    session.elements = lambda **kwargs: reads.append(kwargs) or []
    made = DesktopResolver(session=session, elements=True, windows=True)
    slept = []
    monkeypatch.setattr(resolver_module.time, "sleep", slept.append)
    made.resolve(30, 20)
    made.resolve_hover(30, 20)
    assert reads == []
    assert slept == []
    assert made._menu_owner is None


def test_a_run_of_moves_in_one_window_looks_the_window_up_once():
    # The cost this removes: pyguitest's X11 hit test lists every window and
    # reads a rectangle for each one, so one lookup per motion event is a window
    # list per motion event. Found live: 572 motions over a 21-second session
    # left the recorder 4.6s behind the hand making it, and the stop key
    # unanswered until the backlog had been worked through.
    session = CountingSession(geometry=(100, 50, 800, 600))
    made = resolver(session)
    targets = [
        made.resolve_window(x, y) for x, y in ((120, 80), (300, 200), (700, 500))
    ]
    assert session.window_lookups == 1
    assert all(target.window is not None for target in targets)
    assert {target.window.geometry for target in targets} == {(100, 50, 800, 600)}


def test_every_move_still_reads_the_origin_it_is_rendered_against():
    # Caching the rectangle along with the identity would answer for where the
    # window *used* to be. Every coordinate is an offset into that origin, so a
    # window that moved has to be noticed exactly as it was before.
    session = CountingSession(geometry=(100, 50, 800, 600))
    made = resolver(session)
    made.resolve_window(300, 200)
    session._geometry = (250, 50, 800, 600)
    target = made.resolve_window(300, 200)
    assert target.window.geometry == (250, 50, 800, 600)
    assert session.window_lookups == 1
    assert session.geometry_reads == 2


def test_a_move_that_leaves_the_window_looks_it_up_again():
    # Outside the rectangle the cached window no longer covers the point, so the
    # question has to be asked again -- this is the case that keeps a run of
    # moves from being answered by a window it has left.
    session = CountingSession(geometry=(100, 50, 800, 600))
    made = resolver(session)
    made.resolve_window(300, 200)
    made.resolve_window(20, 20)
    assert session.window_lookups == 2


def test_a_window_that_moved_off_the_point_is_looked_up_again():
    # The cached window no longer covers the point, so the answer is stale even
    # though the identity is not: whatever is under the pointer now is the
    # window the coordinate belongs to.
    session = CountingSession(geometry=(100, 50, 800, 600))
    made = resolver(session)
    made.resolve_window(300, 200)
    session._geometry = (0, 0, 40, 40)
    made.resolve_window(300, 200)
    assert session.window_lookups == 2


def test_a_move_on_another_screen_is_looked_up_again():
    # A window on one screen says nothing about a point on another, and the
    # origin a coordinate is rendered against has to come from its own.
    session = CountingSession(geometry=(100, 50, 800, 600))
    made = resolver(session)
    made.resolve_window(300, 200, screen=0)
    made.resolve_window(300, 200, screen=1)
    assert session.window_lookups == 2


def test_a_window_whose_rectangle_cannot_be_read_still_resolves():
    # No rectangle means nothing to check the point against, so the hit test
    # stands in -- the point is attributed to a window with no origin rather
    # than dropped, which is what the generator needs to see to fall back to
    # absolute coordinates.
    session = CountingSession(fail=("geometry",))
    made = resolver(session)
    first = made.resolve_window(300, 200)
    second = made.resolve_window(300, 210)
    assert first.window is not None and second.window is not None
    assert second.window.geometry is None


def test_a_click_leaves_the_moves_after_it_a_warm_answer():
    # A click resolves the window fully anyway; keeping that answer is free, and
    # it means the pointer moving after a click does not pay for a hit test it
    # has just paid for.
    session = CountingSession(geometry=(100, 50, 800, 600))
    made = resolver(session)
    made.resolve(300, 200)
    made.resolve_window(300, 210)
    assert session.window_lookups == 1


def test_a_window_drawn_over_the_last_answer_is_noticed_once_it_expires(clock):
    # A rectangle containing the point does not say the window is the one *under*
    # it: the pointer starts over bare desktop, moves onto an application drawn
    # over it, and every move from there is still inside the desktop's rectangle.
    # Answered on containment alone they were all attributed to the desktop --
    # rendered against its origin, not the application's -- until the pointer left
    # the desktop altogether, which it never does. Found by comparing the moves
    # against what a full lookup answers for the same points.
    made = resolver(desktop_with_an_app_on_it())
    assert made.resolve_window(50, 50).window.title == "Desktop"
    assert made.resolve_window(250, 150).window.title == "Desktop"  # still trusted
    clock.advance(resolver_module.MOTION_TRUST_SECONDS)
    assert made.resolve_window(300, 200).window.title == "App"
    assert made.resolve_window(350, 250).window.title == "App"


def test_a_recent_answer_is_trusted_for_a_fraction_of_a_second_and_no_longer(clock):
    # The bound is what makes the saving honest: a few lookups a second where
    # there were hundreds, and a stacking change noticed within one interval.
    session = CountingSession(geometry=(100, 50, 800, 600))
    made = resolver(session)
    made.resolve_window(300, 200)
    clock.advance(resolver_module.MOTION_TRUST_SECONDS / 2)
    made.resolve_window(310, 200)
    assert session.window_lookups == 1
    clock.advance(resolver_module.MOTION_TRUST_SECONDS / 2)
    made.resolve_window(320, 200)
    assert session.window_lookups == 2


def test_a_move_over_bare_desktop_does_not_sleep_through_a_retry(monkeypatch):
    # A click that finds no window waits and asks again, because a window still
    # animating in is worth waiting for. A pointer move is one of hundreds and
    # the next will ask anyway: sleeping 50ms twice per move over bare desktop
    # is a tenth of a second each, a recorder that cannot keep up with a hand
    # on the one stretch of screen nothing is drawn.
    slept = []
    monkeypatch.setattr(resolver_module.time, "sleep", slept.append)
    made = resolver(FakeSession(fail={"window_at", "active_window"}))
    target = made.resolve_window(7, 8)
    assert slept == []
    assert target.window is None
    assert (target.x, target.y) == (7, 8)


def test_a_click_that_finds_no_window_still_waits_and_retries(monkeypatch):
    # The counterpart to the above, so the patience is not lost from the one
    # caller that has a use for it.
    slept = []
    monkeypatch.setattr(resolver_module.time, "sleep", slept.append)
    made = resolver(FakeSession(fail={"window_at", "active_window"}))
    made.resolve(7, 8)
    assert slept == [0.05, 0.05]


def test_a_miss_is_remembered_for_the_moves_that_follow_it(clock):
    # Bare desktop is where a pointer spends much of its time, and a miss is the
    # expensive answer -- the whole lookup, where a hit at least stops at the
    # first window that covers the point. A hit-only cache did nothing for it.
    session = CountingSession(fail=("window_at", "active_window"))
    made = resolver(session)
    for point in ((7, 8), (9, 8), (11, 9)):
        assert made.resolve_window(*point).window is None
    assert session.window_lookups == 1
    clock.advance(resolver_module.MOTION_TRUST_SECONDS)
    made.resolve_window(13, 9)
    assert session.window_lookups == 2


def test_a_miss_does_not_hide_a_window_from_a_click(clock):
    # Only the motion path reads the remembered answer. A click still resolves
    # the point it was made at, whatever the moves before it found.
    session = desktop_with_an_app_on_it()
    made = resolver(session)
    stack, session.stack = session.stack, []
    assert made.resolve_window(300, 200).window is None
    session.stack = stack
    assert made.resolve(300, 200).window.title == "App"


def test_an_element_with_no_actions_is_described_as_unclickable():
    # KDE's Kickoff menu categories are AT-SPI labels with an empty Action
    # interface -- confirmed live, not a fake session's guess -- and the
    # generator needs to see that to route around Element.click().
    element = FakeElement("label", "Office", pid=77, actions=())
    target = element_resolver(element=element).resolve(130, 130)
    assert target.element.actions == ()
    assert not target.element.clickable


def test_an_element_with_a_click_action_is_described_as_clickable():
    element = FakeElement("push button", "Save", pid=77, actions=("click",))
    target = element_resolver(element=element).resolve(130, 130)
    assert target.element.actions == ("click",)
    assert target.element.clickable


def test_selectable_is_described_from_pyguitest_directly_not_actions():
    # A GTK page tab measured live publishes no Action interface entries at
    # all -- actions=() -- while pyguitest's own Element.selectable still
    # correctly reads True and .select() works, because GTK exposes the
    # Selection interface without naming it as an action. Reading `actions`
    # for this, the way clickable does, would have missed it on that
    # platform entirely.
    element = FakeElement("page tab", "Tree", pid=77, actions=(), selectable=True)
    target = element_resolver(element=element).resolve(130, 130)
    assert target.element.selectable is True


def test_an_expandable_elements_open_state_is_described():
    element = FakeElement("tree item", "Documents", pid=77, expanded=True)
    target = element_resolver(element=element).resolve(130, 130)
    assert target.element.expanded is True
    assert target.element.expandable


def test_an_unexpandable_elements_state_is_described_as_none():
    element = FakeElement("push button", "Save", pid=77)
    target = element_resolver(element=element).resolve(130, 130)
    assert target.element.expanded is None
    assert not target.element.expandable


def test_the_ancestry_walks_the_elements_own_parents():
    target = element_resolver().resolve(130, 130)
    assert target.element.path == (("frame", "Target"),)


def test_a_pyguitest_without_element_geometry_degrades_to_coordinates():
    # The capability is the version check too: an older pyguitest declares
    # none, so this must degrade rather than call a method it lacks.
    made = element_resolver(capabilities=("WINDOW_LIST",))
    assert made.resolve(130, 130).element is None
    assert any("ELEMENT_GEOMETRY" in warning for warning in made.warnings)


def test_a_dead_accessible_tree_is_noticed_before_it_is_believed():
    # A reachable bus whose registry is dead answers every question emptily
    # rather than failing, so element resolution would switch on against a
    # tree that can never name anything.
    made = element_resolver(tree_error=RuntimeError("registry is not running"))
    assert made.resolve(130, 130).element is None
    assert any("accessible tree" in warning for warning in made.warnings)


def test_chromium_invisibility_is_noted_when_measured_off(monkeypatch):
    # Chromium/Electron build no accessible tree at all until something
    # announces an AT is running -- element resolution otherwise works, so
    # without this note a click on VS Code or Slack finding no element
    # would read as a resolver bug rather than a known, diagnosable gap.
    monkeypatch.setattr("pyguitest.session.assistive_technology_enabled", lambda: False)
    made = element_resolver()
    assert made.resolves_elements
    assert any("org.a11y.Status.IsEnabled" in warning for warning in made.warnings)


def test_no_chromium_note_when_measured_on(monkeypatch):
    monkeypatch.setattr("pyguitest.session.assistive_technology_enabled", lambda: True)
    made = element_resolver()
    assert not any("IsEnabled" in warning for warning in made.warnings)


def test_no_chromium_note_when_it_cannot_be_measured(monkeypatch):
    # None means the question could not be asked (no gdbus, no bus) -- not
    # a reason to warn about a desktop-specific gap that may not apply.
    monkeypatch.setattr("pyguitest.session.assistive_technology_enabled", lambda: None)
    made = element_resolver()
    assert not any("IsEnabled" in warning for warning in made.warnings)


def test_no_at_bridge_is_noted_even_though_the_tree_answers(monkeypatch):
    """The probe cannot see this one, which is exactly why it needs saying.

    `NO_AT_BRIDGE` stops GTK3 and Qt registering at startup, so an
    application launched from such an environment publishes nothing -- while
    the desktop's own components, which registered long before the variable
    was ever set, keep the tree populated and the probe passing. Found live
    on a MATE session where `--doctor` answered "ready to record, and clicks
    will be named" and a mate-calc launched from that same environment never
    appeared in the tree at all.
    """
    monkeypatch.setenv("NO_AT_BRIDGE", "1")
    made = element_resolver()
    assert made.resolves_elements
    assert made.bridge_disabled
    assert any("NO_AT_BRIDGE" in warning for warning in made.warnings)


def test_no_bridge_note_when_the_variable_is_unset(monkeypatch):
    monkeypatch.delenv("NO_AT_BRIDGE", raising=False)
    made = element_resolver()
    assert not made.bridge_disabled
    assert not any("NO_AT_BRIDGE" in warning for warning in made.warnings)


def test_no_bridge_note_when_the_variable_is_switched_off(monkeypatch):
    # "0" is how a shell turns it back off again, and reads as unset here --
    # the toolkits treat it that way too.
    monkeypatch.setenv("NO_AT_BRIDGE", "0")
    made = element_resolver()
    assert not made.bridge_disabled
    assert not any("NO_AT_BRIDGE" in warning for warning in made.warnings)


def test_no_session_leaves_element_resolution_off_and_says_so():
    made = DesktopResolver(session=None, elements=True)
    assert made.resolve(130, 130).element is None
    assert any("no pyguitest session" in warning for warning in made.warnings)


def test_a_lookup_that_raises_mid_recording_falls_back_to_the_coordinate():
    made = element_resolver(fail=["element_at"])
    target = made.resolve(130, 130)
    assert target.element is None
    assert target.window is not None


def test_an_unreadable_rectangle_leaves_the_element_without_one():
    # The element is still worth naming; only the corroboration checks that
    # need a rectangle are skipped.
    target = element_resolver(fail=["extents"]).resolve(130, 130)
    assert target.element is not None
    assert target.element.extents is None


def test_a_hit_that_bottoms_out_at_the_window_is_not_a_widget():
    # What a toolkit reporting widgets in window coordinates looks like from
    # outside: the frame's own rectangle checks out, every widget inside it
    # claims points elsewhere, so the walk stops at the frame. Clicking that
    # is never the click that was recorded.
    frame = FakeElement("frame", "Recorder Check", pid=77)
    made = element_resolver(element=frame, extents=(100, 50, 310, 263))
    target = made.resolve(130, 130)
    assert target.element is None
    assert target.window is not None
    assert any("bottomed out at the window" in w for w in made.warnings)


# -- inspecting for a check --------------------------------------------------


def test_null_resolver_inspects_to_a_bare_point():
    observed = NullResolver().inspect(5, 6)
    assert (observed.target.x, observed.target.y) == (5, 6)
    assert observed.text is None and observed.checked is None


def test_inspect_reads_the_state_off_the_live_element():
    element = FakeElement(
        "entry", "Filename", pid=77, text="report.txt", checkable=False
    )
    observed = element_resolver(element=element).inspect(130, 130)
    assert observed.target.element.name == "Filename"
    assert observed.text == "report.txt"


def test_inspect_reads_a_checkbox_state():
    element = FakeElement(
        "check box", "Read only", pid=77, checked=True, checkable=True
    )
    observed = element_resolver(element=element).inspect(130, 130)
    assert observed.checked is True and observed.checkable is True


def test_a_tree_that_changed_between_the_two_reads_drops_the_state():
    # The element is named once and read again; a menu closing under the
    # pointer between the two is enough. A reading that cannot be attributed
    # to the element the check named is worse than no reading, because the
    # generated check would then assert some other widget's text.
    made = element_resolver()
    swapped = FakeElement("label", "Something else", pid=77, text="not the same widget")
    named = made.session.element

    answers = [named, swapped]

    def element_at(x, y):
        return answers.pop(0) if answers else swapped

    made.session.element_at = element_at
    observed = made.inspect(130, 130)
    assert observed.text is None
    assert any("changed between" in warning for warning in made.warnings)


def test_a_state_read_that_raises_leaves_the_check_at_showing():
    made = element_resolver()
    made.session.fail.add("element_at")
    observed = made.inspect(130, 130)
    assert observed.text is None and observed.checked is None


# -- keyboard focus ----------------------------------------------------------


def focus_resolver(focused, windows=None, **kwargs):
    """A resolver whose session reports `focused` and lists `windows`."""
    made = element_resolver(**kwargs)
    made.session.focused = lambda: focused
    made.session.windows = lambda: (
        windows if windows is not None else [made.session.window]
    )
    return made


def test_null_resolver_knows_nothing_about_focus():
    assert NullResolver().focused() is None


def test_focus_names_the_focused_field():
    element = FakeElement("entry", "Search", pid=77)
    target = focus_resolver(element).focused()
    assert target is not None
    assert target.element.name == "Search"
    assert target.window.title == "Target"


def test_a_toplevel_holding_focus_is_read_as_no_answer():
    # GNOME Shell carries FOCUSED on its own window for the whole desktop, so
    # a window role means "this desktop does not publish per-widget focus"
    # rather than "the frame is what you are typing into".
    for role in ("frame", "window", "dialog"):
        assert focus_resolver(FakeElement(role, "Shell", pid=77)).focused() is None


def test_focus_in_a_process_owning_no_window_here_is_refused():
    # The leak that matters most: the accessibility bus is scoped to the login
    # session, `focused()` searches the whole desktop, and unlike a click
    # there is no coordinate to corroborate the answer against. Typing would
    # otherwise be attributed to a widget in the developer's own editor.
    made = focus_resolver(FakeElement("entry", "Elsewhere", pid=4242))
    assert made.focused() is None
    # Worded per platform; `test_platforms.py` is where each wording is pinned.
    assert any("ignored keyboard focus" in warning for warning in made.warnings)
    assert any(foreign_focus_reason() in warning for warning in made.warnings)


def test_focus_on_an_element_with_no_pid_is_refused():
    # Nothing else can tie it to the recorded display.
    made = focus_resolver(FakeElement("entry", "Search", pid=None))
    assert made.focused() is None
    assert any("no process id" in warning for warning in made.warnings)


def test_no_focus_at_all_is_not_a_failure():
    assert focus_resolver(None).focused() is None


def test_a_session_that_raises_on_focus_degrades_quietly():
    made = element_resolver()

    def boom():
        raise RuntimeError("the tree went stale")

    made.session.focused = boom
    assert made.focused() is None


class _ElementReapedBetweenFocusAndRole:
    """An element that `session.focused()` could still return, but is gone.

    `.role` raises the way dogtail does for an accessible the bus has
    already dropped (a real GLib.GError there, stood in for here so this
    test needs no gi dependency).
    """

    @property
    def role(self):
        raise RuntimeError("atspi_error: No such object path '/org/a11y/x' (1)")


def test_an_element_reaped_between_focus_and_its_role_degrades_quietly():
    # `session.focused()` and `.role` are two separate live bus reads, not
    # one atomic snapshot -- a menu closing (Escape, most often) between them
    # is enough to make the second one ask about an accessible already gone.
    # Seen live: recording stopped with Escape, Escape and the whole process
    # crashed on this exact GError, losing the recording -- not caught by
    # the try/except around `session.focused()` alone, since that call had
    # already returned successfully.
    made = element_resolver()
    made.session.focused = lambda: _ElementReapedBetweenFocusAndRole()
    assert made.focused() is None


def test_focus_is_not_asked_when_element_resolution_is_off():
    made = element_resolver(capabilities=("WINDOW_LIST",))
    made.session.focused = lambda: FakeElement("entry", "Search", pid=77)
    assert made.focused() is None


def test_focus_picks_the_window_the_element_actually_descends_from():
    # A process commonly owns more than one window, and taking the first is
    # how a recording announces a window nothing was ever done in. Seen live:
    # zenity owns both its dialog and a window called "zenity", and typing
    # into the dialog generated wait_for_window("zenity").
    entry = FakeElement("entry", "Name", pid=77)
    entry.parent = FakeElement("dialog", "Recorder Check", pid=77)
    made = focus_resolver(
        entry,
        windows=[
            FakeWindow(title="zenity", pid=77),
            FakeWindow(title="Recorder Check", pid=77),
        ],
    )
    target = made.focused()
    assert target.window.title == "Recorder Check"


def test_focus_falls_back_to_the_first_window_when_ancestry_says_nothing():
    entry = FakeElement("entry", "Name", pid=77)
    made = focus_resolver(
        entry,
        windows=[FakeWindow(title="Only", pid=77), FakeWindow(title="Other", pid=77)],
    )
    assert made.focused().window.title == "Only"


# -- a title that drifts while the recording is being made -------------------


class Handled(FakeWindow):
    """A window whose identity is its handle, as pyguitest's own Window is."""

    def __init__(self, handle, **kwargs):
        super().__init__(**kwargs)
        self.handle = handle

    def __eq__(self, other):
        return isinstance(other, Handled) and self.handle == other.handle

    def __hash__(self):
        return hash(self.handle)


def test_a_drifting_title_stays_one_window():
    # The bug the first recording of a real application found: a text editor
    # renamed itself on every keystroke, each title looked like a new window,
    # and the script waited for four windows that were always one.
    session = FakeSession(window=Handled(1, title="New Document - Editor"))
    made = resolver(session)
    first = made.resolve(1, 2)
    session.window = Handled(1, title="Hello - Editor")
    second = made.resolve(1, 2)
    session.window = Handled(1, title="Hello There - Editor")
    third = made.resolve(1, 2)

    titles = {t.window.title for t in (first, second, third)}
    assert titles == {"New Document - Editor"}
    assert third.window.title_stable is False
    assert any("renamed itself" in w for w in made.warnings)
    # One note per window, not one per title: an editor retitling on
    # every keystroke put eight near-identical notes in one header.
    assert sum("renamed itself" in w for w in made.warnings) == 1


def test_the_first_title_is_the_one_kept():
    # Replay starts from the same state and follows the same sequence, so the
    # title the window had when the recording first touched it is the one the
    # script will find.
    session = FakeSession(window=Handled(7, title="Untitled"))
    made = resolver(session)
    made.resolve(1, 2)
    session.window = Handled(7, title="Report.odt")
    assert made.resolve(1, 2).window.title == "Untitled"


def test_two_different_windows_keep_their_own_titles():
    session = FakeSession(window=Handled(1, title="First"))
    made = resolver(session)
    assert made.resolve(1, 2).window.title == "First"
    session.window = Handled(2, title="Second")
    assert made.resolve(1, 2).window.title == "Second"


def test_a_steady_title_is_still_stable():
    session = FakeSession(window=Handled(1, title="Steady"))
    made = resolver(session)
    made.resolve(1, 2)
    assert made.resolve(3, 4).window.title_stable is True


def test_an_app_id_seen_later_is_adopted():
    # Some backends fill app_id in only once the window is fully mapped.
    session = FakeSession(window=Handled(1, title="App", app_id=""))
    made = resolver(session)
    made.resolve(1, 2)
    session.window = Handled(1, title="App", app_id="org.example.App")
    assert made.resolve(1, 2).window.app_id == "org.example.App"


class TestPlatformWordingInNotes:
    """The notes a recording carries must describe the platform it was made on.

    These strings end up in the generated script's own footer, where a Windows
    reader told about "the recorded display" and "one X display" is being sent
    after machinery their desktop does not have.
    """

    def test_windows_notes_name_no_display(self, monkeypatch):
        import pyguitest_recorder.platforms as platforms
        from pyguitest_recorder.windows import resolver as resolver_module

        monkeypatch.setattr(platforms.sys, "platform", "win32")
        phrase = resolver_module._scope_phrase()
        assert "display" not in phrase
        assert phrase == "in this recording"

    def test_other_platforms_keep_the_recorded_display(self, monkeypatch):
        import pyguitest_recorder.platforms as platforms
        from pyguitest_recorder.windows import resolver as resolver_module

        monkeypatch.setattr(platforms.sys, "platform", "linux")
        assert resolver_module._scope_phrase() == "on the recorded display"

    def test_the_recording_can_overrule_the_host(self, monkeypatch):
        # Backend = "xrecord" on a Windows machine records X clients, so the
        # recording is of a display even though the process is a Windows one
        # -- see DesktopResolver.windows.
        import pyguitest_recorder.platforms as platforms
        from pyguitest_recorder.windows import resolver as resolver_module

        monkeypatch.setattr(platforms.sys, "platform", "win32")
        assert resolver_module._scope_phrase(False) == "on the recorded display"


class TestTheUwpProcessSplit:
    """A Store app's window and its widgets are owned by different processes.

    Windows hosts every UWP toplevel in an `ApplicationFrameWindow` belonging
    to `ApplicationFrameHost.exe`, while the widgets inside belong to the
    application -- which pyguitest documents on `WINDOW_PID`. Treating that
    mismatch as evidence of another session, which is what it means on Linux,
    rejected every widget in the app.

    Measured on a real Calculator recording: window pid 8824 (the host),
    buttons pid 16672, and the only element that survived was `Close
    Calculator` on the frame's own title bar, which the host does own. The
    frame *class* is what a recording can check that against, since
    `Window.app_id` carries the window class on this platform.

    The other half of the same rule is that a mismatch with nothing behind it
    is refused: `_fits` answers True wherever it cannot tell, and on a scaled
    screen -- the ordinary case on Windows -- that is every element from every
    other process on the machine.
    """

    def test_a_uwp_widget_is_kept_on_windows(self, monkeypatch):
        import pyguitest_recorder.platforms as platforms

        monkeypatch.setattr(platforms.sys, "platform", "win32")
        target = leak_resolver(
            element_pid=16672, window_pid=8824, window_app_id="ApplicationFrameWindow"
        ).resolve(100, 100)
        assert target.element is not None
        assert target.element.name == "Save"

    def test_the_frame_class_is_matched_without_regard_to_case(self, monkeypatch):
        # Window classes are compared case-insensitively, and this string is
        # whatever the backend quoted out of `GetClassNameW`.
        import pyguitest_recorder.platforms as platforms

        monkeypatch.setattr(platforms.sys, "platform", "win32")
        target = leak_resolver(
            element_pid=16672, window_pid=8824, window_app_id="applicationframewindow"
        ).resolve(100, 100)
        assert target.element is not None

    def test_an_unexplained_mismatch_is_refused_on_windows(self, monkeypatch):
        # No frame host, and no rectangle on either side for the geometry to
        # corroborate it with: this used to be kept as soon as the geometry
        # stopped talking.
        import pyguitest_recorder.platforms as platforms

        monkeypatch.setattr(platforms.sys, "platform", "win32")
        made = leak_resolver(element_pid=16672, window_pid=8824)
        assert made.resolve(100, 100).element is None
        assert any("ApplicationFrameWindow" in w for w in made.warnings)

    def test_a_scaled_screen_no_longer_excuses_an_unexplained_mismatch(
        self, monkeypatch
    ):
        # 125%/150% displays are the ordinary case on Windows, and there the
        # units are not agreed well enough to compare at all -- which is
        # exactly why "cannot tell" stopped being enough for a pid that
        # disagrees.
        import pyguitest_recorder.platforms as platforms

        monkeypatch.setattr(platforms.sys, "platform", "win32")
        made = leak_resolver(element_pid=16672, window_pid=8824)
        made._scaled = True
        made._element = lambda x, y: ElementRef(
            role="push button", name="Save", pid=16672, extents=(95, 95, 10, 10)
        )
        assert made.resolve(100, 100).element is None

    def test_geometry_that_corroborates_keeps_the_mismatch(self, monkeypatch):
        # The other side of the same rule: an element whose own rectangle is
        # inside the window has corroborated itself, which is the evidence
        # this already accepted wherever a pid was missing outright.
        import pyguitest_recorder.platforms as platforms

        monkeypatch.setattr(platforms.sys, "platform", "win32")
        made = leak_resolver(element_pid=16672, window_pid=8824)
        made._element = lambda x, y: ElementRef(
            role="push button", name="Save", pid=16672, extents=(95, 95, 10, 10)
        )
        assert made.resolve(100, 100).element is not None

    def test_an_x11_recording_on_a_windows_host_still_refuses_the_mismatch(
        self, monkeypatch
    ):
        # `backend = "xrecord"` on a Windows machine records X clients through
        # Xming/VcXsrv/WSLg: the host answers Windows, the recording is X11's,
        # and `Recorder` hands the resolver that answer. A frame class means
        # nothing to a desktop with no `ApplicationFrameHost` on it.
        import pyguitest_recorder.platforms as platforms

        monkeypatch.setattr(platforms.sys, "platform", "win32")
        made = leak_resolver(
            element_pid=16672,
            window_pid=8824,
            window_app_id="ApplicationFrameWindow",
            windows=False,
        )
        assert made.resolve(100, 100).element is None

    def test_the_same_mismatch_is_still_refused_off_windows(self, monkeypatch):
        # On Linux it means exactly what it always meant: the accessibility
        # bus answering about another login session.
        import pyguitest_recorder.platforms as platforms

        monkeypatch.setattr(platforms.sys, "platform", "linux")
        made = leak_resolver(element_pid=16672, window_pid=8824)
        assert made.resolve(100, 100).element is None
        assert any("pid 16672" in warning for warning in made.warnings)

    def test_windows_still_rejects_an_element_that_cannot_fit(self, monkeypatch):
        # The pid stops being evidence there; geometry takes over, and an
        # element bigger than the window it is supposedly inside is still
        # describing a different screen.
        import pyguitest_recorder.platforms as platforms

        monkeypatch.setattr(platforms.sys, "platform", "win32")
        session = FakeSession(window=FakeWindow(title="Target", pid=8824))
        made = DesktopResolver(session=session, elements=False)
        made._resolves_elements = True
        made._element = lambda x, y: ElementRef(
            role="push button", name="Save", pid=16672, extents=(0, 0, 9999, 9999)
        )
        assert made.resolve(100, 100).element is None


class TestProcessAncestry:
    """Finding the terminal the recorder is being driven from.

    `ps` does not exist on Windows, so the Unix reader raised
    FileNotFoundError, was swallowed, and left the ancestry empty -- silently
    turning off the terminal exclusion it exists for. A real Windows recording
    then waited for a window titled after the recording command itself, a
    title that only exists while recording and can never match on replay.
    """

    def test_the_chain_is_walked_nearest_first(self):
        from pyguitest_recorder.windows.resolver import _walk_parents

        me = os.getpid()
        chain = _walk_parents({me: 100, 100: 200, 200: 300, 300: 0}, limit=8)
        assert chain == [100, 200, 300]

    def test_the_limit_is_honoured(self):
        from pyguitest_recorder.windows.resolver import _walk_parents

        me = os.getpid()
        parents = {me: 1000}
        parents.update({n: n + 1 for n in range(1000, 1020)})
        assert len(_walk_parents(parents, limit=3)) == 3

    def test_a_cycle_cannot_hang_the_walk(self):
        # A process table read while processes are exiting can hand back a
        # cycle, and a reused pid can point back down its own chain.
        from pyguitest_recorder.windows.resolver import _walk_parents

        me = os.getpid()
        chain = _walk_parents({me: 100, 100: 200, 200: 100}, limit=8)
        assert chain == [100, 200]

    def test_windows_does_not_shell_out_to_ps(self, monkeypatch):
        import pyguitest_recorder.platforms as platforms
        from pyguitest_recorder.windows import resolver as resolver_module

        monkeypatch.setattr(platforms.sys, "platform", "win32")

        def _explode(*args, **kwargs):
            raise AssertionError("ps was called on Windows")

        monkeypatch.setattr(resolver_module.subprocess, "run", _explode)
        monkeypatch.setattr(
            resolver_module, "_parent_pids_windows", lambda: {os.getpid(): 4242}
        )
        assert resolver_module._ancestor_pids() == [4242]
