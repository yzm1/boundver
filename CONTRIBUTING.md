# Contributing to boundver

Thanks for your interest in improving boundver.

## Development setup

- Python 3.10+
- Git

Install the development extras, run the undefined-name/unused-code gate, and
then run tests:

```bash
python -m pip install -e '.[dev]'
python -m ruff check src tests scripts
python -m pytest -q
```

Repository automation does not use this convenience install. CI and release
jobs install complete, exact, hash-locked dependency profiles with
`scripts/install_locked_tools.py`; local project installation then runs
offline with dependency and build isolation disabled. If a tooling dependency
changes, update `scripts/release-tool-lock.toml` and regenerate the reviewed
locks with Python 3.12+:

```bash
python scripts/lock_release_tools.py generate
python scripts/lock_release_tools.py verify
python scripts/lock_release_tools.py check
```

`generate` and `check` query official PyPI metadata. `verify` is network-free
and fails on any manifest, artifact-evidence, hash, include, or generated-lock
drift. Do not edit `scripts/requirements/*.lock` or
`scripts/release-tool-artifacts.json` by hand.

## Pull requests

- Keep changes focused and small.
- Add or update tests for behavior changes.
- Update docs when CLI or config behavior changes.
- Ensure tests pass before submitting.

For documentation changes, follow the
[documentation style guide](docs/documentation-style.md), build the site
strictly, and inspect the advisory prose report:

```bash
python -m mkdocs build --strict
python scripts/check_prose.py
```

Prose findings are review prompts and do not fail by default. Prefer a clear,
accurate exception over rewriting technical meaning to satisfy a heuristic.

## Versioning philosophy

boundver fingerprints are deterministic and content-addressed. Prefer explicit,
strict behavior over implicit fallback to avoid surprising CI behavior.

## Maintainer releases

From a full repository checkout, follow the complete
[release runbook](https://github.com/yzm1/boundver/blob/main/docs/RELEASING.md).
A release is not just
a PyPI upload: the exact commit, docs, changelog, TestPyPI rehearsal, production
PyPI files, immutable GitHub Release, Marketplace version, release assets, and
explicitly approved Action aliases must agree. Breaking releases do not move a
broader alias automatically.

The maintainer tooling is repository-only and is intentionally omitted from the
source distribution. In a clean, current `main` checkout, run
`python3 scripts/publish_release.py check --tag vX.Y.Z`. Start promotion only
through the script's explicitly confirmed `start` command; do not dispatch
`publish.yml` or create release tags by hand.

Publishing an Action release to Marketplace is an owner-only consent step that
must happen while publishing the prepared GitHub Release draft. An immutable
release cannot be published automatically and edited afterward to add the
Marketplace opt-in. Do not advance stable tags until all public surfaces have
been verified.
