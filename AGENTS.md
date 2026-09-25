# Agent instructions

*Regarding AI, adopt or get left behind...*

Working conventions for this repo, learned from the maintainer (Dennis K.
Paulsden) over prior sessions. This is about *how* to work here, not *what
the code does* — read the source and `docs/` for that.

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

## Docs and changelog

`CHANGELOG.md`'s `[Unreleased]` section gets an entry for any user-facing
fix or feature, in the same voice as the existing entries (a bold one-line
summary, then the story: what broke, how it was found, what changed).
Bumping `__version__` and retitling `[Unreleased]` to a dated version header
is ordinary work here too, not a separate ceremony — see `pyguitest`'s
AGENTS.md, "Cutting a version," for the convention; it applies to this
package's own `__version__` the same way.

## Testing against a real desktop

This tool exists to record and replay real input against a real desktop —
recording a session or replaying a generated script moves the actual mouse
and clicks real windows. Don't do either against the user's own live
desktop without telling them first and letting them confirm, the same as
any other real-GUI action.

If a live Windows box is available for testing the win32 capture path, see
`pyguitest`'s AGENTS.md for the logistics (a `SetWinEventHook` gotcha in
particular applies here too, since this project's win32 capture path goes
through the same backend).

## This repo's place in the family

pyguitest-recorder generates scripts against pyguitest's own public API and
tracks it as a version floor in `pyproject.toml`. A pyguitest change that
touches window/element resolution, title matching, or anything the resolver
(`src/pyguitest_recorder/windows/resolver.py`) depends on may need a matching
fix or floor bump here too — check before considering that side's work done.
