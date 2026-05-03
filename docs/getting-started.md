# Getting Started with boundver

This guide walks you from zero to a committed lockfile and a passing CI check in one sitting.

## Prerequisites

- Python 3.8+
- Git (any recent version)
- A repository with at least one component directory

No other dependencies are required.

## 1. Install

```bash
pip install boundver
```

Verify the install:

```bash
boundver --help
```

Optional extras for enhanced validation:

```bash
pip install "boundver[schema]"   # jsonschema — stricter config validation
pip install "boundver[yaml]"     # PyYAML — robust YAML version extraction
```

## 2. Create your config

### Option A — Auto-discover (recommended for new adopters)

```bash
boundver init --discover
```

boundver scans for `package.json`, `pyproject.toml`, `Cargo.toml`, and `go.mod` and creates a starter `boundary.config.json` with one component per discovered manifest. Review and adjust the output before committing.

### Option B — Interactive scaffold

```bash
boundver init
```

Creates a minimal config with a placeholder `example-component`. Edit it to match your repo.

### Option C — Write it yourself

Create `boundary.config.json` at the repository root:

```json
{
  "$schema": "https://raw.githubusercontent.com/yzm1/boundver/main/boundary.config.schema.json",
  "project": "my-project",
  "defaults": {
    "compat_mode": "major"
  },
  "components": {
    "auth-service": {
      "path": "services/auth",
      "version_source": { "file": "package.json", "field": "version" },
      "boundary": {
        "provider": "openapi",
        "paths": ["openapi.yaml"]
      }
    }
  },
  "slices": {
    "auth-api": {
      "description": "Auth service public API contract",
      "mode": "boundary",
      "components": ["auth-service"]
    }
  }
}
```

## 3. Validate the config

Before generating anything, confirm the config is valid:

```bash
boundver validate-config
```

A clean run prints `Config is valid.` If there are errors, they describe exactly what to fix — missing fields, unknown providers, path traversal, etc.

## 4. Generate the lockfile

```bash
boundver generate --source working-tree
```

This creates `boundary.lock.json` next to your config. Commit both files.

> **Note:** In CI you'll typically use `--source head` (the default) to hash committed content. `--source working-tree` is convenient locally when files aren't committed yet.

### Inspect the lockfile

```bash
boundver status
```

Sample output:

```
  Project: my-project
  Components: 1
  Slices: 1

  Versioned: 1  |  Unversioned: 0

  Boundary coverage:
    openapi: 1

  Boundary extraction status:
    ok: 1

  Slices:
    auth-api [boundary] (1 components) = a3f9b12c4e1d...
```

## 5. Verify the lockfile

```bash
boundver verify
```

`Lockfile is up to date.` means the current repo state matches what was recorded. If something drifted, you'll see which component and which fingerprint facet (`exact`, `boundary`, or `compat`) changed.

## 6. Commit and push

```bash
git add boundary.config.json boundary.lock.json
git commit -m "feat: add boundver config and initial lockfile"
git push
```

## 7. Add a CI check

Add a step to your PR pipeline that rejects stale lockfiles:

```yaml
# .github/workflows/boundary-check.yml
name: Boundary check
on: [pull_request]

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install boundver
      - run: boundver verify
      - name: Show what changed (on failure)
        if: failure()
        run: |
          boundver generate --out boundary.lock.new.json
          boundver diff boundary.lock.json boundary.lock.new.json
```

The CI check fails if a PR changes a component without regenerating the lockfile. The diff step shows exactly which fingerprints changed and why.

## Next steps

- **Add more components.** See the [config reference](../README.md#config-reference) for all options.
- **Use slice fingerprints as cache keys.** See [CI cookbook](ci-cookbook.md).
- **Adopt incrementally.** See [Gradual adoption guide](gradual-adoption.md).
- **Use `implicit` provider first** if you don't have explicit API boundary files yet — you'll get `exact` fingerprinting right away and can upgrade providers later.
