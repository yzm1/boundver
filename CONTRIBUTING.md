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

After the version, changelog, and v2 lockfiles are updated, merge the release PR
only after its full CI matrix passes. Then create a branch named
`release/vX.Y.Z` at the tested `main` commit. The release-tag workflow verifies
that the branch points to current `main` and matches `pyproject.toml` before it
creates the immutable version tag. The tag-triggered publish workflow retests
and builds the package, publishes the verified artifacts to PyPI, creates the
GitHub Release, and advances the stable major tag.
