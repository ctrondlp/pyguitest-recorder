# Security

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting:
**[open a draft advisory](https://github.com/ctrondlp/pyguitest-recorder/security/advisories/new)**.
That keeps the report between you and the maintainer until there is a fix to
publish. Please do not open a public issue for something you believe is
exploitable.

A useful report says what an attacker starts with, what they end up able to
do, and which of the two is more than they had before. `pyguitest-recorder
--doctor` identifies the session, the selected capture backend and the
permission tier it found, which is usually the context that matters.

**Do not attach a session `.json` to a report.** They are credential-bearing
by design (see below) — describe the shape of the problem instead, or build a
reproduction by typing a known dummy string.

## What this project is

The recorder observes input across the whole desktop. That is not a side
effect of how it is built; it is what recording a GUI means. On X11 it reads
XRecord, on native Wayland the kernel's input devices below the compositor, on
Windows a low-level input hook, on macOS a `CGEventTap` — and in every case it
can see keystrokes typed into applications other than the one being recorded,
including your password manager. The README says so in its own words, and
`--doctor` says which mechanism is in use.

So a report that amounts to "the recorder can see input to other
applications" describes the intended function. The security question is
narrower: **can that capability be obtained, kept, or widened beyond the
authorization it was granted under, or does a privacy control fail to do what
it says?** In scope:

- Capture starting without the permission tier the platform requires for it —
  an event tap without its TCC grant, `/dev/input` read without `input`-group
  membership or a udev ACL, a hook installed where the OS should have refused.
- A privacy control that does not hold: text written where `--sensitive` or
  password detection said it would not be, a window recorded that
  `--exclude-window` matched, redaction that can be reversed from what is
  saved.
- A secret reaching a file, a log, or a terminal that should not have it —
  including modes on a written file that let another local account read it.
- Code execution from loading a recording. `--regenerate` parses attacker-
  shaped JSON and renders Python from it; anything that escapes the literal
  escaping in `generator/python.py` and becomes executable code, or that
  executes during *parsing*, is in scope.
- Privilege escalation, or crossing a sandbox boundary through this package.

## Known exposures, documented on purpose

Read these before filing; each is a deliberate decision with its reasoning
written down.

- **A saved `--save-session` recording is a credential-bearing artifact**,
  with redaction on and `--sensitive` set. Redaction is a *generation*-time
  step, so that regenerating later can still produce a working script; the
  saved events keep the text. The file is created mode 0600 and the
  documentation says to treat it like a password file. See
  [docs/recipes.md](docs/recipes.md#keeping-secrets-out-of-the-script).
- **`--sensitive` governs the generated script, not the session file.** Its
  `--help` and the recipe both say so.
- **`--exclude-window` is a convenience, not a security boundary.** It needs
  the window to have been *identified*, so a desktop whose hit-testing fails
  has nothing to exclude by. It keeps events out of your recording; it does
  not stop the capture mechanism from seeing them.
- **`--record-raw` keeps the unfiltered stream, and is refused in combination
  with `--sensitive` or `--exclude-window`** — both promise something the raw
  stream, captured before any of it is resolved, cannot deliver. The refusal
  is the fix for that class; a combination that *does* get through is a
  report.
- **Password detection is best-effort and depends on accessibility.** A
  toolkit that does not publish a password role, a terminal prompt, or a
  custom-rendered login screen cannot be detected as one — which is what
  `--sensitive` exists for. The stale-focus gate fails closed and withholds
  text it cannot attribute; a case where it fails *open* is a report.
- **Native Wayland capture reads all readable input devices**, which can
  include virtual devices created by other automation. Prefer a scoped udev
  ACL over `input`-group membership.

The library this generates scripts for,
[`pyguitest`](https://github.com/ctrondlp/pyguitest), has its own
`SECURITY.md`; report injection, portal and GNOME Shell extension issues
there.

## Supported versions

Fixes land on the latest release line. There are no long-term support
branches: if you are more than a minor behind, the first question will be
whether the current release still has the problem.
