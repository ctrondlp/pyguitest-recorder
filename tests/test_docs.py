"""Keep the user documentation in step with the code.

Every drift found in this repo so far has been the same shape: something the
code says that the prose still says the other way. The 0.2.0 release removed
the private ``expect_*`` helpers, raised the pyguitest floor and moved the
default check key off a function key -- and four pages kept describing the
release before it, two of them in code a reader would copy.

Nothing checks prose, so these do. Each one reads the real files and compares
them against the real code, rather than grepping for words:

* the pyguitest floor in the prose equals the one in ``pyproject.toml``
* the sample ``Profile:`` line names the generator's own ``PROFILE``
* no page calls an ``expect_*`` helper in the pre-0.2.0 free-function form
* the default check key the docs name is the one ``Settings`` actually has
* every relative link, and every anchor in a page's own contents, resolves
* every ``--flag`` the docs name exists in the CLI parser

The pages are listed explicitly rather than globbed: ``testable-guis.md`` is
written for application developers and ``docs/developers/`` is rationale, and
neither should be held to the CLI's claims. Add a page here when it starts
making them.
"""

import re
from pathlib import Path

from pyguitest_recorder.cli import build_parser
from pyguitest_recorder.config import Settings
from pyguitest_recorder.generator import PROFILE

_ROOT = Path(__file__).resolve().parent.parent

_PAGES = (
    "README.md",
    "docs/README.md",
    "docs/getting-started.md",
    "docs/recipes.md",
    "docs/troubleshooting.md",
)

# Flags the docs name that belong to something else.
# `--force-renderer-accessibility` is Chromium's, quoted as the fix when a
# Chromium application publishes no accessibility tree; it is not ours to
# define, so it is not a flag this parser should be expected to know.
_NOT_OURS = frozenset({"--force-renderer-accessibility"})

_LINK = re.compile(r"\]\(([^)\s]+)\)")
_TOC = re.compile(r"^- \[[^\]]+\]\(#([^)]+)\)$", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6} (.+)$", re.MULTILINE)
_FLOOR = re.compile(r"pyguitest (\d+\.\d+\.\d+) or newer")
_PROFILE = re.compile(r"^Profile:\s+(\S+)$", re.MULTILINE)
# The pre-0.2.0 shape: the helper called with the session as its first
# argument, the way a copy written into the generated file had to be.
_OLD_HELPER = re.compile(
    r"\b(?:expect_(?:window|element|text|checked|showing)|double_click_element)"
    r"\(\s*gui\b"
)


def _read(page):
    return (_ROOT / page).read_text(encoding="utf-8")


def _slug(heading):
    """The anchor a heading gets, insensitive to how a slugger trims.

    Runs of dashes are collapsed and both ends stripped, so a heading that
    names a flag (``--doctor says ...``) matches an anchor written with one
    leading dash as well as the two a strict slugger would produce. The
    question this answers is "is there a section here", not "is the anchor
    byte-exact".
    """
    text = re.sub(r"[^a-z0-9 \-_]", "", heading.strip().lower())
    return re.sub(r"-+", "-", text.replace(" ", "-")).strip("-")


def _declared_floor():
    """The pyguitest floor from ``pyproject.toml``, as a version string."""
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r'"pyguitest>=(\d+\.\d+\.\d+)"', text)
    assert found, "pyproject.toml declares no pyguitest floor"
    return found.group(1)


def test_documented_pyguitest_floor_matches_packaging():
    """The prose said 0.5.0 for a release after the floor had moved to 0.9.0."""
    floor = _declared_floor()
    for page in _PAGES:
        for stated in _FLOOR.findall(_read(page)):
            assert stated == floor, f"{page} says pyguitest {stated} or newer"


def test_rendered_profile_line_names_the_generator_profile():
    """The samples showed ``Profile: pyguitest-0.5`` while PROFILE was 0.9."""
    seen = 0
    for page in _PAGES:
        for stated in _PROFILE.findall(_read(page)):
            seen += 1
            assert stated == PROFILE, f"{page} shows Profile: {stated}"
    assert seen, "no page shows what a generated file's header looks like"


def test_no_page_calls_a_helper_the_old_way():
    """0.2.0 stopped writing these into the script; they are Session methods."""
    for page in _PAGES:
        old = _OLD_HELPER.search(_read(page))
        assert old is None, f"{page} calls an expect_ helper as a free function"


def test_docs_name_the_check_key_the_code_defaults_to():
    """The docs told users Ctrl+F1 for a release after the default moved."""
    key = Settings().check_key
    for page in ("README.md", "docs/getting-started.md"):
        assert key.lower() in _read(page).lower(), f"{page} does not name {key}"


def test_relative_links_point_at_something_that_exists():
    for page in _PAGES:
        for target in _LINK.findall(_read(page)):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path = target.split("#", 1)[0]
            if not path:
                continue
            linked = (_ROOT / page).parent / path
            assert linked.exists(), f"{page} links to missing {path}"


def test_each_pages_own_table_of_contents_resolves():
    for page in _PAGES:
        text = _read(page)
        headings = {_slug(heading) for heading in _HEADING.findall(text)}
        for anchor in _TOC.findall(text):
            assert _slug(anchor) in headings, f"{page} has no section for #{anchor}"


def test_documented_flags_exist_in_the_parser():
    parser = build_parser()
    known: set[str] = set()
    for action in parser._actions:
        known.update(action.option_strings)
    for page in _PAGES:
        for flag in re.findall(r"(?<![\w-])--[a-z][a-z0-9-]+", _read(page)):
            if flag in _NOT_OURS:
                continue
            assert flag in known, f"{page} documents {flag}, which the parser lacks"
