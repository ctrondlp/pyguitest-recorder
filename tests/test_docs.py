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
* the example wait every page quotes is the arithmetic the analyzer applies
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

from pyguitest_recorder.analyzer import SyncOptions, infer_synchronization
from pyguitest_recorder.cli import build_parser
from pyguitest_recorder.config import Settings
from pyguitest_recorder.generator import PROFILE
from pyguitest_recorder.model import Click, Pause, Target, WaitForWindow, WindowRef

_MAIN = WindowRef(
    title="Editor", app_id="org.x.Editor", pid=11, geometry=(0, 0, 800, 600)
)
_DIALOG = WindowRef(title="Save As", app_id="org.x.Editor.Dialog", pid=11)

_ROOT = Path(__file__).resolve().parent.parent

# The canonical URL for a file in this repository. The README writes its
# cross-references this way because the same file is the PyPI long
# description, where a relative link 404s rather than resolving -- so they
# are checked as URLs, against the tree.
_REPO = "https://github.com/ctrondlp/pyguitest-recorder/"

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


def test_the_documented_timeout_example_is_the_scaling_the_analyzer_applies():
    """The pages show 12.4s above ``timeout=13``, and both numbers are the code's.

    A wait is the pause the recording took, rounded up to the next whole second,
    so the second number is derived here by running the analyzer over the pair of
    clicks the pages describe rather than by matching words. The floor and the cap
    the prose quotes are read off the same `SyncOptions` the analyzer defaults to.
    """
    events = [
        Click(timestamp=1.0, target=Target(x=10, y=10, window=_MAIN)),
        Pause(timestamp=1.1, seconds=12.4),
        Click(timestamp=14.0, target=Target(x=10, y=10, window=_DIALOG)),
    ]
    waits = [e for e in infer_synchronization(events) if isinstance(e, WaitForWindow)]
    assert waits[-1].timeout == 13.0, "the analyzer no longer writes the example"

    options = SyncOptions()
    for page in ("README.md", "docs/getting-started.md"):
        text = _read(page)
        assert "the recording waited 12.4s here" in text, f"{page} lost the example"
        assert "timeout=13" in text, f"{page} lost the example's timeout"
        # The header block each page reproduces, in the analyzer's own numbers.
        assert (
            f"floor {options.min_timeout:g}s, cap {options.max_timeout:g}s" in text
        ), f"{page} quotes a floor or cap the analyzer does not apply"


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
            if linked.is_dir():
                # A directory is only a page where it has a README. Without one
                # the link lands a reader on a file listing, which is what
                # `docs/developers/` did until the README named the pages in
                # it -- and `exists()` alone cannot tell the two apart.
                assert (linked / "README.md").exists(), (
                    f"{page} links to the directory {path}, which has no README"
                )


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


def test_the_readme_needs_no_repository_to_render():
    """README.md is the PyPI long description, and PyPI serves it alone.

    Every `docs/...` cross-reference in it used to be relative, which is what
    works in a checkout and a 404 on the index -- invisible to the
    relative-link check above, whose whole premise is a tree to resolve
    against. In-page anchors are fine: the headings travel with the file.
    """
    for target in _LINK.findall(_read("README.md")):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        assert target.startswith("#"), (
            f"README.md links to {target}; PyPI cannot serve that, so the link "
            "has to be an absolute URL"
        )


def test_absolute_links_into_this_repository_resolve():
    """The README's cross-references are absolute now, so check those instead.

    `blob/main/<path>` names a file and `tree/main/<dir>` names a directory,
    and either is a 404 once what it names is renamed or unshipped -- the same
    question the relative-link test asks of the pages under docs/.
    """
    checked = 0
    for page in _PAGES:
        for target in _LINK.findall(_read(page)):
            if not target.startswith(_REPO):
                continue
            rest = target[len(_REPO) :].split("#", 1)[0]
            kind, _, path = rest.partition("/main/")
            if not path or kind not in ("blob", "tree"):
                continue
            checked += 1
            linked = _ROOT / path
            assert linked.exists(), f"{page} links to missing {path}"
            if kind == "tree":
                assert (linked / "README.md").exists(), (
                    f"{page} links to the directory {path}, which has no README"
                )
    assert checked > 5, "the README's links went unchecked"


def test_every_page_these_tests_read_is_shipped_in_the_sdist():
    """`docs/` was in no MANIFEST.in, so the sdist carried none of it.

    Setuptools infers README, LICENSE, pyproject.toml and tests/, and infers
    nothing else: the pages this file reads, and every `docs/...` link the
    README makes, existed in the repository and nowhere else. A packager
    building from the sdist is who finds that out, by running the suite
    there. pyguitest has a `--full` sdist self-test for the same failure; this
    is the cheap version of it, reading the manifest rather than building.
    """
    manifest = (_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    for page in _PAGES:
        directory = Path(page).parent
        if str(directory) == ".":
            continue  # setuptools ships the README without being told
        assert f"recursive-include {directory} *" in manifest, (
            f"{page} is read by these tests, and {directory}/ is not in MANIFEST.in"
        )
    for name in ("config.example.toml", "CHANGELOG.md"):
        assert f"include {name}" in manifest, (
            f"{name} is linked by the README and not shipped"
        )
