"""What the generator writes, and whether the installed pyguitest answers it.

Generated source is parsed and validated against the real API rather than
matched as text, so a call that no longer exists fails here instead of at
replay -- where it would be somebody's test that broke, not this one.
"""

import ast
import math
import re

import pytest

from pyguitest_recorder.generator import GeneratorOptions, generate, validate
from pyguitest_recorder.generator import python as generator_module
from pyguitest_recorder.model import (
    Assertion,
    Click,
    Drag,
    ElementRef,
    Environment,
    HotKey,
    KeyStroke,
    MouseMove,
    Pause,
    Recording,
    Scroll,
    Sync,
    Target,
    TextInput,
    WaitForElement,
    WaitForIdle,
    WaitForWindow,
    WindowActivate,
    WindowRef,
)


def window_pattern(source):
    """The regex the generated script will actually search window titles with.

    A real call site has a literal title.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in (
            "wait_for_window",
            "expect_window",
        ):
            title_arg = node.args[0] if node.args else None
        else:
            continue
        if isinstance(title_arg, ast.Constant):
            return title_arg.value
    raise AssertionError("no wait_for_window call in the generated source")


def render(*events, **options):
    recording = Recording(events=list(events))
    return generate(recording, GeneratorOptions(include_header=False, **options))


def _is_private(name: str) -> bool:
    """Whether a name is private: a leading underscore, not a dunder or `_`.

    `_` on its own is the throwaway convention -- the generated scripts unpack
    `gui.geometry(...)`'s unused width and height into it -- and a dunder is
    the language's own (`__main__`). Neither says anything about what the
    script is allowed to touch, which is what this is asking.
    """
    if name == "_" or (name.startswith("__") and name.endswith("__")):
        return False
    return name.startswith("_")


def _private_references(source: str) -> list[str]:
    """Every private name the generated source refers to.

    The output is meant to be a script someone could have written by hand
    against pyguitest's *public* API. A private attribute in it is a
    dependency on an implementation detail, and a function of its own is
    extensibility that belongs upstream: the generator once wrote private
    helpers into every script that needed one (`_HELPER_SOURCE`, removed in
    0.2.0), and the test below is what stops that coming back.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and _is_private(node.attr):
            found.append(f".{node.attr}")
        elif isinstance(node, ast.Name) and _is_private(node.id):
            found.append(node.id)
        elif isinstance(node, ast.alias) and _is_private(node.name):
            found.append(node.name)
    return sorted(set(found))


def test_the_generated_script_names_nothing_private(window):
    """Public API only, and no helper function of its own.

    Every shape that once needed a private helper written into the output is
    in here -- a named click, a selection, a double click, an expand, typed
    text, a redacted secret, a chord, a drag, a scroll, an inferred wait, a
    waited-for element and an assertion -- so a regression has nowhere to
    hide.
    """
    button = ElementRef(
        role="push button", name="Save", extents=(180, 80, 80, 30), actions=("click",)
    )
    radio = ElementRef(
        role="radio button",
        name="High",
        extents=(20, 60, 120, 22),
        actions=(),
        selectable=True,
    )
    tree_row = ElementRef(
        role="tree item",
        name="Documents",
        extents=(20, 140, 200, 20),
        expanded=False,
    )
    label = ElementRef(role="label", name="Status", extents=(30, 300, 200, 20))
    source = render(
        WindowActivate(window=window),
        Click(target=Target(x=220, y=95, window=window, element=button)),
        Click(target=Target(x=80, y=70, window=window, element=radio)),
        Click(target=Target(x=220, y=95, window=window, element=button), count=2),
        Click(target=Target(x=30, y=150, window=window, element=tree_row), count=2),
        TextInput(text="Ada"),
        TextInput(text="hunter2", sensitive=True),
        HotKey(keys=("ctrl", "s")),
        Drag(
            start=Target(x=40, y=40, window=window),
            end=Target(x=90, y=120, window=window),
        ),
        Scroll(target=Target(x=50, y=50, window=window), dy=-1),
        Pause(seconds=1.25),
        WaitForElement(element=ElementRef(role="dialog", name="Save As")),
        WaitForIdle(window=window, pid=99, timeout=30),
        Assertion(
            check="text",
            target=Target(x=60, y=310, window=window, element=label),
            expected="ready",
        ),
    )
    assert validate(source) == []
    assert _private_references(source) == []
    definitions = [
        node.name
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    ]
    assert definitions == ["main"], "the script defines something of its own"


def test_generated_source_is_valid_and_uses_the_real_api(window, save_button):
    source = render(
        WindowActivate(window=window),
        Click(target=Target(x=180, y=90, window=window, element=save_button)),
        TextInput(text="hello"),
    )
    assert validate(source) == []
    compile(source, "<test>", "exec")


def test_a_recording_that_is_entirely_unaddressable_still_parses():
    # Every event degrading to a comment (no statement at all) used to leave
    # `with pyguitest.connect() as gui:` with nothing indented under it --
    # invalid Python, not just an unhelpful script.
    unaddressable = WindowRef(title="", app_id="")
    source = render(WindowActivate(window=unaddressable))
    assert "pass" in source
    compile(source, "<test>", "exec")
    assert validate(source) == []


@pytest.mark.needs_ruff
def test_named_element_beats_a_coordinate(window, save_button):
    source = render(
        Click(target=Target(x=180, y=90, window=window, element=save_button))
    )
    assert 'gui.button("Save").click()' in source
    assert "move_mouse" not in source


@pytest.mark.needs_ruff
def test_unsugared_role_uses_the_element_form(window):
    item = ElementRef(role="list item", name="Inbox")
    source = render(Click(target=Target(x=1, y=2, window=window, element=item)))
    assert 'gui.element(role=Role.LIST_ITEM, name="Inbox").click()' in source
    assert "from pyguitest import Capability, Role" in source


@pytest.mark.needs_ruff
def test_a_click_on_a_tree_item_is_emitted_as_a_select(window):
    # A tree item answers a click with a *selection*, not an activation.
    # `selectable` comes from pyguitest's own Element.selectable, not from
    # `actions` -- GTK measured live publishes no `select` action at all for
    # a selectable row, so `actions` alone would miss this on that platform.
    item = ElementRef(role="tree item", name="Q1", actions=(), selectable=True)
    source = render(Click(target=Target(x=1, y=2, window=window, element=item)))
    assert 'gui.element(role=Role.TREE_ITEM, name="Q1").select()' in source
    assert "gui.click(" not in source
    assert validate(source) == []


@pytest.mark.needs_ruff
def test_a_click_on_a_radio_is_emitted_as_a_select(window):
    # Measured on Windows 11: a coordinate click on this radio did nothing at
    # all, while `select()` moved it and left the other radio group alone.
    radio = ElementRef(role="radio button", name="High", actions=(), selectable=True)
    source = render(Click(target=Target(x=1, y=2, window=window, element=radio)))
    assert 'gui.element(role=Role.RADIO_BUTTON, name="High").select()' in source
    assert validate(source) == []


@pytest.mark.parametrize("role", ["radio button", "tree item"])
def test_select_wins_over_a_click_action_where_selecting_is_the_act(window, role):
    # UIA publishes `do default action` on these too, and a tree row's
    # default action there is a double click -- `click()` would toggle the
    # row, not select it.
    element = ElementRef(
        role=role, name="Q1", actions=("do default action",), selectable=True
    )
    source = render(Click(target=Target(x=1, y=2, window=window, element=element)))
    assert ".select()" in source
    assert ".click()" not in source


def test_a_selectable_menu_item_still_clicks(window):
    # AT-SPI marks menu items SELECTABLE; selecting one only highlights it.
    item = ElementRef(
        role="menu item", name="Open", actions=("click",), selectable=True
    )
    source = render(Click(target=Target(x=1, y=2, window=window, element=item)))
    assert 'gui.menu_item("Open").click()' in source
    assert ".select()" not in source


def test_an_element_offering_nothing_still_falls_to_a_coordinate(window):
    # A recorded empty tuple is "confirmed: nothing to act through", so the
    # coordinate stays -- and the comment says which element it meant.
    item = ElementRef(role="tree item", name="Q1", actions=())
    source = render(Click(target=Target(x=1, y=2, window=window, element=item)))
    assert "gui.move_mouse(" in source
    assert ".select()" not in source


def test_unknown_actions_do_not_become_a_select(window):
    # `actions is None` is "not captured", not "offered nothing", and guessing
    # a select from it would change what --regenerate writes for recordings
    # made before the field existed.
    item = ElementRef(role="tree item", name="Q1", actions=None)
    source = render(Click(target=Target(x=1, y=2, window=window, element=item)))
    assert ".click()" in source
    assert ".select()" not in source


def test_locators_absolute_keeps_the_coordinate_for_a_selectable_element(window):
    # The setting that says to favour coordinates wins over the name, however
    # the element could have been acted on.
    item = ElementRef(role="tree item", name="Q1", actions=(), selectable=True)
    source = render(
        Click(target=Target(x=1, y=2, window=window, element=item)),
        locators="absolute",
    )
    assert "gui.move_mouse(" in source
    assert ".select()" not in source


def test_a_repeated_click_keeps_its_coordinates(window):
    # `select()` is idempotent, so there is no rendering of a triple click as
    # one: those stay coordinates rather than silently becoming a single act.
    item = ElementRef(role="tree item", name="Q1", actions=(), selectable=True)
    source = render(
        Click(target=Target(x=1, y=2, window=window, element=item), count=3)
    )
    assert ".select()" not in source
    assert "gui.click(" in source


def test_unnamed_element_falls_back_to_coordinates(window):
    anonymous = ElementRef(role="panel")
    source = render(
        Click(target=Target(x=420, y=315, window=window, element=anonymous))
    )
    assert "gui.geometry(example)" in source
    assert "gui.move_mouse(example_x + 320, example_y + 265)" in source


def test_no_window_geometry_falls_back_to_absolute():
    bare = WindowRef(title="Plain")
    source = render(Click(target=Target(x=42, y=99, window=bare)))
    assert "gui.move_mouse(42, 99)" in source


def test_a_totally_unattributed_click_gets_a_settle_wait_first():
    # Seen live on KDE: dismissing GNOME Text Editor's own in-window
    # "Discard changes?" sheet with a click that resolved to no window at
    # all -- not even the desktop -- landed on whatever was still there
    # before the sheet finished appearing. A bare coordinate with a real
    # window (test above) is not this case: it already has some grounding.
    source = render(Click(target=Target(x=1355, y=408)))
    assert "gui.wait(0.3)" in source
    assert "a moment to finish appearing" in source
    assert source.index("gui.wait(0.3)") < source.index("gui.move_mouse(1355, 408)")
    assert validate(source) == []


def test_an_unattributed_scroll_also_gets_a_settle_wait():
    source = render(Scroll(target=Target(x=5, y=5), dy=1))
    assert "gui.wait(0.3)" in source
    assert validate(source) == []


def test_a_click_with_a_real_window_gets_no_settle_wait(window):
    source = render(Click(target=Target(x=180, y=90, window=window)))
    assert "gui.wait(0.3)" not in source


def test_the_settle_wait_only_fires_once_for_a_stationary_click():
    # `_move` skips re-emitting the same point; the wait must be skipped
    # right along with it, or a click-then-click at the same unattributed
    # spot would pay for two waits it never asked for.
    source = render(
        Click(target=Target(x=1355, y=408)),
        Click(target=Target(x=1355, y=408)),
    )
    assert source.count("gui.wait(0.3)") == 1


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


def test_warning_suppressions_are_off_by_default():
    source = render(Pause(seconds=0.1))
    assert "KeymapWarning" not in source
    assert "dbind" not in source
    assert "import warnings" not in source


def test_suppress_keymap_warning_emits_a_filter_with_an_explanatory_comment():
    source = render(Pause(seconds=0.1), suppress_keymap_warning=True)
    assert "import warnings" in source
    assert "from pyguitest.backends.input import KeymapWarning" in source
    assert 'warnings.filterwarnings("ignore", category=KeymapWarning)' in source
    assert "--suppress-keymap-warning" in source
    assert validate(source) == []
    compile(source, "<test>", "exec")


def test_suppress_atspi_chatter_emits_a_best_effort_glib_handler():
    source = render(Pause(seconds=0.1), suppress_atspi_chatter=True)
    assert "from gi.repository import GLib" in source
    assert '"dbind"' in source
    assert "except Exception:" in source
    assert "--suppress-atspi-chatter" in source
    assert validate(source) == []
    compile(source, "<test>", "exec")


def test_both_suppressions_can_be_on_together():
    source = render(
        Pause(seconds=0.1),
        suppress_keymap_warning=True,
        suppress_atspi_chatter=True,
    )
    assert "KeymapWarning" in source
    assert "dbind" in source
    assert validate(source) == []
    compile(source, "<test>", "exec")


def test_double_click_at_a_coordinate_emits_double_click(window):
    source = render(Click(target=Target(x=5, y=5, window=window), count=2))
    assert "gui.double_click()" in source
    assert "gui.click()" not in source


def test_triple_click_emits_three_clicks_with_an_explanation(window):
    source = render(Click(target=Target(x=5, y=5, window=window), count=3))
    assert source.count("gui.click()") == 3
    assert "no primitive past double_click" in source


def test_two_separate_clicks_near_each_other_get_a_settle_wait_between(window):
    # Two distinct Click events (count=1 each) -- normalize.py already
    # decided these were not one double-click -- rendered with nothing in
    # between. Replaying them back to back risks the target app's own
    # double-click gesture firing instead, e.g. toggling a window's
    # maximize state instead of the two single clicks that were recorded.
    source = render(
        Click(target=Target(x=700, y=51, window=window)),
        Click(target=Target(x=698, y=48, window=window)),
    )
    assert source.count("gui.click()") == 2
    assert "gui.wait(0.5)" in source
    first_click = source.index("gui.click()")
    wait_call = source.index("gui.wait(0.5)")
    second_click = source.index("gui.click()", first_click + 1)
    assert first_click < wait_call < second_click


def test_binding_a_window_between_two_clicks_is_not_treated_as_a_pause():
    # Regression, found live on MATE: the click that opens the Applications
    # menu, then the click meant to pick an item out of it, with only a
    # window bind between them. `gui.expect_window("Desktop", ...)` for an
    # already-open window returns instantly, so it separates the two clicks
    # by nothing at all -- the item click landed before the menu had drawn,
    # the menu took it as a dismiss, and the application never started.
    panel = WindowRef(title="Top Panel", app_id="mate-panel", geometry=(0, 0, 1920, 30))
    desktop = WindowRef(title="Desktop", app_id="caja", geometry=(0, 0, 1920, 1080))
    source = render(
        Click(target=Target(x=47, y=16, window=panel)),
        WaitForWindow(window=desktop, timeout=10),
        Click(target=Target(x=226, y=368, window=desktop)),
    )
    assert "gui.wait(0.5)" in source
    assert source.index("gui.wait(0.5)") < source.index("gui.move_mouse(desktop_x")


def test_the_first_click_gets_no_settle_wait(window):
    source = render(Click(target=Target(x=700, y=51, window=window)))
    assert "gui.wait(0.5)" not in source


def test_a_click_after_an_unrelated_action_gets_no_settle_wait(window):
    # Anything else rendered in between -- here, typed text -- is already
    # enough separation; inserting one anyway would be noise.
    source = render(
        Click(target=Target(x=700, y=51, window=window)),
        TextInput(text="hi", target=Target(x=700, y=51, window=window)),
        Click(target=Target(x=698, y=48, window=window)),
    )
    assert "gui.wait(0.5)" not in source


def test_a_double_click_event_gets_no_internal_settle_wait(window):
    # One Click event with count=2 is a real recorded double-click --
    # normalize.py already decided the two presses belonged together, and
    # inserting a wait here would be the same bug the settle wait exists to
    # prevent, turning a real double-click into two single ones on replay.
    source = render(Click(target=Target(x=5, y=5, window=window), count=2))
    assert "gui.wait(0.5)" not in source


def test_double_click_on_a_named_element_still_degrades(window, save_button):
    # save_button carries no rectangle, so there is nowhere to move the
    # pointer to; the comment has to say that rather than blame the library.
    source = render(
        Click(target=Target(x=5, y=5, window=window, element=save_button), count=2)
    )
    assert source.count(".click()") == 2
    assert "the element has no rectangle to move to" in source


@pytest.mark.needs_ruff
def test_text_into_a_named_field_sets_it_directly():
    field = ElementRef(role="entry", name="Name")
    source = render(TextInput(text="Ada", target=Target(x=1, y=1, element=field)))
    assert 'gui.text_field("Name").set_text("Ada")' in source


def test_sensitive_text_never_appears_as_a_literal():
    source = render(TextInput(text="hunter2", sensitive=True))
    assert "hunter2" not in source
    assert 'SECRET_1 = os.environ["SECRET_1"]' in source
    assert "gui.type_text(SECRET_1)" in source


@pytest.mark.needs_ruff
def test_redaction_can_be_disabled():
    source = render(TextInput(text="hunter2", sensitive=True), redact_sensitive=False)
    assert 'gui.type_text("hunter2")' in source


def test_quotes_and_newlines_survive_the_literal():
    source = render(TextInput(text='say "hi"\n'))
    assert validate(source) == []
    namespace = {}
    exec(compile(source, "<test>", "exec"), namespace)  # noqa: S102


@pytest.mark.needs_ruff
def test_hotkey_uses_send_keys_grammar():
    assert 'gui.send_keys("^(s)")' in render(HotKey(keys=("ctrl", "s")))
    assert 'gui.send_keys("^(+(l))")' in render(HotKey(keys=("ctrl", "shift", "l")))


@pytest.mark.needs_ruff
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


@pytest.mark.needs_ruff
def test_wait_for_element_renders_a_role_constant():
    source = render(WaitForElement(element=ElementRef(role="dialog", name="Save As")))
    assert 'gui.wait_for_element(role="dialog", name="Save As", timeout=10)' in source


@pytest.mark.needs_ruff
def test_wait_for_an_ambiguous_element_is_scoped_too(window):
    # The third element-rendering path (sync-inferred waits, alongside
    # clicks and checks) needs the identical ancestry scoping: waiting for
    # one of two same-named elements to appear is just as ambiguous as
    # clicking or checking it.
    in_import = ElementRef(role="push button", name="OK", path=(("dialog", "Import"),))
    in_export = ElementRef(role="push button", name="OK", path=(("dialog", "Export"),))
    source = render(
        WaitForElement(element=in_import),
        Click(target=Target(x=1, y=2, window=window, element=in_export)),
    )
    assert 'gui.element(role="dialog", name="Import")' in source
    assert "gui.wait_for_element(" in source
    assert "within=import_window" in source
    assert validate(source) == []


@pytest.mark.needs_ruff
def test_a_drifting_title_is_dropped_for_the_app_id():
    drifting = WindowRef(
        title="Untitled - Editor", app_id="org.x.E", title_stable=False
    )
    source = render(WindowActivate(window=drifting))
    # The title is not matched on at all -- the whole point of noticing it
    # drifted -- and the helper that looks a window up by app id comes along.
    assert 'gui.wait_for_window("Untitled' not in source
    assert 'gui.expect_window(app_id="org.x.E"' in source
    assert "title drifted while recording" in source
    assert validate(source) == []


def test_a_drifting_title_with_no_app_id_is_used_anyway_with_a_warning():
    drifting = WindowRef(title="Untitled - Editor", title_stable=False)
    source = render(WindowActivate(window=drifting))
    assert "gui.expect_window(" in source
    # The comment wraps, so match a phrase that survives the break.
    assert "match is fragile" in source


def test_an_ambiguous_app_id_with_no_title_is_not_matched_on(save_button):
    # Seen live on KDE: a desktop shell's own desktop, panels, and popups can
    # all report the same app_id ("plasmashell"). Matching on it alone would
    # silently find whichever one happens to be listed first, not the one
    # actually meant, so this must fall back the same way a window with
    # neither field at all does -- not emit `_window_by_app_id`.
    popup = WindowRef(title="", app_id="plasmashell", app_id_ambiguous=True)
    source = render(WindowActivate(window=popup))
    assert "_window_by_app_id" not in source
    # The comment wraps, so match a phrase that survives the break.
    assert "'plasmashell'" in source
    assert "was shared by another" in source
    assert validate(source) == []


def test_an_ambiguous_app_id_drag_endpoint_falls_back_to_absolute_coordinates():
    popup = WindowRef(
        title="",
        app_id="plasmashell",
        app_id_ambiguous=True,
        geometry=(8, 501, 655, 517),
    )
    source = render(
        Drag(
            start=Target(x=100, y=100),
            end=Target(x=667, y=965, window=popup),
        )
    )
    assert "_window_by_app_id" not in source
    assert "667, 965" in source
    assert validate(source) == []


def test_window_variable_is_named_from_the_app_id_when_there_is_no_title():
    # The title leads where there is one -- it is what a reader recognizes --
    # so the app id's reverse-DNS tail is what names a window without one.
    window = WindowRef(title="", app_id="org.gnome.TextEditor")
    source = render(WindowActivate(window=window))
    assert "texteditor = " in source


def test_validate_rejects_a_call_pyguitest_does_not_have():
    problems = validate("def f(gui):\n    gui.telepathy()\n")
    assert problems == ["pyguitest.Session has no method 'telepathy'"]


def test_validate_rejects_a_method_an_element_does_not_have():
    # A method called on an element is an attribute read on a *result*, which
    # the `gui.*` check cannot see: it matches a Name, not a Call. Left
    # unchecked, that shape reached the file and failed at replay with the
    # AttributeError this now reports at generation time.
    source = "def f(gui, Role):\n    gui.element(role=Role.ICON).telepathy()\n"
    assert validate(source) == ["pyguitest.Element has no method 'telepathy'"]


def test_validate_accepts_what_a_generated_script_calls_on_an_element():
    # Every shape the generator emits, plus the accessors it only names when a
    # recording carries that role, and a name -- rather than a call -- whose
    # attribute is nobody's business here.
    source = (
        "def f(gui, Role):\n"
        "    gui.button('Save').click()\n"
        "    gui.checkbox('Enable').click()\n"
        "    gui.dropdown('Country').choose('Norway')\n"
        "    gui.menu_item('Open').click()\n"
        "    gui.link('Docs').click()\n"
        "    gui.text_field('Name').set_text('Ada')\n"
        "    gui.element(role=Role.ICON, name='Computer').double_click()\n"
        "    gui.window_element('Editor').focus()\n"
        "    gui.root_element().children\n"
        "    found = gui.element_at(1, 2)\n"
        "    return found.text\n"
    )
    assert validate(source) == []


def test_validate_sees_the_expression_the_generator_actually_emits(window, monkeypatch):
    # The pair above works on shapes written by hand here. This one takes the
    # generator's own output for a double click and pretends the installed
    # Element has lost the method -- so a check that did not recognise the
    # emitted shape would pass those two and fail this, instead of reporting
    # nothing while looking thorough.
    icon = ElementRef(role="icon", name="Computer", extents=(10, 20, 64, 64))
    source = render(
        Click(target=Target(x=40, y=50, window=window, element=icon), count=2)
    )
    assert validate(source) == []

    real = generator_module._public_names

    def without_double_click(attribute: str) -> frozenset[str]:
        names = real(attribute)
        return names - {"double_click"} if attribute == "Element" else names

    monkeypatch.setattr(generator_module, "_public_names", without_double_click)
    assert validate(source) == ["pyguitest.Element has no method 'double_click'"]


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


@pytest.mark.needs_ruff
def test_a_left_click_still_prefers_the_element(window, save_button):
    source = render(
        Click(target=Target(x=180, y=90, window=window, element=save_button))
    )
    assert 'gui.button("Save").click()' in source


def test_a_click_on_an_element_with_no_atspi_actions_falls_back_to_a_coordinate(
    window,
):
    # KDE's QML-based Kickoff menu publishes its category labels with no
    # AT-SPI Action interface at all. Element.click() falls back to it only
    # after dogtail's own coordinate click fails needing GNOME's ponytail
    # daemon -- absent on KDE -- so with no action either, this element was
    # never clickable through the accessible tree and the recording should
    # say so up front rather than emit a call guaranteed to raise at replay.
    office = ElementRef(
        role="label", name="Office", extents=(40, 300, 120, 24), actions=()
    )
    source = render(Click(target=Target(x=60, y=310, window=window, element=office)))
    assert "gui.element(role=Role.LABEL, name='Office').click()" not in source
    assert "gui.click()" in source
    assert "offered AT-SPI no click or" in source


@pytest.mark.needs_ruff
def test_an_element_with_unrecorded_actions_still_prefers_the_element(window):
    # A session saved before this field existed deserializes with
    # actions=None -- unknown, not "confirmed none" -- and --regenerate on it
    # should not downgrade elements that were working fine to coordinates.
    save = ElementRef(role="push button", name="Save", actions=None)
    source = render(Click(target=Target(x=180, y=90, window=window, element=save)))
    assert 'gui.button("Save").click()' in source


def test_a_scroll_puts_the_pointer_back_where_it_was_recorded(window):
    # The wheel acts on whatever is under the pointer, so a scroll emitted
    # without a move scrolled whichever widget the previous action left it on.
    source = render(Scroll(target=Target(x=420, y=315, window=window), dy=3))
    body = source.split("gui.scroll")[0]
    assert "gui.move_mouse(example_x + 320, example_y + 265)" in body
    assert "gui.scroll(dy=3)" in source


def test_a_scroll_at_the_same_point_does_not_move_twice(window):
    target = Target(x=420, y=315, window=window)
    source = render(Click(target=target), Scroll(target=target, dy=1))
    assert source.count("gui.move_mouse") == 1


def test_a_scroll_of_nothing_emits_nothing(window):
    source = render(Scroll(target=Target(x=1, y=2, window=window)))
    assert "gui.scroll" not in source


@pytest.mark.needs_ruff
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


@pytest.mark.needs_ruff
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
    assert "gui.wait_for_idle(example.pid, timeout=30)" in source
    assert "example = gui.expect_window(" in source
    assert "Capability.WINDOW_PID" in source
    assert validate(source) == []


def test_wait_for_idle_with_no_window_degrades_to_a_sleep():
    source = render(WaitForIdle(pid=99))
    assert "gui.wait(" in source
    assert validate(source) == []


def test_a_window_title_with_regex_metacharacters_is_emitted_unescaped():
    # pyguitest's own wait_for_window matches a plain string literally now
    # (as a substring) -- escaping it here too would double-escape and break
    # the match (see _title_pattern's docstring). "Doc (1) [draft]+" is
    # exactly the kind of title that broke under the old double-escaping
    # regression this guards against.
    title = "Doc (1) [draft]+"
    source = render(
        WindowActivate(window=WindowRef(title=title, geometry=(0, 0, 9, 9)))
    )
    assert window_pattern(source) == title
    assert validate(source) == []


def test_a_title_with_no_metacharacters_is_emitted_unchanged():
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
    assert "example = gui.expect_window(" in source


def test_binding_a_window_never_raises_or_focuses_it(window):
    # Regression, found live on MATE: binding a window used to activate it,
    # so a script that only wanted the desktop's origin to offset a menu
    # click by raised the desktop, dismissed the open menu, and clicked on
    # nothing. Only a recorded WindowActivate may focus anything.
    source = render(Click(target=Target(x=180, y=90, window=window)))
    assert "gui.focus_window(" not in source
    assert "gui.activate_window(" not in source
    assert "Capability.WINDOW_ACTIVATE" not in source


def test_a_recorded_activation_still_focuses_and_declares_it(window):
    source = render(WindowActivate(window=window))
    assert "gui.focus_window(example)" in source
    assert "Capability.WINDOW_ACTIVATE" in source


def test_typing_confirms_focus_first(window):
    # Regression: a keystroke goes to whatever holds focus, and a freshly
    # opened window can exist before the window manager has focused it --
    # seen live twice as typed text landing in the terminal the replay was
    # running in. A click lands at a coordinate regardless; typing does not.
    source = render(
        Click(target=Target(x=10, y=10, window=window)),
        TextInput(text="Ada", target=Target(x=10, y=10, window=window)),
    )
    assert "gui.focus_window(example)" in source
    assert source.index("gui.focus_window(example)") < source.index("gui.type_text(")
    assert "Capability.WINDOW_ACTIVATE" in source


def test_focus_is_confirmed_once_per_window_not_once_per_run(window):
    source = render(
        TextInput(text="one", target=Target(x=10, y=10, window=window)),
        TextInput(text="two", target=Target(x=10, y=10, window=window)),
    )
    assert source.count("gui.focus_window(") == 1


def test_a_key_tap_confirms_focus_too(window):
    source = render(KeyStroke(key="Return", target=Target(x=10, y=10, window=window)))
    assert "gui.focus_window(example)" in source


@pytest.mark.needs_ruff
def test_typing_with_no_window_to_focus_still_renders(window):
    source = render(TextInput(text="Ada", target=Target(x=10, y=10)))
    assert "gui.focus_window(" not in source
    assert 'gui.type_text("Ada")' in source


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
    assert source.count("gui.geometry(example)") == 1


# -- checks ------------------------------------------------------------------


def check(kind, role=None, name="", expected=None, window=None, sensitive=False):
    element = ElementRef(role=role, name=name) if role else None
    return Assertion(
        check=kind,
        target=Target(x=10, y=20, window=window, element=element),
        expected=expected,
        sensitive=sensitive,
    )


@pytest.mark.needs_ruff
def test_a_text_check_names_the_element_and_what_it_read():
    source = render(check("text", "label", "Status", expected="Saved"))
    assert 'gui.expect_text(role=Role.LABEL, name="Status", equals="Saved")' in source
    assert "# Check: 'Status' reads 'Saved'" in source
    assert validate(source) == []


@pytest.mark.needs_ruff
def test_a_checked_check_renders_the_recorded_state():
    source = render(check("checked", "check box", "Read only", expected=True))
    assert (
        'gui.expect_checked(role=Role.CHECK_BOX, name="Read only", checked=True)'
        in source
    )
    assert validate(source) == []


@pytest.mark.needs_ruff
def test_a_showing_check_is_the_floor_for_a_button():
    source = render(check("showing", "push button", "Undo"))
    assert 'gui.expect_showing(role=Role.PUSH_BUTTON, name="Undo")' in source
    assert validate(source) == []


@pytest.mark.needs_ruff
def test_a_check_on_an_ambiguous_element_is_scoped_too(window):
    # Ancestry disambiguation was built for clicked elements first; checks
    # go through a separate rendering path (_expect, not _element_expr) and
    # need the exact same scoping, not a parallel implementation that only
    # clicks get to benefit from.
    in_import = ElementRef(
        role="check box", name="Read only", path=(("dialog", "Import"),)
    )
    in_export = ElementRef(
        role="check box", name="Read only", path=(("dialog", "Export"),)
    )
    source = render(
        Assertion(
            check="checked",
            target=Target(x=1, y=2, window=window, element=in_import),
            expected=True,
        ),
        Click(target=Target(x=3, y=4, window=window, element=in_export)),
    )
    assert 'gui.element(role="dialog", name="Import")' in source
    assert "expect_checked(" in source
    # Long enough to be reformatted onto multiple lines by ruff, so check the
    # pieces rather than one contiguous call string.
    assert "within=import_window" in source
    assert validate(source) == []


def test_a_window_check_emits_its_title_unescaped():
    # expect_window forwards straight to wait_for_window, which now matches
    # a plain string literally -- see _title_pattern.
    window = WindowRef(title="Document (1)", app_id="org.example.App")
    source = render(check("window", window=window))
    assert window_pattern(source) == "Document (1)"
    assert validate(source) == []


def test_a_password_check_is_redacted_like_typed_input_is():
    source = render(
        check("text", "password text", "Password", expected="hunter2", sensitive=True)
    )
    assert "hunter2" not in source
    assert "equals=SECRET_1" in source
    assert 'SECRET_1 = os.environ["SECRET_1"]' in source


@pytest.mark.needs_ruff
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
    # The note lives in the trailing comment block, wrapped, so match a
    # phrase short enough to survive the wrap.
    assert "neither an element nor" in source
    assert source.index("neither an element nor") > source.index("__main__")


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


@pytest.mark.needs_ruff
def test_a_button_recorded_under_either_atspi_name_gets_the_sugar(window):
    # at-spi2 renamed the role without changing its integer, so which string
    # a recording carries depends on the version it was made against. 2.61.1
    # emits only "button", and without that spelling every recorded button
    # came out as the longhand element() form.
    for role in ("push button", "button"):
        element = ElementRef(role=role, name="Save")
        source = render(Click(target=Target(x=1, y=2, window=window, element=element)))
        assert 'gui.button("Save").click()' in source, role
        assert validate(source) == []


def test_a_button_recorded_as_button_still_names_the_role_constant(window):
    element = ElementRef(role="button", name="Undo")
    source = render(
        WaitForElement(element=element),
        Click(target=Target(x=1, y=2, window=window, element=element)),
    )
    assert "Role.PUSH_BUTTON" in source
    assert validate(source) == []


@pytest.mark.needs_ruff
def test_two_same_named_buttons_are_scoped_by_their_dialog(window):
    # A "Save" button in a Save As dialog and one in Preferences: identical
    # role+name, so an unscoped gui.element(role=..., name="Save") would
    # match whichever pyguitest's search finds first. "dialog" has no Role
    # constant, so the ancestor renders as a plain role= string -- only the
    # ambiguous push-button role does.
    save_as = ElementRef(role="push button", name="Save", path=(("dialog", "Save As"),))
    preferences = ElementRef(
        role="push button", name="Save", path=(("dialog", "Preferences"),)
    )
    source = render(
        Click(target=Target(x=1, y=2, window=window, element=save_as)),
        Click(target=Target(x=3, y=4, window=window, element=preferences)),
    )
    assert 'gui.element(role="dialog", name="Save As")' in source
    assert 'gui.element(role="dialog", name="Preferences")' in source
    assert 'gui.element(role=Role.PUSH_BUTTON, name="Save", within=' in source
    # No unscoped mention of the ambiguous pair -- every use of it is scoped.
    assert 'gui.element(role=Role.PUSH_BUTTON, name="Save")' not in source
    assert "could not be told apart" not in source
    assert validate(source) == []


@pytest.mark.needs_ruff
def test_disambiguation_walks_past_a_shared_immediate_parent(window):
    # Both buttons sit in a container named "Content" -- identical at the
    # nearest level -- so the disambiguating ancestor has to be found one
    # level further out, where the two dialogs' own names differ.
    in_import = ElementRef(
        role="push button",
        name="OK",
        path=(("frame", "Import"), ("panel", "Content")),
    )
    in_export = ElementRef(
        role="push button",
        name="OK",
        path=(("frame", "Export"), ("panel", "Content")),
    )
    source = render(
        Click(target=Target(x=1, y=2, window=window, element=in_import)),
        Click(target=Target(x=3, y=4, window=window, element=in_export)),
    )
    assert 'gui.element(role="frame", name="Import")' in source
    assert 'gui.element(role="frame", name="Export")' in source
    # The shared "Content" panel never gets bound -- it disambiguates nothing.
    assert 'name="Content"' not in source
    assert validate(source) == []


def test_disambiguation_rejects_an_ancestor_shared_at_a_different_depth(window):
    # An extra unnamed wrapper closest to `a`'s leaf shifts every named
    # ancestor beyond it one level deeper than the same names sit at in
    # `b`'s path -- "General" is `a`'s depth-2 ancestor but `b`'s depth-1
    # one. Comparing ancestors only at matching depths (the old behaviour)
    # missed that "General" is shared, and would have scoped `a` to a
    # container `b` sits in too, matching whichever panel named "General"
    # pyguitest's search happened to find first.
    a = ElementRef(
        role="push button",
        name="Save",
        path=(("box", ""), ("panel", "General"), ("dialog", "Prefs")),
    )
    b = ElementRef(
        role="push button",
        name="Save",
        path=(("panel", "General"), ("dialog", "Prefs")),
    )
    recording = Recording(
        events=[
            Click(target=Target(x=1, y=2, window=window, element=a)),
            Click(target=Target(x=3, y=4, window=window, element=b)),
        ]
    )
    source = generate(recording, GeneratorOptions())
    assert "WARNING: 2 elements named" in source
    assert "told apart by ancestry" in source
    assert "within=" not in source
    assert validate(source) == []


def test_an_unresolvable_collision_warns_instead_of_guessing(window):
    # Both elements sit only under unnamed ancestors, at different depths --
    # their full (role, name, path) keys differ, so this is a real collision,
    # but nothing in either path is nameable, so there is nothing left to
    # disambiguate with. Must not silently emit two identical unscoped
    # lookups that pyguitest's search would resolve arbitrarily.
    #
    # The warning is header-only (like the "check that resolved to nothing"
    # warning), so this goes through generate() directly rather than the
    # include_header=False test helper.
    a = ElementRef(role="label", name="Status", path=(("panel", ""),))
    b = ElementRef(role="label", name="Status", path=(("panel", ""), ("group", "")))
    recording = Recording(
        events=[
            Click(target=Target(x=1, y=2, window=window, element=a)),
            Click(target=Target(x=3, y=4, window=window, element=b)),
        ]
    )
    source = generate(recording, GeneratorOptions())
    assert "WARNING: 2 elements named" in source
    # The comment wraps, so match a phrase that survives the break.
    assert "could not be told" in source
    assert "within=" not in source
    assert validate(source) == []


def test_the_same_element_mentioned_twice_is_not_treated_as_a_collision(window):
    # One widget, clicked twice -- same role, name AND path -- must still
    # render as an ordinary unscoped lookup, not trigger scoping.
    element = ElementRef(role="push button", name="Retry", path=(("dialog", "Sync"),))
    source = render(
        Click(target=Target(x=1, y=2, window=window, element=element)),
        Click(target=Target(x=3, y=4, window=window, element=element)),
    )
    assert "within=" not in source
    assert "could not be told apart" not in source
    assert validate(source) == []


def test_a_window_variable_is_named_from_the_title_a_reader_recognizes():
    # Identity and readability want different fields. Once X11 began
    # reporting app ids, a window titled "Recorder Check" bound to `zenity`.
    window = WindowRef(title="Recorder Check", app_id="Zenity")
    source = render(WindowActivate(window=window))
    assert "recorder_check = gui.expect_window(" in source
    assert "zenity = " not in source


@pytest.mark.needs_ruff
def test_one_window_still_binds_once_however_it_is_named():
    # Two mentions of the SAME window collapse to a single binding. Title is
    # part of the key, but that does not reintroduce the drift problem: the
    # resolver pins a window's title to what it was first seen as (see
    # WindowRef's docstring) and reuses that pinned title for every later
    # mention, so two WindowRefs for one window always carry the same title
    # here, however many times its title actually changed on screen.
    first = WindowRef(title="Untitled", app_id="Editor", geometry=(0, 0, 800, 600))
    second = WindowRef(title="Untitled", app_id="Editor", geometry=(0, 0, 800, 600))
    source = render(
        WindowActivate(window=first),
        WindowActivate(window=second),
    )
    assert source.count('gui.expect_window("') == 1


@pytest.mark.needs_ruff
def test_two_windows_of_one_app_do_not_collapse_to_one_binding():
    # The bug this guards: app_id alone was the key, and app_id names the
    # application, not the window -- two terminal windows of one app share
    # an app_id, so the second one's clicks were silently generated against
    # the first one's binding. Each window's title is pinned by the
    # resolver at first sight (see the test above), so two windows that are
    # genuinely different keep their own, different titles here.
    first = WindowRef(title="~/project", app_id="Terminal", geometry=(0, 0, 800, 600))
    second = WindowRef(title="~/docs", app_id="Terminal", geometry=(900, 0, 800, 600))
    source = render(
        WindowActivate(window=first),
        WindowActivate(window=second),
    )
    assert source.count('gui.expect_window("') == 2


@pytest.mark.needs_ruff
def test_two_windows_with_the_same_first_seen_title_do_not_collapse_either():
    # Found by a repo-wide bug audit, not live: (app_id, title) alone is not
    # enough either -- two windows launched independently (two "Open File"
    # dialogs from different processes, an unnumbered default title on two
    # freshly-opened windows) can be first seen with the identical title,
    # the same failure class as the app_id-alone bug above, just reopened
    # one field over. pid, which already survives on WindowRef, is what
    # distinguishes different processes; see _window_var's own docstring
    # for the one case even pid cannot catch (two dialogs of one process).
    first = WindowRef(
        title="Open File",
        app_id="org.gnome.TextEditor",
        pid=100,
        geometry=(0, 0, 400, 300),
    )
    second = WindowRef(
        title="Open File",
        app_id="org.gnome.TextEditor",
        pid=200,
        geometry=(500, 0, 400, 300),
    )
    source = render(
        WindowActivate(window=first),
        WindowActivate(window=second),
    )
    assert source.count('gui.expect_window("') == 2


def test_a_program_name_app_id_is_not_mistaken_for_reverse_dns():
    # One dot is not reverse-DNS: `check_app.py` was binding to `py`.
    window = WindowRef(title="", app_id="check_app.py")
    source = render(WindowActivate(window=window))
    assert "check_app_py = gui.wait_for_window" in source or "check_app_py" in source
    assert "\npy = " not in source


def test_reverse_dns_app_ids_still_lose_their_leading_segments():
    window = WindowRef(title="", app_id="org.gnome.TextEditor")
    source = render(WindowActivate(window=window))
    assert "texteditor" in source


def test_a_long_window_title_is_cut_at_a_word_boundary():
    # Truncating to the character produced `hello_there_draft_text_edito`,
    # which reads as a typo everywhere it appears.
    window = WindowRef(title="Hello There (Draft) - Text Editor Deluxe")
    source = render(WindowActivate(window=window))
    assert "edito " not in source and "edito." not in source
    assert "hello_there_draft_text = " in source


def test_a_hyphen_and_parens_in_a_title_are_not_escaped_at_all():
    # pyguitest matches a plain string literally now, so nothing here needs
    # escaping -- "Hello \\(Draft\\) \\- Text Editor" would read badly for no
    # benefit, and would in fact be wrong: pyguitest would escape it a
    # second time, turning the literal backslashes into a pattern that
    # matches none of this title's real characters.
    window = WindowRef(title="Hello (Draft) - Text Editor")
    source = render(WindowActivate(window=window))
    assert window_pattern(source) == "Hello (Draft) - Text Editor"


def full(*events, **options):
    """Render with the header, which `render` deliberately omits."""
    recording = Recording(events=list(events))
    recording.environment.recorder_version = "0.1.0"
    return generate(recording, GeneratorOptions(**options))


def test_the_header_names_the_recorder_that_wrote_the_script():
    # The profile says which pyguitest API the calls were checked against;
    # this says which recorder emitted them, which is the question asked when
    # a script has a bug in its own shape rather than in the application.
    source = full(KeyStroke(key="Return"))
    assert "Recorder:    pyguitest-recorder 0.1.0" in source


def test_the_header_names_the_session_the_way_a_reader_would():
    # Environment stores the str() of pyguitest's enum members, and the header
    # printed them as stored: `Recorded on: SessionType.X11 (Compositor.OTHER,
    # MATE)`, where the README shows `x11 (mutter)`. Seen on the first real
    # recording made on MATE.
    recording = Recording(events=[KeyStroke(key="Return")])
    recording.environment.session_type = "SessionType.X11"
    recording.environment.compositor = "Compositor.MUTTER"
    source = generate(recording)
    assert "Recorded on: x11 (mutter)" in source
    recording.environment.desktop = "MATE"
    assert "Recorded on: x11 (mutter, MATE)" in generate(recording)
    assert "SessionType" not in source
    assert "Compositor" not in source


@pytest.mark.needs_ruff
def test_the_header_can_be_switched_off_entirely():
    source = full(KeyStroke(key="Return"), include_header=False)
    assert "Generated by pyguitest-recorder" not in source
    assert 'gui.tap_key("Return")' in source


def test_a_custom_header_leads_but_does_not_replace_provenance():
    # Setting your own header should not silently drop the record of what
    # made the file; --no-header is how you drop it on purpose.
    source = full(KeyStroke(key="Return"), header="(c) ACME. Ticket QA-1234.")
    assert source.startswith('"""(c) ACME. Ticket QA-1234.')
    assert "Generated by pyguitest-recorder. Edit freely." in source
    assert "Recorder:    pyguitest-recorder 0.1.0" in source
    assert validate(source) == []


def test_a_multi_line_custom_header_keeps_its_shape():
    source = full(KeyStroke(key="Return"), header="Line one.\nLine two.")
    assert '"""Line one.\nLine two.\n' in source
    assert validate(source) == []


@pytest.mark.needs_ruff
def test_a_header_containing_triple_quotes_does_not_corrupt_the_module():
    # Found by a repo-wide bug audit, not live: a licence block or a ticket
    # reference containing `"""` (a plausible --header value) used to close
    # the module's own docstring early, turning the rest of the header into
    # bare top-level string statements and leaving the real closing `"""`
    # to reopen an unterminated string that swallowed the rest of the file.
    source = full(KeyStroke(key="Return"), header='Ticket QA-1234"""')
    assert validate(source) == []
    assert 'gui.tap_key("Return")' in source
    assert 'Ticket QA-1234\\"""' in source


@pytest.mark.needs_ruff
def test_a_three_key_combination_keeps_every_modifier():
    assert 'gui.send_keys("^(+(S))")' in render(HotKey(keys=("ctrl", "shift", "S")))


_ROW = 'gui.element(role=Role.TREE_ITEM, name="Documents")'
_TOGGLE = (
    f"if {_ROW}.expanded:\n"
    f"            {_ROW}.collapse()\n"
    "        else:\n"
    f"            {_ROW}.expand()\n"
)


@pytest.mark.needs_ruff
@pytest.mark.parametrize("expanded", [False, True])
def test_a_double_click_on_a_tree_row_replays_as_the_toggle_it_was(window, expanded):
    # Measured live on a GTK3 GtkTreeView: double_click() activates a row
    # rather than opening it, so the row is named and toggled through
    # expand()/collapse() instead. Which one is decided at replay, not from
    # the recorded `expanded`: on Windows that read lands after the native
    # tree view has already toggled the row whenever the consumer runs
    # behind the second press -- a collapsed row recorded True, rendered
    # collapse(), and the replay never opened it. Both recorded states have
    # to render the same toggle for that reason.
    row = ElementRef(role="tree item", name="Documents", expanded=expanded)
    source = render(
        Click(target=Target(x=40, y=50, window=window, element=row), count=2)
    )
    assert _TOGGLE in source
    assert ".double_click()" not in source
    assert validate(source) == []


def test_a_double_click_on_a_non_expandable_element_still_double_clicks(window):
    icon = ElementRef(role="icon", name="Computer", extents=(10, 20, 64, 64))
    source = render(
        Click(target=Target(x=40, y=50, window=window, element=icon), count=2)
    )
    assert ".double_click()" in source
    assert ".expand()" not in source
    assert ".collapse()" not in source


def test_unknown_expanded_state_does_not_become_an_expand(window):
    # expanded is None both for "not expandable" and "recorded before this
    # field existed" -- a recording made before this shipped keeps whatever
    # it rendered as, rather than being guessed into an expand it never saw.
    row = ElementRef(role="tree item", name="Documents", extents=(10, 20, 64, 64))
    source = render(
        Click(target=Target(x=40, y=50, window=window, element=row), count=2)
    )
    assert ".expand()" not in source
    assert ".collapse()" not in source


def test_locators_absolute_keeps_the_coordinate_for_an_expandable_row(window):
    row = ElementRef(role="tree item", name="Documents", expanded=False)
    source = render(
        Click(target=Target(x=40, y=50, window=window, element=row), count=2),
        locators="absolute",
    )
    assert ".expand()" not in source
    assert "gui.double_click()" in source


def test_a_double_click_on_a_named_element_stays_one_gesture(window):
    # Two Element.click() calls are not a double click: each is a separate
    # round trip over the accessibility bus, slower than any toolkit's
    # double-click interval, so a double-clicked folder icon never opens.
    icon = ElementRef(role="icon", name="Computer", extents=(10, 20, 64, 64))
    source = render(
        Click(target=Target(x=40, y=50, window=window, element=icon), count=2)
    )
    expected = 'gui.element(role=Role.ICON, name="Computer").double_click()'
    assert expected in source
    main_body = source.split("def main")[1]
    assert ".click()" not in main_body
    assert "double_click_element" not in source
    assert "Capability.ELEMENT_GEOMETRY" in source
    assert validate(source) == []


@pytest.mark.needs_ruff
def test_the_double_click_names_the_element_not_the_recorded_rectangle():
    # The element stays the locator, and the gesture reads its extents fresh
    # at replay (see pyguitest), so baking the recorded rectangle in here
    # would break the moment the window moved.
    icon = ElementRef(role="icon", name="Computer", extents=(10, 20, 64, 64))
    source = render(Click(target=Target(x=40, y=50, element=icon), count=2))
    assert 'gui.element(role=Role.ICON, name="Computer").double_click()' in source
    assert "10" not in source.split("def main")[1]


def test_a_double_click_with_no_rectangle_falls_back_to_two_clicks(window):
    # Nothing said this element has a position worth trusting, and a move to
    # a rectangle that does not exist is worse than two clicks.
    icon = ElementRef(role="icon", name="Computer")
    source = render(
        Click(target=Target(x=40, y=50, window=window, element=icon), count=2)
    )
    assert source.count(".click()") == 2
    assert "double_click_element" not in source
    assert ".double_click()" not in source


def test_a_redacted_run_says_why_at_the_point_of_use():
    # A reader scanning the body should not have to work out why one
    # `type_text` takes a name where every other takes a string.
    source = render(TextInput(text="hunter2", sensitive=True))
    assert "went into a password field" in source
    assert "hunter2" not in source


def test_text_with_no_identified_field_is_reported_as_a_risk():
    # Redaction works by recognising the field, so text typed somewhere the
    # recording could not name is in the script verbatim -- and used to be so
    # silently. A real network-share password went in that way.
    recording = Recording(events=[TextInput(text="whatever")])
    source = generate(recording, GeneratorOptions())
    assert "could not identify" in source
    assert "--sensitive" in source


def test_text_into_a_named_field_is_not_reported_as_a_risk():
    field = ElementRef(role="entry", name="Name")
    recording = Recording(
        events=[TextInput(text="Ada", target=Target(x=1, y=1, element=field))]
    )
    source = generate(recording, GeneratorOptions())
    assert "could not identify" not in source


def test_a_drag_that_moved_its_own_window_uses_screen_coordinates():
    # The window travels with the pointer, so the offset within it barely
    # changes and both endpoints collapse. A real recording of someone
    # dragging a calculator produced gui.drag((x + 485, y + 49), (x + 485, y + 49)).
    before = WindowRef(title="Calculator", geometry=(100, 100, 400, 300))
    after = WindowRef(title="Calculator", geometry=(300, 250, 400, 300))
    source = render(
        Drag(
            start=Target(x=150, y=140, window=before),
            end=Target(x=350, y=290, window=after),
        )
    )
    assert "gui.drag((150, 140), (350, 290))" in source
    assert "moved the window it began in" in source


def test_a_drag_inside_a_window_that_stayed_put_is_still_relative(window):
    source = render(
        Drag(
            start=Target(x=150, y=100, window=window),
            end=Target(x=300, y=200, window=window),
        )
    )
    assert "example_x +" in source
    assert "moved the window it began in" not in source


def drag_with_route(window, *route, start=(150, 100), end=(300, 200)):
    """A drag between two points of one window, through `route`."""
    return Drag(
        start=Target(x=start[0], y=start[1], window=window),
        end=Target(x=end[0], y=end[1], window=window),
        route=tuple(Target(x=x, y=y, window=window) for x, y in route),
    )


def test_a_drag_carries_the_route_it_was_dragged_along(window):
    # `pyguitest.drag` glides between the two ends, so without this a recorded
    # curve replays as a straight line -- and a straight drag between the same
    # points is a different gesture from the one that was made.
    source = render(
        drag_with_route(window, (200, 120), (240, 160), (280, 140)),
        locators="absolute",
    )
    assert "gui.drag(" in source
    assert "(240, 160)" in waypoints(source)
    assert validate(source) == []


def test_a_drag_with_no_recorded_route_is_still_a_plain_drag(window):
    # Nothing is invented for a recording made without `record_motion`: the two
    # ends are all it has, and `gui.drag` is what renders them.
    source = render(drag_with_route(window), locators="absolute")
    assert "gui.drag((150, 100), (300, 200))" in source
    assert "via=" not in source
    assert validate(source) == []


def test_a_straight_drag_route_needs_no_waypoints(window):
    # Collinear points carry nothing the endpoints do not, exactly as they do
    # not for a movement.
    source = render(
        drag_with_route(window, (200, 133), (250, 166)),
        locators="absolute",
    )
    assert "via=" not in source


def test_a_drag_inside_one_window_writes_its_route_relative_to_that_window(window):
    # The ordinary case: one origin serves the whole list, so the route reads
    # like everything else in the script.
    source = render(drag_with_route(window, (200, 120), (240, 160), (280, 140)))
    assert "example_x +" in source
    assert len(waypoints(source)) == 3
    assert validate(source) == []


def test_a_route_that_crossed_windows_is_written_in_screen_coordinates(window):
    # One `window_x`/`window_y` read serves a `via` list, so a route with no
    # single origin cannot be measured against one. Screen coordinates always
    # can be -- and a drag whose endpoints are in two windows is exactly that.
    other = WindowRef(
        title="Other", app_id="org.example.Other", pid=99, geometry=(0, 0, 800, 600)
    )
    source = render(
        Drag(
            start=Target(x=150, y=100, window=window),
            end=Target(x=300, y=200, window=other),
            route=(
                Target(x=200, y=170, window=window),
                Target(x=240, y=110, window=other),
            ),
        )
    )
    kept = waypoints(source)
    assert len(kept) == 2
    assert all("_x +" not in point for point in kept)
    assert validate(source) == []


def test_a_drag_that_moved_its_window_keeps_its_route_in_screen_coordinates(window):
    # Written in screen coordinates every point stands alone, so the whole
    # route goes in even though the window travelled underneath it.
    before = WindowRef(title="Calculator", geometry=(100, 100, 400, 300))
    after = WindowRef(title="Calculator", geometry=(300, 250, 400, 300))
    source = render(
        Drag(
            start=Target(x=150, y=140, window=before),
            end=Target(x=350, y=290, window=after),
            route=(
                Target(x=200, y=170, window=before),
                Target(x=260, y=150, window=after),
            ),
        )
    )
    assert "gui.drag((150, 140), (350, 290)" in source
    assert "(200, 170)" in waypoints(source)
    # Literals, as the drag's own two ends are: nothing is measured against a
    # window that was travelling underneath the gesture.
    assert "calculator_x" not in source
    assert validate(source) == []


def test_a_hover_renders_as_a_move_and_the_wait_that_makes_it_one(window):
    source = render(
        MouseMove(target=Target(x=165, y=97, window=window), dwell=0.9),
        Click(target=Target(x=326, y=418, window=window)),
    )
    assert "gui.move_mouse(example_x + 65, example_y + 47)" in source
    assert "gui.wait(0.90)" in source
    assert source.index("gui.wait(0.90)") < source.index("example_x + 226")
    assert "Capability.TIMING" in source
    assert validate(source) == []


def test_plain_motion_renders_without_a_wait(window):
    source = render(MouseMove(target=Target(x=65, y=47, window=window)))
    assert "gui.move_mouse(" in source
    assert "gui.wait(" not in source


def test_a_very_long_hover_is_capped(window):
    # Past the point the interface has reacted, the pointer was resting
    # because the person was reading -- replaying that only wastes time.
    source = render(MouseMove(target=Target(x=65, y=47, window=window), dwell=45.0))
    assert "gui.wait(2.00)" in source


# -- how a pointer move is rendered -----------------------------------------
#
# One axis, four values: `teleport` (what this always did), `natural` (one
# shaped call, the same length), `recorded` (that, plus the route as thinned
# waypoints) and `verbatim` (every position with the wait that preceded it,
# which is the only one that replays the recorded clock too). Every test below
# asserts on the generated *text*, which is the point: the choice is made while
# rendering and never touches a display, so it behaves the same on X11, on
# XWayland, on a Wayland session and on a BSD with none of the three. What
# varies by platform is whether pyguitest can inject at all, and that is the
# capability preamble's business, not this one's.


def moves(window, *points, dwell=0.0, screen=0):
    """MouseMove events for a path, resting `dwell` at each of `points`."""
    return [
        MouseMove(target=Target(x=x, y=y, screen=screen, window=window), dwell=dwell)
        for x, y in points
    ]


def timed_moves(window, *stamped):
    """MouseMove events for a path, each carrying when it was recorded."""
    return [
        MouseMove(timestamp=t, target=Target(x=x, y=y, window=window))
        for t, x, y in stamped
    ]


def menu_route(window):
    """The route a hand takes out of a menu: right along a row, then down.

    Positions from a real recording on MATE -- into the Applications menu's
    first row, along it, and then down the submenu column that opened from it.
    The shape is the whole point of it: it never crosses another row of the
    menu itself, which is what a shaped path does instead.
    """
    return moves(
        window,
        (77, 41),
        (170, 36),
        (221, 57),
        (232, 76),
        (236, 132),
        (212, 250),
        (212, 345),
    )


def _to_segment(point, start, end):
    """How far `point` sits from the segment start -> end.

    The measure a route's fidelity is judged by: the distance to the nearest
    point of that segment, not to either of its ends.
    """
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    span = dx * dx + dy * dy
    if not span:
        return math.dist(point, start)
    along = max(0.0, min(1.0, ((point[0] - ax) * dx + (point[1] - ay) * dy) / span))
    return math.dist(point, (ax + along * dx, ay + along * dy))


def waypoints(source):
    """The `via` points a generated script carries, as source text.

    Text rather than numbers because a window-relative point is an expression
    and not a literal -- `example_x + 140` rather than `240` -- so the tests
    that care about the coordinates ask for absolute locators and compare
    against that literal form.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "via":
                    return [ast.unparse(item) for item in keyword.value.elts]
    return []


def test_verbatim_motion_is_every_position_with_the_wait_before_it(window):
    # The one value that replays the clock as well as the path. Three recorded
    # positions 6ms apart are three events 6ms apart and not one call: what the
    # interface saw in between is that interval, and a menu row deciding
    # whether the pointer has *stayed* is reacting to exactly it.
    source = render(
        *timed_moves(window, (0.5, 200, 200), (0.506, 205, 203), (0.512, 210, 206)),
        motion="verbatim",
        locators="absolute",
    )
    assert source.count("gui.move_mouse(") == 3
    assert source.count("gui.wait(0.006)") == 2
    # Nothing is invented between them, and no route is claimed either.
    assert "move_mouse_naturally(" not in source
    assert waypoints(source) == []
    assert validate(source) == []


def test_verbatim_motion_does_not_sleep_through_the_recordings_opening_gap(window):
    # The first event has no earlier statement to be measured from, and the
    # recording's own lead-in is not something a script should replay: the
    # pointer was simply already there before anything happened.
    source = render(
        *timed_moves(window, (3.5, 200, 200), (3.51, 210, 210)),
        motion="verbatim",
        locators="absolute",
    )
    assert "gui.wait(3.500)" not in source
    assert source.count("gui.wait(0.010)") == 1


def test_a_hover_wait_is_not_charged_again_to_the_position_that_leaves_it(window):
    # A rest is one recorded interval, and the position that leaves it is that
    # interval's *end* -- so emitting the hover's wait and then the next
    # event's own timestamp gap would wait out the dwell twice. On the menu
    # this was built for, that dwell is what opened the submenu.
    source = render(
        MouseMove(timestamp=1.0, target=Target(x=57, y=40, window=window), dwell=0.6),
        MouseMove(timestamp=1.6, target=Target(x=180, y=45, window=window)),
        motion="verbatim",
        locators="absolute",
    )
    assert "gui.wait(0.60)" in source
    assert source.count("gui.wait(") == 1


def test_a_gap_too_short_to_deliver_is_not_a_statement(window):
    # Below a millisecond there is nothing to replay -- `time.sleep` does not
    # deliver it -- and a line of noise between every pair of samples is what
    # `verbatim` would otherwise be made of. The 40ms gap after it still is one.
    source = render(
        *timed_moves(window, (0.5, 200, 200), (0.5005, 201, 200), (0.5404, 210, 200)),
        motion="verbatim",
        locators="absolute",
    )
    assert "gui.wait(0.000)" not in source
    assert source.count("gui.wait(") == 1


def test_an_idle_pause_is_not_slept_through_twice_under_verbatim(window):
    # An explicit Pause is timestamped where the idle it stands for *began* --
    # see `_gap` -- and the event that ended that idle sits at the other end of
    # the same seconds. `verbatim` replays the clock from the last statement it
    # emitted, so the sleep a Pause writes has to carry it forward: without
    # that, the interruption is waited out once as the Pause and again as the
    # gap after it, and a 2.5s recording becomes 4.5s of waiting -- on the one
    # value whose whole point is replaying the recorded clock.
    source = render(
        MouseMove(timestamp=0.5, target=Target(x=200, y=200, window=window)),
        Pause(timestamp=1.0, seconds=2.0),
        MouseMove(timestamp=3.0, target=Target(x=210, y=200, window=window)),
        motion="verbatim",
        locators="absolute",
    )
    assert source.count("gui.wait(") == 2
    assert "gui.wait(0.500)" in source  # the gap before the idle began
    assert "gui.wait(2.00)" in source  # the idle itself, once
    assert "gui.wait(2.000)" not in source  # not again as the gap that ended it


def waits(source):
    """Every `gui.wait(...)` a script sleeps through, in seconds."""
    return [float(n) for n in re.findall(r"gui\.wait\(([0-9.]+)\)", source)]


def test_a_rest_is_not_slept_through_again_for_the_positions_inside_it(window):
    # Under `record_motion` every position is an event, the tremor of a resting
    # hand included -- and the hover is only known once the pointer leaves, so it
    # comes out *after* those positions, carrying the time the rest *began*. Each
    # position had already been replayed with the wait that preceded it, so
    # waiting out the whole dwell as well replayed a 0.9s rest as 1.4s, and a
    # real 1.7s one as 3.6s: about twice as long, on the one value whose point is
    # replaying the recorded clock.
    source = render(
        MouseMove(timestamp=1.0, target=Target(x=200, y=100, window=window)),
        MouseMove(timestamp=1.1, target=Target(x=201, y=101, window=window)),
        MouseMove(timestamp=1.5, target=Target(x=202, y=100, window=window)),
        MouseMove(timestamp=1.0, target=Target(x=202, y=100, window=window), dwell=0.9),
        MouseMove(timestamp=1.9, target=Target(x=300, y=140, window=window)),
        motion="verbatim",
        locators="absolute",
    )
    assert sum(waits(source)) == pytest.approx(0.9, abs=0.02)


def test_the_part_of_a_rest_not_yet_replayed_is_what_the_hover_waits_for(window):
    # The tremor stopped 0.4s before the pointer left, and the recording has
    # nothing between those two moments but the rest itself: that stretch is the
    # hover's to wait out, and it is all of it that is.
    source = render(
        MouseMove(timestamp=1.0, target=Target(x=200, y=100, window=window)),
        MouseMove(timestamp=1.5, target=Target(x=202, y=100, window=window)),
        MouseMove(timestamp=1.0, target=Target(x=202, y=100, window=window), dwell=0.9),
        MouseMove(timestamp=1.9, target=Target(x=300, y=140, window=window)),
        motion="verbatim",
        locators="absolute",
    )
    assert waits(source) == [0.5, 0.4]


def test_a_pause_and_a_hover_over_the_same_idle_are_slept_through_once(window):
    # A pointer that stops dead for a second or more is an inferred Pause *and*
    # a hover: the analyzer emits both for the one interval. The Pause is not
    # capped and comes first, so it spends the interval and the hover has nothing
    # left to wait for -- where each used to wait the whole of it.
    source = render(
        MouseMove(timestamp=0.15, target=Target(x=200, y=100, window=window)),
        Pause(timestamp=0.15, seconds=1.7),
        MouseMove(
            timestamp=0.15, target=Target(x=200, y=100, window=window), dwell=1.7
        ),
        MouseMove(timestamp=1.85, target=Target(x=300, y=140, window=window)),
        motion="verbatim",
        locators="absolute",
    )
    assert sum(waits(source)) == pytest.approx(1.7, abs=0.02)


def test_a_hover_wait_is_unchanged_outside_verbatim(window):
    # Every other value replays a hover as the wait it always was, whatever sits
    # around it: only `verbatim` has a clock to keep.
    for motion in ("teleport", "natural", "recorded"):
        source = render(
            MouseMove(timestamp=1.0, target=Target(x=200, y=100, window=window)),
            MouseMove(timestamp=1.5, target=Target(x=202, y=100, window=window)),
            MouseMove(
                timestamp=1.0, target=Target(x=202, y=100, window=window), dwell=0.9
            ),
            motion=motion,
            locators="absolute",
        )
        assert waits(source) == [0.9], motion


def test_teleport_motion_is_unchanged_and_still_the_default(window):
    # The whole reason it is the default: a recording nobody asked anything of
    # comes out exactly as it did before this option existed.
    source = render(*moves(window, (200, 200), (260, 240), (330, 300)))
    assert "gui.move_mouse(" in source
    assert "move_mouse_naturally(" not in source
    assert validate(source) == []


def test_natural_motion_is_one_call_and_carries_no_waypoints(window):
    source = render(
        *moves(window, (200, 200), (260, 240), (330, 300)), motion="natural"
    )
    # Three recorded positions, one call: the movement is the event, not each
    # sample of it. That is what keeps the script the same length as before.
    assert source.count("gui.move_mouse_naturally(") == 1
    assert waypoints(source) == []
    assert validate(source) == []


def test_a_natural_move_that_started_at_a_rest_keeps_the_route_it_took(window):
    # Live, on MATE: the pointer rested on the Applications menu's first row --
    # which is what opens that row's submenu -- and then travelled right and
    # *down the submenu column* to the item it clicked. Collapsed into one leg
    # and shaped, the path bows by a fraction of that leg (pyguitest's own
    # `arc`), which crosses the menu's other rows; each of those opens its own
    # submenu on the way past, so the click landed on an item the recording
    # never chose and the application never opened. The route *is* the
    # interaction here, not travel between two places.
    rest = moves(window, (57, 40), dwell=0.6)[0]
    source = render(
        rest,
        *menu_route(window),
        motion="natural",
        locators="absolute",
    )
    route = [ast.literal_eval(point) for point in waypoints(source)]
    assert route, "the route through the menu was dropped"
    # Right along the first row, then down inside the submenu column: the part
    # of the trip below that row never goes back across the menu's own width.
    below_the_first_row = [(x, y) for x, y in route if y > 100]
    assert below_the_first_row
    assert all(x > 200 for x, _ in below_the_first_row)
    assert validate(source) == []


def test_a_natural_move_that_ended_at_a_rest_keeps_the_route_it_took(window):
    # The other end of the same rule. Where the pointer comes to rest is where
    # something reacted to it, and it got there by the path it took.
    source = render(
        *menu_route(window),
        moves(window, (212, 345), dwell=0.6)[0],
        motion="natural",
        locators="absolute",
    )
    assert waypoints(source)
    assert validate(source) == []


def test_a_natural_move_with_no_rest_next_to_it_is_still_shaped_and_route_free(window):
    # The reason `natural` still exists: a movement with nothing settled on
    # either side of it is travel, and shaping travel is what it is for. Same
    # route as the test above, no rest: no waypoints.
    source = render(*menu_route(window), motion="natural", locators="absolute")
    assert source.count("gui.move_mouse_naturally(") == 1
    assert waypoints(source) == []
    assert validate(source) == []


def test_a_straight_move_that_touched_a_rest_still_needs_no_waypoints(window):
    # Collinear samples carry nothing the endpoints do not, rest or no rest --
    # a `via` here would be claiming a route that was never there.
    source = render(
        moves(window, (200, 200), dwell=0.5)[0],
        *moves(window, (240, 200), (280, 200), (320, 200)),
        motion="natural",
        locators="absolute",
    )
    assert waypoints(source) == []


def test_a_click_between_movements_does_not_confuse_the_run_split(window):
    # A click has no dwell -- only a pointer position does -- and every event
    # is being asked about, not only the movements. Raising an AttributeError
    # here is how the run splitter announced that it had assumed otherwise.
    # Three calls, not two: each run folds to one, and the click positions the
    # pointer for itself again.
    source = render(
        *moves(window, (10, 10), (40, 40)),
        Click(target=Target(x=60, y=60, window=window)),
        *moves(window, (90, 90), (120, 120)),
        motion="natural",
        locators="absolute",
    )
    assert source.count("gui.move_mouse_naturally(") == 3
    assert "gui.click()" in source
    assert validate(source) == []
    # Collinear samples carry nothing the endpoints do not, so a `via` here
    # would be claiming a route that was never there.
    source = render(
        *moves(window, (200, 200), (220, 200), (240, 200), (260, 200)),
        motion="recorded",
    )
    assert source.count("gui.move_mouse_naturally(") == 1
    assert waypoints(source) == []
    assert validate(source) == []


def test_a_recorded_route_thins_to_the_corners_it_turned_on(window):
    # An L: right along y=200, down x=300, then right again. Ten intermediate
    # samples of travel, two corners worth keeping.
    route = moves(
        window,
        (200, 200),
        (220, 200),
        (240, 200),
        (260, 200),
        (280, 200),
        (300, 200),
        (300, 300),
        (300, 400),
        (300, 500),
        (350, 500),
        (400, 500),
    )
    source = render(*route, motion="recorded", locators="absolute")
    kept = waypoints(source)
    assert "(300, 200)" in kept
    assert "(300, 500)" in kept
    assert len(kept) < 10
    assert validate(source) == []


def test_a_thinned_route_stays_within_the_tolerance_it_claims(window):
    """Every dropped sample must be within the tolerance of the line kept.

    This is the property `_WAYPOINT_TOLERANCE` exists to promise, and the one
    an iterative neighbour-dropping loop quietly broke: 500 samples of a 180px
    curve came out as six chords, up to 286px from the path they were meant to
    reproduce, against a tolerance claiming 8. Douglas-Peucker is *defined* by
    the property, so this pins it rather than re-describing it.
    """
    samples = [
        (120 + index * 3, 300 + int(180 * math.sin(index / 25))) for index in range(500)
    ]
    source = render(*moves(window, *samples), motion="recorded", locators="absolute")
    route = [ast.literal_eval(point) for point in waypoints(source)]
    assert route, "the curve thinned away to nothing"
    # Douglas-Peucker keeps both ends of what it is given, so the polyline
    # starts on the first sample and ends on the last one before the target.
    assert route[0] == samples[0], "the route lost its start"
    assert route[-1] == samples[-2], "the route lost the sample before the target"
    walked = [*route, samples[-1]]
    worst = max(
        min(
            _to_segment(sample, walked[index], walked[index + 1])
            for index in range(len(walked) - 1)
        )
        for sample in samples[1:-1]
    )
    assert worst <= generator_module._WAYPOINT_TOLERANCE + 1, (
        f"{worst:.0f}px off the emitted route, tolerance is "
        f"{generator_module._WAYPOINT_TOLERANCE}"
    )


def test_the_waypoint_cap_backstops_a_route_that_genuinely_wanders(window):
    # A zig-zag has nothing to thin -- every point is a real corner -- so the
    # cap is the only thing keeping the script to a length a person would
    # have been willing to write.
    zigzag = moves(
        window,
        *[(200 + 40 * index, 260 if index % 2 else 140) for index in range(12)],
    )
    thinned = render(*zigzag, motion="recorded", locators="absolute")
    capped = render(*zigzag, motion="recorded", locators="absolute", max_waypoints=2)
    assert len(waypoints(thinned)) > 2
    assert len(waypoints(capped)) == 2


def test_a_hover_is_never_folded_into_a_run(window):
    # The dwell is the whole point of the event: the pointer arrived and
    # stayed, and a menu opened because of it. Folding a run across it would
    # drop the one thing the recording existed to capture.
    events = [
        *moves(window, (200, 200), (220, 220)),
        *moves(window, (300, 300), dwell=1.2),
        *moves(window, (320, 320), (340, 340)),
    ]
    source = render(*events, motion="natural")
    assert source.count("gui.move_mouse_naturally(") == 3
    assert "gui.wait(1.20)" in source


def test_a_run_stops_where_the_window_moved_underneath_it(window):
    # Same title, same pid, different origin. A relative coordinate is an
    # offset into where its window was at the moment of capture, and one `via`
    # is rendered against a single window_x/window_y read -- so these two
    # cannot share a call however alike they look.
    shifted = WindowRef(
        title="Example", app_id="org.example.App", pid=99, geometry=(0, 0, 800, 600)
    )
    source = render(
        MouseMove(target=Target(x=200, y=200, window=window)),
        MouseMove(target=Target(x=260, y=240, window=shifted)),
        motion="natural",
    )
    assert source.count("gui.move_mouse_naturally(") == 2


def test_an_older_pyguitest_falls_back_to_teleports(monkeypatch, window):
    # The method is newer than the release this package's floor names. Emitting
    # it anyway would produce a script that imports cleanly and then fails at
    # replay, which is the failure validate() exists to prevent -- so the
    # recording still renders, as teleports, and the caller is warned.
    monkeypatch.setattr(generator_module, "_session_methods", lambda: frozenset())
    source = render(*moves(window, (200, 200), (260, 240)), motion="natural")
    assert "move_mouse_naturally(" not in source
    assert "gui.move_mouse(" in source
    assert validate(source) == []


def test_a_recorded_route_renders_waypoints_relative_to_its_window(window):
    # The default locator mode, and the one a waypoint list could plausibly
    # break: here a point is an *expression*, `example_x + 200` rather than
    # `300`, and every point in one `via` is measured against the same window
    # origin — which is exactly what `_group_motion` refuses to take for
    # granted.
    source = render(
        *moves(window, (300, 300), (400, 400), (200, 400), (100, 200)),
        motion="recorded",
    )
    assert "via=[" in source
    assert "example_x +" in waypoints(source)[0]
    assert validate(source) == []


def test_a_negative_waypoint_cap_is_treated_as_no_cap(window):
    # `range()` over a negative cap is empty, which emptied the route
    # silently — a nonsense setting quietly switching off the one thing it
    # configures. Nonsense now means "no cap" instead.
    source = render(
        *moves(window, (300, 300), (400, 400), (200, 400), (100, 200)),
        motion="recorded",
        max_waypoints=-1,
    )
    assert "via=[" in source
    assert validate(source) == []


def test_an_unrecognised_motion_value_falls_back_to_the_default(window):
    # Nothing validates config *values*, only keys, so a typo arrives intact.
    # Falling back to the recorded behaviour is the conservative answer: an
    # unknown setting should not silently select one of the new ones.
    source = render(
        *moves(window, (200, 200), (260, 240), (330, 300)),
        motion="naturl",
    )
    assert "gui.move_mouse(" in source
    assert "move_mouse_naturally(" not in source
    assert validate(source) == []


class TestKeyActionSettle:
    """The gap after a keystroke, which normalize.py drops below its threshold.

    Measured on a real Windows 11 recording of `Win+R`, `cmd`, Enter: four
    real gaps of 0.49-0.69s, every one under `pause_threshold`, all four
    dropped -- so the script fired the chord, the text and the Return back to
    back and the Run dialog never had time to take focus.
    """

    def _render(self, events):
        recording = Recording(environment=Environment(session_type="x11"))
        for event in events:
            recording.add(event)
        return generate(recording)

    def test_the_gap_after_a_chord_is_given_back(self):
        source = self._render(
            [
                HotKey(timestamp=1.0, delay=0.0, keys=["meta", "r"]),
                TextInput(timestamp=1.59, delay=0.59, text="cmd"),
            ]
        )
        assert 'gui.send_keys("#(r)")' in source
        assert "gui.wait(0.59)" in source
        assert source.index("send_keys") < source.index("gui.wait(0.59)")
        assert source.index("gui.wait(0.59)") < source.index("type_text")

    def test_a_run_of_fast_keys_gains_no_waits(self):
        # Arrow navigation, or a typed accelerator: the person never paused,
        # so neither should the script.
        source = self._render(
            [
                KeyStroke(timestamp=1.0, delay=0.0, key="Down"),
                KeyStroke(timestamp=1.05, delay=0.05, key="Down"),
                KeyStroke(timestamp=1.10, delay=0.05, key="Return"),
            ]
        )
        assert "gui.wait(" not in source

    def test_a_long_think_is_capped_rather_than_slept_through(self):
        source = self._render(
            [
                KeyStroke(timestamp=1.0, delay=0.0, key="Return"),
                TextInput(timestamp=9.0, delay=8.0, text="hello"),
            ]
        )
        assert "gui.wait(8" not in source
        assert "gui.wait(1.00)" in source

    def test_a_move_does_not_take_the_wait_the_click_after_it_needs(self):
        # A MouseMove is a positioning step and carries its own settle;
        # allowing this one as well put two waits on consecutive lines.
        source = self._render(
            [
                HotKey(timestamp=1.0, delay=0.0, keys=["ctrl", "o"]),
                MouseMove(timestamp=1.6, delay=0.6, target=Target(x=5, y=6)),
            ]
        )
        assert "gui.wait(0.60)" not in source

    def test_a_move_in_between_does_not_lose_the_wait_the_click_needs(self):
        # The wait is owed to the *chord*, and each event's delay is the
        # interval to the one before it: a click 0.2s after a move that was
        # itself 0.7s after the chord read as 0.2s, under the floor, on an
        # interval the recording had spent 0.9s on. The move is a positioning
        # step, so it neither takes the wait nor cancels it.
        source = self._render(
            [
                HotKey(timestamp=1.0, delay=0.0, keys=["meta", "r"]),
                MouseMove(timestamp=1.7, delay=0.7, target=Target(x=5, y=6)),
                Click(timestamp=1.9, delay=0.2, target=Target(x=5, y=6)),
            ]
        )
        assert "gui.wait(0.90)" in source
        assert source.index("send_keys") < source.index("gui.wait(0.90)")

    def test_the_click_that_addresses_the_new_window_takes_the_wait_once(self):
        # The first click after the chord is the line that needed the pause;
        # the one after it is addressing a window that is already up.
        source = self._render(
            [
                HotKey(timestamp=1.0, delay=0.0, keys=["ctrl", "o"]),
                Click(timestamp=1.6, delay=0.6, target=Target(x=5, y=6)),
                Click(timestamp=2.2, delay=0.6, target=Target(x=5, y=6)),
            ]
        )
        assert source.count("gui.wait(0.60)") == 1

    def test_a_recorded_pause_already_stands_for_the_gap(self):
        # A gap long enough to be recorded as a `Pause` is already in the
        # script, so the settle would sleep through the same interval twice.
        source = self._render(
            [
                HotKey(timestamp=1.0, delay=0.0, keys=["meta", "r"]),
                Pause(timestamp=1.6, delay=0.6, seconds=0.6),
                Click(timestamp=1.7, delay=0.1, target=Target(x=5, y=6)),
            ]
        )
        assert "gui.wait(0.60)" in source
        assert "gui.wait(0.70)" not in source


class TestNoForeignPlatformVocabulary:
    """A generated script must not name mechanisms the platform lacks.

    Read by someone at the machine the recording was made on: "offered AT-SPI
    no click action" in a Windows script sends them after an accessibility bus
    their machine has never had.
    """

    def _script(self, session_type):
        window = WindowRef(title="Run", app_id="run", pid=7, geometry=(0, 0, 400, 200))
        element = ElementRef(role="push button", name="OK", actions=())
        recording = Recording(environment=Environment(session_type=session_type))
        recording.add(
            Click(
                timestamp=1.0,
                target=Target(x=20, y=30, window=window, element=element),
            )
        )
        return generate(recording)

    def test_a_windows_script_names_ui_automation_not_atspi(self):
        source = self._script("SessionType.WIN32")
        assert "AT-SPI" not in source
        assert "UI Automation" in source

    def test_a_linux_script_still_names_atspi(self):
        source = self._script("SessionType.X11")
        assert "AT-SPI" in source
        assert "UI Automation" not in source

    def test_a_windows_script_mentions_no_x11_concept_anywhere(self):
        # The whole vocabulary, not just the one comment: $DISPLAY, the X
        # display and the accessibility bus have all reached generated output.
        source = self._script("SessionType.WIN32")
        for absent in ("$DISPLAY", "X display", "accessibility bus", "XWayland"):
            assert absent not in source, f"{absent!r} leaked into a Windows script"

    def test_regenerating_a_windows_recording_anywhere_still_says_windows(
        self, monkeypatch
    ):
        # The recording's platform decides, not the machine regenerating it --
        # `--regenerate` on Linux for a Windows recording is ordinary.
        import pyguitest_recorder.platforms as platforms

        monkeypatch.setattr(platforms.sys, "platform", "linux")
        assert "UI Automation" in self._script("SessionType.WIN32")
