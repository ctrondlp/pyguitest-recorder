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
    made._resolves_elements = True
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
    made._resolves_elements = True
    made._element = lambda x, y: ElementRef(role="panel", name="", pid=4242)
    target = made.resolve(100, 100)
    assert target.window is None
    assert target.element is None
    assert any("not scoped to" in warning for warning in made.warnings)


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


def test_an_element_is_described_from_what_the_session_says():
    target = element_resolver().resolve(130, 130)
    assert target.element.role == "push button"
    assert target.element.name == "Save"
    assert target.element.description == "Save the file"
    assert target.element.pid == 77
    assert target.element.extents == (120, 120, 60, 24)


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
    assert any("another session" in warning for warning in made.warnings)


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
