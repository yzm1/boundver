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
