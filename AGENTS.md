# Agent instructions

*Regarding AI, adopt or get left behind...*

Working conventions for this repo, learned from the maintainer (Dennis K.
Paulsden) over prior sessions. This is about *how* to work here, not *what
the code does* — read the source and `docs/` for that.

## Resolve the issue; a note about it is not a resolution

**The default response to a defect is to fix its cause.** Not to describe
it, not to route around it, not to pin it as a known gap. If you find
yourself reaching for prose where a fix belongs, that is the signal to stop
and go fix it.

None of these count as resolving anything, and none should be the end state
of a task: a paragraph in `CHANGELOG.md` or a docstring saying the thing is
broken; a comment explaining the symptom, however well written; a retry
loop, a widened timeout, a `sleep`, a second attempt "in case the first
misses"; an `except` that turns a failure into a pass; a skipped or
loosened test, or an assertion weakened to match what the code does today.

**The shape this takes here, specifically: a generated script must never
work around a `pyguitest` gap.** If replaying a recording needs something
the library does not expose, the fix is in `pyguitest` — a new `Session`
call, a new `Element` property — not a private helper emitted into the
user's script. A helper in generated output is the workaround par
excellence: it ships the gap to every reader of that file, and the next
recording has to invent it again. Same for the capture backends: a backend
that cannot see an event is a backend to fix, not an event to filter out.

**Phrases to treat as alarms in your own output.** "Left open and
characterised", "reported rather than changed", "recorded here rather than
fixed", "awaits a future round", "out of scope for this change",
"pre-existing, so not mine". When one appears, either fix the thing or ask.

**Diagnose to the cause, not the symptom.** A measurement that says *what*
fails is the start of the work, not the end: keep going until you can say
*why*, then fix that. Two routes differing in one variable is usually the
cheapest way there. A nondeterministic result is a race to find, not a
flake to retry around — this repo's capture-clock epoch bug and its
dead-zone regressions both looked like flakes first.

**The only acceptable reasons to leave something unfixed**, and both end in
asking rather than writing: the fix is the maintainer's call, because it
changes a public contract, a recorded-script format, or something
`pyguitest` owns; or you are genuinely blocked, because the platform is
unreachable this session or the cause lives somewhere you do not control.
Say which, and say what you established before you ran out of road.

**Documentation's job is to record a resolved thing** — the measurement
that proves the fix. A docs change whose subject is an *unfixed* defect
needs the maintainer to have agreed to leave it unfixed first.

## Git workflow

- **AI will never commit.** Never run `git commit` (or `git add` in
  service of one), no exceptions. The user commits their own work, always.
  Make changes, verify them, and leave them in the working tree.
- **Never work on `main`.** Every change goes on a feature branch named
  `P<priority>-<short-kebab-description>` (e.g. `P2-decoration-click-
  misattribution`). If no priority number was given, ask for one before
  starting.
- **Any mention of "commit" from the user — "commit this," "one line
  commit for each," anything — means produce the message text for them to
  use, never run the command.** One line, Conventional Commits style
  (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, …), no body.
- **A multi-repo or multi-file plan approved once is not standing
  approval for the rest of it.** Do one repo (or one file, for something
  like a README pass), stop, report what changed, and ask before doing the
  next. This applies doubly to editing `README.md` specifically — ask
  first even within one approved plan.
- **Local environment specifics stay out of checked-in docs and code.** One
  contributor's machine setup — which VM software, which host OS, personal
  file paths, usernames, hardware — belongs in that person's own private
  notes, never in this repo's docs, `AGENTS.md`, code comments, or commit
  messages. Describe requirements and behavior in portable terms ("a live
  macOS session," not a specific VM stack at a specific local path); a
  future contributor won't have the same setup.

## Verifying a change

`./scripts/pre-commit-test.sh` is the authoritative gate — tests (pytest),
ruff lint, ruff format check, and mypy, mirroring `.github/workflows/ci.yml`'s
`lint` and `tests` jobs. A green run here is what CI means. `--full` adds the
sdist/wheel build and `twine check --strict`; `live` (recording a real
application on a real X server) is deliberately not covered — see the
script's own header before running it by hand.

Fix lint/format/type issues right the first time rather than looping
edit-check-edit-check; run the full gate once near the end of a change, not
after every edit.

## Cross-platform record and playback are the product

A recording's value is a script that replays on a desktop the recording
never saw — a session captured on X11 driving the same application on
Wayland, Windows or a Mac. Keep both halves of that the first test of any
capture, resolver or generator change:

- **Record on every platform the same way.** The capture backends (x11,
  wayland/evdev, win32, macos) feed one canonical event model, and the
  resolver names whatever the platform's accessible tree reports — in
  pyguitest's canonical vocabulary, which is what makes a recording made
  anywhere mean the same thing everywhere.
- **Emit the cross-platform spelling.** Roles render through
  `_ROLE_CONSTANTS` as `Role.*`; elements lead and coordinates follow,
  and that rule is measured, not aesthetic — a replayed screen-coordinate
  click typed three runs' text into GNOME Shell's search box while every
  element-anchored step of the same script held (2026-11-25).
- **New emitted API moves the floor.** Anything the generator can emit
  that an older pyguitest lacks or gets wrong is exactly what `FLOOR`
  carries — see "`PROFILE`, `FLOOR` and the `pyproject.toml` floor move
  together" below.
- **Tables that mirror pyguitest groups are pinned by test, not import.**
  Every `import pyguitest` in this package is function-local on purpose —
  a recorder slow to start is a recorder that misses input — so
  module-level role tables stay strings, and `test_generator.py` holds
  each mirror to the installed `Role` with its delta *named*
  (`_WINDOW_ROLES` is the group plus `application`; `_TEXT_ROLES` the
  group minus `spin button`). A change on either side becomes a decision,
  never drift.

## Docs and changelog

`CHANGELOG.md`'s `[Unreleased]` section gets an entry for any user-facing
fix or feature, in the same voice as the existing entries (a bold one-line
summary, then the story: what broke, how it was found, what changed).
Bumping `__version__` and retitling `[Unreleased]` to a dated version header
is ordinary work here too, not a separate ceremony — see `pyguitest`'s
AGENTS.md, "Cutting a version," for the convention; it applies to this
package's own `__version__` the same way. If the change touches
platform-specific code, do this only after the live check in "Testing
against a real desktop" below, not before.

## Testing against a real desktop

This tool exists to record and replay real input against a real desktop —
recording a session or replaying a generated script moves the actual mouse
and clicks real windows. Don't do either against the user's own live
desktop without telling them first and letting them confirm, the same as
any other real-GUI action. The suite's hardware round-trip tests
(`tests/test_wayland.py::TestHardwareRoundTrip`) are the same kind of action
— they inject real keystrokes and a real click that land in whatever has
focus — so they run only with `PGT_LIVE_INPUT_TESTS=1` set, which is for a
test box, not a desktop someone is using.

If a live Windows box is available for testing the win32 capture path, see
`pyguitest`'s AGENTS.md for the logistics (a `SetWinEventHook` gotcha in
particular applies here too, since this project's win32 capture path goes
through the same backend).

**A platform-specific change needs a live check on that platform before it
ships, not just this repo's mocked test suite.** If a change touches a
capture backend (`backends/windows.py` or the win32 hook path, X11/XRecord,
`backends/wayland.py`'s evdev reader — which has five live compositors to
answer to, see `docs/developers/status.md` — or `backends/macos.py`'s
`CGEventTap`), the resolver's platform-specific
branches (`windows/resolver.py`), or anything else that isn't shared,
platform-agnostic logic, run it against a real desktop on each platform it
touches before bumping the version. CI's fakes verify logic; they can't
catch what only a real desktop exposes — the 0.6.0 fix for special keys
being recorded as their window-server control codes (`'\x1c'` for `Left`,
`'\r'` for `Return`; see `CHANGELOG.md`) was only found because the first
`Recorder` run against a live Mac desktop captured garbage where the fakes
had been happy. Windows logistics are above. For macOS, use whatever live
access you have — a physical Mac, a cloud device, an emulated or
virtualized VM — the specifics are yours to work out and belong in your own
local notes, not in this repo's checked-in docs; `pyguitest`'s
`docs/validation.md` records what one such live run measured, not a
required setup. If a needed platform isn't reachable from here (no live
Windows box, no macOS access this session), say so and ask rather than
shipping the change unverified on that platform.

## This repo's place in the family

pyguitest-recorder generates scripts against pyguitest's own public API and
tracks it as a version floor in `pyproject.toml`. A pyguitest change that
touches window/element resolution, title matching, or anything the resolver
(`src/pyguitest_recorder/windows/resolver.py`) depends on may need a matching
fix or floor bump here too — check before considering that side's work done.

**`PROFILE`, `FLOOR` and the `pyproject.toml` floor move together.** The
generator stamps a `PROFILE` constant (`generator/python.py`) naming the
pyguitest *minor line* it was checked against, and a `FLOOR` constant beside
it naming the full release a generated script needs — the
`require_version(...)` the generator emits derives from `FLOOR`, and `FLOOR`
is the `pyproject.toml` pin. The two constants differ only when a *patch*
release fixed something an emitted call gets wrong (0.26.1: macOS refused the
emitted `Prior`/`Next` before it); `FLOOR` always lies on `PROFILE`'s line.
Raising them is a five-file change, and all five are enforced:
`test_generator.py` ties `PROFILE` to the installed pyguitest and `FLOOR` to
`PROFILE`'s line, and `test_docs.py` holds `FLOOR` equal to the pyproject pin
and checks the `Profile: pyguitest-X.Y` sample-header lines and the floor
string in `README.md`/`docs/` against the code. The `pyguitest-floor` CI job
then installs that floor from PyPI, so recorder CI stays red until the
matching pyguitest release is published — release pyguitest first.
