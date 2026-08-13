# Contributing to boundver

Thanks for your interest in improving boundver.

## Development setup

- Python 3.9+
- Git

Run tests:

```bash
python -m pytest -q
```

## Pull requests

- Keep changes focused and small.
- Add or update tests for behavior changes.
- Update docs when CLI or config behavior changes.
- Ensure tests pass before submitting.

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
