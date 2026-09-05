import os

from pyguitest_recorder.model import ElementRef
from pyguitest_recorder.windows import DesktopResolver, NullResolver


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


def resolver(session, **kwargs):
    return DesktopResolver(session=session, elements=False, **kwargs)


def test_null_resolver_returns_bare_coordinates():
    target = NullResolver().resolve(5, 6)
    assert (target.x, target.y) == (5, 6)
    assert target.window is None and target.element is None


def test_window_and_geometry_are_captured():
    target = resolver(FakeSession()).resolve(420, 315)
    assert target.window.app_id == "org.example.App"
    assert target.window.geometry == (100, 50, 800, 600)
    assert target.relative == (320, 265)


def test_hit_test_failure_falls_back_to_the_active_window():
    target = resolver(FakeSession(fail={"window_at"})).resolve(1, 2)
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


def test_no_session_means_no_window_context():
    target = resolver(None).resolve(7, 8)
    assert target.window is None


def test_the_recorders_own_window_is_never_the_target():
    session = FakeSession(window=FakeWindow(pid=os.getpid()))
    assert resolver(session).resolve(1, 2).window is None


def test_extra_ignored_pids_are_honoured():
    session = FakeSession(window=FakeWindow(pid=4321))
    assert resolver(session, ignore_pids={4321}).resolve(1, 2).window is None


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


# -- session leakage ---------------------------------------------------------


def leak_resolver(element_pid, window_pid):
    """A resolver whose window and element deliberately may not agree."""
    session = FakeSession(window=FakeWindow(title="Target", pid=window_pid))
    made = DesktopResolver(session=session, elements=False)
    made._atspi = object()
    made._element = lambda x, y: ElementRef(
        role="push button", name="Save", pid=element_pid
    )
    return made


def test_an_element_from_another_process_is_refused():
    # The accessibility bus is session-scoped, not display-scoped, so a
    # recorder capturing a private X server is answered about applications on
    # every other one -- and both were asked about the same coordinate, so the
    # wrong answer is indistinguishable from the right one.
    made = leak_resolver(element_pid=4242, window_pid=77)
    target = made.resolve(100, 100)
    assert target.element is None
    assert target.window is not None
    assert any("pid 4242" in warning for warning in made.warnings)


def test_the_mismatch_is_warned_about_once_not_per_event():
    made = leak_resolver(element_pid=4242, window_pid=77)
    for _ in range(5):
        made.resolve(100, 100)
    assert len(made.warnings) == 1


def test_an_element_from_the_same_process_is_kept():
    target = leak_resolver(element_pid=77, window_pid=77).resolve(100, 100)
    assert target.element is not None
    assert target.element.name == "Save"


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
    made._atspi = object()
    made._element = lambda x, y: ElementRef(role="panel", name="", pid=4242)
    target = made.resolve(100, 100)
    assert target.window is None
    assert target.element is None
    assert any("not scoped to" in warning for warning in made.warnings)


def test_without_window_context_at_all_the_element_is_kept():
    # Nothing to verify against, and one display is the normal case.
    made = DesktopResolver(session=None, elements=False)
    made._atspi = object()
    made._element = lambda x, y: ElementRef(role="push button", name="Save")
    assert made.resolve(100, 100).element is not None


def fitting_resolver(extents, geometry=(0, 0, 310, 263), scale=1.0):
    """A resolver whose window and element rectangles may not be compatible."""
    session = FakeSession(
        window=FakeWindow(title="Target", pid=None), geometry=geometry
    )
    session.screens = lambda: [type("S", (), {"scale": scale})()]
    made = DesktopResolver(session=session, elements=False)
    made._atspi = object()
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
    made._atspi = object()
    made._element = lambda x, y: ElementRef(
        role="panel", name="", pid=77, extents=(0, 0, 9999, 9999)
    )
    assert made.resolve(100, 100).element is not None


def hit_resolver(extents, scale=1.0):
    """A resolver whose AT-SPI hit-testing may be answering nonsense."""
    session = FakeSession(window=FakeWindow(title="Target", pid=77))
    session.screens = lambda: [type("S", (), {"scale": scale})()]
    made = DesktopResolver(session=session, elements=False)
    made._atspi = object()
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
