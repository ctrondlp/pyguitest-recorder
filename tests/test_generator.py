import ast
import re

from pyguitest_recorder.generator import GeneratorOptions, generate, validate
from pyguitest_recorder.model import (
    Assertion,
    Click,
    Drag,
    ElementRef,
    HotKey,
    KeyStroke,
    Pause,
    Recording,
    Scroll,
    Sync,
    Target,
    TextInput,
    WaitForElement,
    WaitForIdle,
    WindowActivate,
    WindowRef,
)


def window_pattern(source):
    """The regex the generated script will actually search window titles with."""
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "wait_for_window"
        ):
            return node.args[0].value
    raise AssertionError("no wait_for_window call in the generated source")


def render(*events, **options):
    recording = Recording(events=list(events))
    return generate(recording, GeneratorOptions(include_header=False, **options))


def test_generated_source_is_valid_and_uses_the_real_api(window, save_button):
    source = render(
        WindowActivate(window=window),
        Click(target=Target(x=180, y=90, window=window, element=save_button)),
        TextInput(text="hello"),
    )
    assert validate(source) == []
    compile(source, "<test>", "exec")


def test_named_element_beats_a_coordinate(window, save_button):
    source = render(
        Click(target=Target(x=180, y=90, window=window, element=save_button))
    )
    assert 'gui.button("Save").click()' in source
    assert "move_mouse" not in source


def test_unsugared_role_uses_the_element_form(window):
    item = ElementRef(role="list item", name="Inbox")
    source = render(Click(target=Target(x=1, y=2, window=window, element=item)))
    assert 'gui.element(role=Role.LIST_ITEM, name="Inbox").click()' in source
    assert "from pyguitest import Capability, Role" in source


def test_unnamed_element_falls_back_to_coordinates(window):
    anonymous = ElementRef(role="panel")
    source = render(
        Click(target=Target(x=420, y=315, window=window, element=anonymous))
    )
    assert "gui.geometry(app)" in source
    assert "gui.move_mouse(app_x + 320, app_y + 265)" in source


def test_no_window_geometry_falls_back_to_absolute():
    bare = WindowRef(title="Plain")
    source = render(Click(target=Target(x=42, y=99, window=bare)))
    assert "gui.move_mouse(42, 99)" in source


def test_absolute_mode_ignores_elements_and_geometry(window, save_button):
    source = render(
        Click(target=Target(x=180, y=90, window=window, element=save_button)),
        locators="absolute",
    )
    assert "gui.move_mouse(180, 90)" in source
    assert "gui.button" not in source


def test_capability_preamble_names_what_the_script_uses(window, save_button):
    source = render(
        Click(target=Target(x=1, y=1, window=window, element=save_button)),
        TextInput(text="x"),
    )
    assert "Capability.ELEMENT_ACTION" in source
    assert "Capability.TEXT_ENTRY" in source
    assert "Capability.POINTER_BUTTON" not in source


def test_preamble_can_be_turned_off(window):
    source = render(
        Click(target=Target(x=1, y=1, window=window)), capability_preamble=False
    )
    assert "gui.require" not in source


def test_double_click_at_a_coordinate_emits_double_click(window):
    source = render(Click(target=Target(x=5, y=5, window=window), count=2))
    assert "gui.double_click()" in source
    assert "gui.click()" not in source


def test_triple_click_emits_three_clicks_with_an_explanation(window):
    source = render(Click(target=Target(x=5, y=5, window=window), count=3))
    assert source.count("gui.click()") == 3
    assert "no primitive past double_click" in source


def test_double_click_on_a_named_element_still_degrades(window, save_button):
    source = render(
        Click(target=Target(x=5, y=5, window=window, element=save_button), count=2)
    )
    assert source.count(".click()") == 2
    assert "Element has no double_click" in source


def test_text_into_a_named_field_sets_it_directly():
    field = ElementRef(role="entry", name="Name")
    source = render(TextInput(text="Ada", target=Target(x=1, y=1, element=field)))
    assert 'gui.text_field("Name").set_text("Ada")' in source


def test_sensitive_text_never_appears_as_a_literal():
    source = render(TextInput(text="hunter2", sensitive=True))
    assert "hunter2" not in source
    assert 'SECRET_1 = os.environ["SECRET_1"]' in source
    assert "gui.type_text(SECRET_1)" in source


def test_redaction_can_be_disabled():
    source = render(TextInput(text="hunter2", sensitive=True), redact_sensitive=False)
    assert 'gui.type_text("hunter2")' in source


def test_quotes_and_newlines_survive_the_literal():
    source = render(TextInput(text='say "hi"\n'))
    assert validate(source) == []
    namespace = {}
    exec(compile(source, "<test>", "exec"), namespace)  # noqa: S102


def test_hotkey_uses_send_keys_grammar():
    assert 'gui.send_keys("^(s)")' in render(HotKey(keys=("ctrl", "s")))
    assert 'gui.send_keys("^(+(l))")' in render(HotKey(keys=("ctrl", "shift", "l")))


def test_named_key_taps():
    assert 'gui.tap_key("Return")' in render(KeyStroke(key="Return"))


def test_scroll_and_sync_and_pause():
    source = render(Scroll(target=Target(x=1, y=1), dy=3), Sync(), Pause(seconds=1.25))
    assert "gui.scroll(dy=3)" in source
    assert "gui.sync()" in source
    assert "gui.wait(1.25)" in source


def test_drag_uses_the_drag_primitive(window):
    source = render(
        Drag(
            start=Target(x=10, y=20, window=window),
            end=Target(x=30, y=40, window=window),
        )
    )
    assert "gui.drag(" in source


def test_wait_for_element_renders_a_role_constant():
    source = render(WaitForElement(element=ElementRef(role="dialog", name="Save As")))
    assert 'gui.wait_for_element(role="dialog", name="Save As", timeout=10)' in source


def test_a_drifting_title_is_dropped_for_the_app_id():
    drifting = WindowRef(
        title="Untitled - Editor", app_id="org.x.E", title_stable=False
    )
    source = render(WindowActivate(window=drifting))
    # The title is not matched on at all -- the whole point of noticing it
    # drifted -- and the helper that looks a window up by app id comes along.
    assert 'gui.wait_for_window("Untitled' not in source
    assert '_window_by_app_id(gui, "org.x.E"' in source
    assert "title drifted while recording" in source
    assert validate(source) == []


def test_a_drifting_title_with_no_app_id_is_used_anyway_with_a_warning():
    drifting = WindowRef(title="Untitled - Editor", title_stable=False)
    source = render(WindowActivate(window=drifting))
    assert "gui.wait_for_window(" in source
    assert "this match is fragile" in source


def test_window_variable_is_named_from_the_app_id_tail():
    window = WindowRef(title="T", app_id="org.gnome.TextEditor")
    source = render(WindowActivate(window=window))
    assert "texteditor = gui.wait_for_window" in source


def test_validate_rejects_a_call_pyguitest_does_not_have():
    problems = validate("def f(gui):\n    gui.telepathy()\n")
    assert problems == ["pyguitest.Session has no method 'telepathy'"]


def test_validate_rejects_a_capability_and_a_role_pyguitest_does_not_have():
    source = "def f(gui, Capability, Role):\n    gui.require(Capability.TELEPATHY)\n"
    assert validate(source) == ["pyguitest has no Capability 'TELEPATHY'"]
    source = "def f(gui, Role):\n    gui.element(role=Role.HOLOGRAM)\n"
    assert validate(source) == ["pyguitest has no Role 'HOLOGRAM'"]


def test_validate_rejects_a_name_nothing_binds():
    # The failure this exists for: `wait_for_idle` used to read a pid off an
    # `app` variable no generated line ever defined. It compiles; it raises
    # NameError the moment it runs.
    problems = validate("def f(gui):\n    gui.wait_for_idle(app.pid)\n")
    assert problems == ["line 2: 'app' is used but never assigned"]


def test_validate_reports_a_syntax_error():
    assert validate("def (:\n")[0].startswith("syntax error")


def test_empty_recording_still_generates_a_valid_module():
    source = generate(Recording(), GeneratorOptions(include_header=False))
    assert validate(source) == []
    assert "pass" in source


def test_header_records_the_xwayland_caveat():
    from pyguitest_recorder.model import Environment

    recording = Recording(
        environment=Environment(xwayland=True, session_type="xwayland")
    )
    source = generate(recording)
    assert "XWayland" in source


# -- regressions -------------------------------------------------------------
# Every test below stands for a bug that shipped in the first cut of this
# generator and produced a script that compiled, validated and misbehaved.


def test_a_right_click_on_a_named_element_stays_a_right_click(window, save_button):
    # Element.click() takes no button, so routing a recorded right click
    # through it turned a context menu into an ordinary activation.
    source = render(
        Click(target=Target(x=180, y=90, window=window, element=save_button), button=3)
    )
    assert "gui.click(3)" in source
    assert 'gui.button("Save").click()' not in source
    assert "Element.click() takes no button" in source


def test_a_left_click_still_prefers_the_element(window, save_button):
    source = render(
        Click(target=Target(x=180, y=90, window=window, element=save_button))
    )
    assert 'gui.button("Save").click()' in source


def test_a_scroll_puts_the_pointer_back_where_it_was_recorded(window):
    # The wheel acts on whatever is under the pointer, so a scroll emitted
    # without a move scrolled whichever widget the previous action left it on.
    source = render(Scroll(target=Target(x=420, y=315, window=window), dy=3))
    body = source.split("gui.scroll")[0]
    assert "gui.move_mouse(app_x + 320, app_y + 265)" in body
    assert "gui.scroll(dy=3)" in source


def test_a_scroll_at_the_same_point_does_not_move_twice(window):
    target = Target(x=420, y=315, window=window)
    source = render(Click(target=target), Scroll(target=target, dy=1))
    assert source.count("gui.move_mouse") == 1


def test_a_scroll_of_nothing_emits_nothing(window):
    source = render(Scroll(target=Target(x=1, y=2, window=window)))
    assert "gui.scroll" not in source


def test_hotkeys_name_keys_the_way_send_keys_resolves_them():
    # `{RET}` is not the abbreviation for Return -- `ENT` is -- so truncating
    # a keysym to three letters produced a key send_keys cannot resolve. The
    # full name works, because an unabbreviated name is passed to press_key.
    source = render(
        HotKey(keys=("ctrl", "Return")),
        HotKey(keys=("ctrl", "Down")),
        HotKey(keys=("alt", "Prior")),
    )
    assert 'gui.send_keys("^({Return})")' in source
    assert 'gui.send_keys("^({Down})")' in source
    assert 'gui.send_keys("%({Prior})")' in source


def test_a_hotkey_on_a_grammar_character_is_escaped():
    source = render(HotKey(keys=("ctrl", "+")), HotKey(keys=("ctrl", "(")))
    assert 'gui.send_keys("^({+})")' in source
    assert 'gui.send_keys("^({(})")' in source


def test_a_hotkey_requires_key_event_not_text_entry():
    # send_keys declares no capability of its own and reaches the keyboard
    # through press_key; requiring TEXT_ENTRY let require() pass on a backend
    # that cannot press a modifier.
    source = render(HotKey(keys=("ctrl", "s")))
    assert "Capability.KEY_EVENT" in source
    assert "Capability.TEXT_ENTRY" not in source


def test_wait_for_idle_takes_its_pid_from_the_window(window):
    # It used to read `app.pid` off a variable no generated line ever bound,
    # which compiles and then raises NameError on the first run.
    source = render(WaitForIdle(window=window, pid=99, timeout=30))
    assert "gui.wait_for_idle(app.pid, timeout=30)" in source
    assert "app = gui.wait_for_window(" in source
    assert "Capability.WINDOW_PID" in source
    assert validate(source) == []


def test_wait_for_idle_with_no_window_degrades_to_a_sleep():
    source = render(WaitForIdle(pid=99))
    assert "gui.wait(" in source
    assert validate(source) == []


def test_a_window_title_is_matched_as_text_not_as_a_pattern():
    # wait_for_window takes a regex. "Document (1)" unescaped is a pattern
    # that matches a different string, and an unbalanced bracket does not
    # compile at all.
    title = "Doc (1) [draft]+"
    source = render(
        WindowActivate(window=WindowRef(title=title, geometry=(0, 0, 9, 9)))
    )
    pattern = window_pattern(source)
    assert re.search(pattern, title)
    # ...and the pattern is not one that would also match a different window.
    assert not re.search(pattern, "Doc 1 draft")
    assert validate(source) == []


def test_a_title_pattern_keeps_its_spaces_readable():
    # re.escape backslashes spaces too, which changes nothing about what the
    # pattern matches and makes every generated window lookup unreadable.
    source = render(WindowActivate(window=WindowRef(title="Text Editor")))
    assert window_pattern(source) == "Text Editor"


def test_a_window_named_like_the_session_does_not_shadow_it():
    # `gui = gui.wait_for_window(...)` compiles, validates, and then fails on
    # the next line with an AttributeError about a Window.
    source = render(
        Click(
            target=Target(
                x=5, y=5, window=WindowRef(title="gui", geometry=(0, 0, 9, 9))
            )
        )
    )
    assert "gui = gui.wait_for_window" not in source
    assert validate(source) == []


def test_a_window_named_like_a_keyword_does_not_break_the_file():
    source = render(
        Click(
            target=Target(
                x=5, y=5, window=WindowRef(app_id="org.x.class", geometry=(0, 0, 9, 9))
            )
        )
    )
    assert validate(source) == []
    compile(source, "<test>", "exec")


def test_absolute_coordinates_are_honoured_for_typing_too(window):
    # The element path for text ignored the locator setting entirely, so
    # --absolute-coordinates still emitted set_text on a named field.
    field = ElementRef(role="entry", name="Name")
    source = render(
        TextInput(text="Ada", target=Target(x=1, y=2, window=window, element=field)),
        locators="absolute",
    )
    assert "gui.type_text" in source
    assert "text_field" not in source


def test_a_window_nothing_uses_is_still_waited_for_but_not_bound(window, save_button):
    source = render(
        Click(target=Target(x=180, y=90, window=window, element=save_button))
    )
    assert "gui.wait_for_window" not in source  # no coordinate needed it at all


def test_a_window_a_coordinate_needs_keeps_its_name(window):
    source = render(Click(target=Target(x=180, y=90, window=window)))
    assert "app = gui.wait_for_window" in source


def test_the_header_says_why_a_recording_degraded():
    # "Why is this script all coordinates?" is the first question a reader
    # asks, and the answer was only ever printed to the terminal that made it.
    from pyguitest_recorder.model import Environment

    recording = Recording(
        environment=Environment(
            notes=["element resolution off: no accessibility bus (no such name)"]
        )
    )
    source = generate(recording)
    assert "element resolution off" in source
    assert validate(source) == []


def test_the_profile_names_the_pyguitest_the_output_was_checked_against():
    # The header's profile is what tells the reader of an old generated
    # script which API it targeted. Letting it drift behind the pyguitest
    # `validate()` actually checked against makes it a false claim.
    import pyguitest

    from pyguitest_recorder.generator import PROFILE

    major_minor = ".".join(pyguitest.__version__.split(".")[:2])
    expected = f"pyguitest-{major_minor}"
    assert expected == PROFILE


def test_a_window_that_moved_has_its_origin_read_again():
    # Every offset is relative to where the window was when that event was
    # captured, so one geometry read shared across a move puts every later
    # coordinate out by however far it travelled. Seen live: a drag inside a
    # window dragged the window, origin (0, 0) -> (-160, 0) mid-recording.
    before = WindowRef(title="W", app_id="org.x.W", geometry=(0, 0, 300, 200))
    after = WindowRef(title="W", app_id="org.x.W", geometry=(-160, 0, 300, 200))
    source = render(
        Click(target=Target(x=50, y=60, window=before)),
        Click(target=Target(x=50, y=60, window=after)),
    )
    assert source.count("gui.geometry(w)") == 2
    assert "the window moved" in source
    # The offsets differ, because the same screen point is a different place
    # in a window that has moved.
    assert "gui.move_mouse(w_x + 50, w_y + 60)" in source
    assert "gui.move_mouse(w_x + 210, w_y + 60)" in source
    assert validate(source) == []


def test_a_window_that_stayed_put_is_read_once(window):
    source = render(
        Click(target=Target(x=180, y=90, window=window)),
        Click(target=Target(x=200, y=95, window=window)),
    )
    assert source.count("gui.geometry(app)") == 1


# -- checks ------------------------------------------------------------------


def check(kind, role=None, name="", expected=None, window=None, sensitive=False):
    element = ElementRef(role=role, name=name) if role else None
    return Assertion(
        check=kind,
        target=Target(x=10, y=20, window=window, element=element),
        expected=expected,
        sensitive=sensitive,
    )


def test_a_text_check_names_the_element_and_what_it_read():
    source = render(check("text", "label", "Status", expected="Saved"))
    assert 'expect_text(gui, role=Role.LABEL, name="Status", equals="Saved")' in source
    assert "# Check: 'Status' reads 'Saved'" in source
    assert validate(source) == []


def test_a_checked_check_renders_the_recorded_state():
    source = render(check("checked", "check box", "Read only", expected=True))
    assert (
        'expect_checked(gui, role=Role.CHECK_BOX, name="Read only", checked=True)'
        in source
    )
    assert validate(source) == []


def test_a_showing_check_is_the_floor_for_a_button():
    source = render(check("showing", "push button", "Undo"))
    assert 'expect_showing(gui, role=Role.PUSH_BUTTON, name="Undo")' in source
    assert validate(source) == []


def expect_window_pattern(source):
    """The regex the generated check will actually search titles with."""
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "expect_window"
        ):
            return node.args[1].value
    raise AssertionError("no expect_window call in the generated source")


def test_a_window_check_escapes_the_title_it_matches_on():
    # `expect_window` searches with a regex, the same trap the window lookups
    # had: "Document (1)" is otherwise a pattern matching "Document 1".
    window = WindowRef(title="Document (1)", app_id="org.example.App")
    source = render(check("window", window=window))
    pattern = expect_window_pattern(source)
    assert re.search(pattern, "Document (1)")
    assert not re.search(pattern, "Document 1")
    assert validate(source) == []


def test_the_helpers_a_check_calls_are_emitted_with_it():
    # `expect_text` calls `_expect_element`; emitting one without the other
    # is a NameError on the first failing check.
    source = render(check("text", "label", "Status", expected="Saved"))
    assert "def expect_text(" in source
    assert "def _expect_element(" in source
    assert "def expect_checked(" not in source


def test_a_check_fails_with_a_message_naming_the_element():
    # The whole reason these are helpers rather than bare asserts: an
    # AssertionError and a line number does not tell a test engineer which
    # element was wrong, what it should have read, or what it actually reads.
    source = render(check("text", "label", "Status", expected="Saved"))
    assert "expected {name!r} to read {equals!r}, but it reads {element.text!r}" in (
        source
    )


def test_a_check_retries_rather_than_reading_once():
    # A check recorded immediately after the action it verifies races the
    # application, which has not necessarily finished redrawing.
    source = render(check("text", "label", "Status", expected="Saved"))
    assert "gui.wait_until(" in source
    assert "timeout=5.0" in source


def test_a_password_check_is_redacted_like_typed_input_is():
    source = render(
        check("text", "password text", "Password", expected="hunter2", sensitive=True)
    )
    assert "hunter2" not in source
    assert "equals=SECRET_1" in source
    assert 'SECRET_1 = os.environ["SECRET_1"]' in source


def test_a_password_check_can_be_written_out_when_redaction_is_off():
    source = render(
        check("text", "password text", "Password", expected="hunter2", sensitive=True),
        redact_sensitive=False,
    )
    assert 'equals="hunter2"' in source


def test_a_check_that_resolved_to_nothing_is_reported_not_dropped():
    # With comments off there is no line to hang it on, so it goes in the
    # header instead: a script that looks like it checks something it does
    # not is the failure this whole feature exists to avoid.
    recording = Recording(events=[check("nothing")])
    source = generate(recording, GeneratorOptions(comments=False))
    assert "expect_" not in source
    assert "neither an element nor a window could be identified" in source


def test_an_unknown_check_from_a_later_recorder_is_reported():
    recording = Recording(events=[check("colour")])
    source = generate(recording, GeneratorOptions())
    assert "unknown check 'colour'" in source


def test_checks_still_generate_with_comments_switched_off():
    source = render(check("showing", "push button", "Undo"), comments=False)
    assert "expect_showing(" in source
    assert "# Check" not in source


def test_a_check_requires_the_capabilities_it_uses():
    source = generate(
        Recording(events=[check("text", "label", "Status", expected="Saved")]),
        GeneratorOptions(include_header=False),
    )
    assert "Capability.ELEMENT_TREE" in source
