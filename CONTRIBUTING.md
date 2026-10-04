# Contributing to pyguitest-recorder

Everything here is about working *on* pyguitest-recorder. For using it, see
[README.md](README.md); for how the pieces fit together, see
[docs/developers/architecture.md](docs/developers/architecture.md) and
[docs/developers/status.md](docs/developers/status.md).

## Ways to contribute

Testers and users are welcome here as much as developers, and on a recorder
the first two shade into the third: a recording of an application nobody here
has recorded is itself a contribution, and so is writing down what the
generated script did with it.

- **Reporting a recording that came out wrong** — attach the recording, not
  just the script; see [Filing a bug](#filing-a-bug).
- **Improving documentation** — `docs/` and this file, plus the generated
  script's own footer, which is where a recording's refusals to name an
  element are explained for that specific recording.
  [tests/test_docs.py](tests/test_docs.py) keeps the pages it lists in step
  with the CLI, so a docs change there is a tested change.
- **Building a testable GUI** —
  [docs/testable-guis.md](docs/testable-guis.md) is written to be handed to
  application developers. It is the answer when a recording came out as
  coordinates because of the application rather than the tool.
- **Adding tests** — the suite drives fakes rather than a live desktop, which
  makes a test that pins a normalisation rule the cheapest contribution here.
- **Fixing a bug** — a fix with a regression test beside it is the easiest
  thing to review, and the entries in [CHANGELOG.md](CHANGELOG.md) are usually
  that shape, naming the live run that found the bug.
- **Recording on a platform nobody here has** — see
  [Platform and domain contributions](#platform-and-domain-contributions).

## Before you start

- **Search the issues first, closed ones included.** Much of what reads as a
  bug is a property of the desktop rather than the code, and
  [docs/troubleshooting.md](docs/troubleshooting.md) is symptom-first for
  exactly that reason.
- **Read the generated script's footer before the recorder's code.** Every
  refusal to name an element is recorded there as a note, for that specific
  recording — which is what separates "the recorder could not" from "the
  application would not".
- **Open an issue before a substantial change.** A new flag, a new capture
  backend, or a change to what the generator emits is a decision rather than
  an implementation detail, and
  [docs/developers/status.md](docs/developers/status.md) and
  [docs/developers/architecture.md](docs/developers/architecture.md) are
  where the existing ones are argued.
- **Separate the desktop from the package.** `--doctor` answers three
  questions — can input be captured at all, can clicks be resolved to named
  elements, and is the installed pyguitest new enough — and the middle one
  changes what every other line of a recording means.

## Platform and domain contributions

What a recording can capture is discovered at run time rather than assumed, so
the most useful report is often one from a desktop this project has never been
recorded on: [docs/developers/status.md](docs/developers/status.md) records
what has actually been run and how, and is the ledger to read before claiming
something is missing.

- **Linux / X11** — the whole path: XRecord capture, element and window
  context, and record → generate → replay on a private Xvfb.
- **Linux / Wayland** — there is no capture backend and there cannot be one:
  it is a property of the platform rather than a missing feature, so the
  contribution here is an XWayland report rather than a patch. See
  [why Wayland has no capture
  backend](docs/developers/architecture.md#why-wayland-has-no-capture-backend).
- **Windows** — one desktop so far: the low-level input hook, its keysyms
  through `ToUnicodeEx`, and the hook Windows removes silently.
- **macOS** — one machine so far: the event tap, its keycodes, and the window
  and element context a recording asks for there.

What a contributor needs installed differs by platform — `x11` is a Python
dependency, while `macos` and `atspi` are pyguitest's own extras reached
through this package's — and the acceptance rule does not vary: a change to a
platform's capture or context path needs a live recording on that platform
before it ships. CI's `live` job records a real application on a real X
server, and X11 is the only platform it can do that for.

## Filing a bug

Save the recording as well as the script. `--save-session FILE.json` writes
the raw event stream, and `--regenerate FILE.json` re-renders the script from
it without touching the desktop — so an issue can carry the exact input that
produced the wrong output, and a fix can be checked against it without a
second recording.

Then attach:

- the generated script, footer included, where each refusal to name an element
  is explained;
- the output of `pyguitest-recorder --doctor`, which answers whether input can
  be captured, whether clicks resolve to named elements, and which pyguitest is
  installed beside the generator profile the emitted calls were checked
  against;
- the recording, when the wrong part is what came out rather than what was
  captured.

## Setting up

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[x11,dev]'
```

## Lint, types, tests

```sh
./scripts/pre-commit-test.sh
```

That is the gate — `scripts/pre-commit-test.sh` — and it mirrors
`.github/workflows/ci.yml` rather than inventing a house style: ruff lint,
ruff format, mypy and the suite, in CI's order and over the same paths, with
a pass/fail summary and a non-zero exit when anything failed.

`--full` adds CI's `build` job so far as it is checkable here: the sdist and
wheel built into a temporary directory, `twine check --strict` over them, and
the tag-matches-the-packaged-version check when HEAD is a `v*` tag. An
interpreter without the build and twine packages reports SKIP, not a pass.
`-k NAME` narrows to the checks matching a name, `-v` streams output, `-q`
prints the summary only, `-x` stops at the first failure, and `--help` lists
them. It checks the working tree, not the index.

It wants the `[x11,dev]` extra in the interpreter it points at (`PYTHON=...`
to point it elsewhere), and checks four of those packages up front: without
pytest, ruff or mypy there is nothing to run, and without pyguitest the
generator's own checks pass vacuously — which is worse than failing, so its
absence is a setup problem rather than a skip.

`.github/workflows/ci.yml` is the reference for what "green" means, including
the `live` job, which records a real application on a real X server — the
kind of bug (context created on one display connection, enabled on another;
window context read from the wrong display) that a unit test with a fake
capture backend cannot catch.

## Releasing

Publishing to PyPI is automated and tag-driven. There are no GitHub Releases;
a plain annotated tag is the record, and [CHANGELOG.md](CHANGELOG.md) is the
release notes.

The version lives in exactly one place, `src/pyguitest_recorder/__init__.py`.
`pyproject.toml` reads it from there via `[tool.setuptools.dynamic]`, so the
wheel and `pyguitest_recorder.__version__` cannot drift apart.

A release is an annotated, `v`-prefixed tag:

```sh
git tag -a v0.2.0 -m "0.2.0"
git push origin v0.2.0
```

Publishing runs from CI on that tag using PyPI
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (OIDC), so
there is no API token in repository secrets to leak or rotate. The `publish`
job in `ci.yml` handles it, uploading the artifact the `build` job already
ran `twine check --strict` over.

### One-time setup

**On PyPI** — a Trusted Publisher rather than an API token:

1. pypi.org → *Your account* → *Publishing* → *Add a new pending publisher*
   (the pending form specifically — this project has no release yet, so
   *Manage* → *Publishing* on an existing project isn't an option until
   after the first upload).
2. PyPI Project Name `pyguitest-recorder`, Owner `ctrondlp`, Repository
   `pyguitest-recorder`, Workflow name `ci.yml`, Environment name `pypi`.

The first successful upload converts the pending publisher into an ordinary
project-level one — the pending form is only needed once. Every field above
must match the workflow exactly, or the publish is rejected as a bad
credential at upload time, not when the pending publisher is saved.

**On GitHub** — *Settings* → *Environments* → *New environment* → `pypi`.
Adding a required reviewer on it is worth doing: it makes each publish a
deliberate approval rather than a side effect of pushing a tag, and a PyPI
filename can never be replaced, only yanked and superseded by a new version.
