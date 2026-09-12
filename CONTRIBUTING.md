# Contributing to pyguitest-recorder

Everything here is about working *on* pyguitest-recorder. For using it, see
[README.md](README.md); for how the pieces fit together, see
[docs/developers/architecture.md](docs/developers/architecture.md) and
[docs/developers/status.md](docs/developers/status.md).

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
