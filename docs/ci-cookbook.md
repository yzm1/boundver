# CI Cookbook

Practical recipes for integrating boundver into CI/CD pipelines.

## GitHub Actions

### Basic PR verification

Blocks merging if the lockfile is stale. Shows a diff when it fails.

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

      - name: Verify lockfile
        run: boundver verify

      - name: Show diff on failure
        if: failure()
        run: |
          boundver generate --out boundary.lock.new.json
          boundver diff boundary.lock.json boundary.lock.new.json
```

### Verify only components that changed in this PR

Skips components that weren't touched, which is faster for large repos:

```yaml
- name: Verify changed components only
  run: |
    boundver verify --changed-from origin/${{ github.base_ref }}
```

If no components changed, the command exits 0 immediately. If a changed component's lockfile entry is stale, it exits 1.

### Generate and commit the lockfile in CI (for auto-update workflows)

Some teams prefer a CI job that regenerates and commits the lockfile automatically rather than requiring developers to regenerate locally:

```yaml
name: Update boundary lockfile
on:
  push:
    branches: [main]

jobs:
  update-lockfile:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - run: pip install boundver

      - name: Regenerate lockfile
        run: boundver generate

      - name: Commit if changed
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add boundary.lock.json
          git diff --cached --quiet || git commit -m "chore: regenerate boundary lockfile [skip ci]"
          git push
```

> **Note:** Lockfiles are always deterministic (no timestamps). The lockfile only changes when actual content changes.

---

## Using slice fingerprints as cache keys

### GitHub Actions cache

Gate downstream jobs on whether the relevant API slice changed:

```yaml
jobs:
  check-api-change:
    runs-on: ubuntu-latest
    outputs:
      api-fingerprint: ${{ steps.fp.outputs.fingerprint }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install boundver
      - name: Extract slice fingerprint
        id: fp
        run: |
          FP=$(python -c "
          import json
          lock = json.load(open('boundary.lock.json'))
          print(lock['slices']['auth-api']['fingerprint'][:16])
          ")
          echo "fingerprint=$FP" >> "$GITHUB_OUTPUT"

  build-consumers:
    needs: check-api-change
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/cache@v4
        with:
          path: consumer-build/
          key: consumer-build-${{ needs.check-api-change.outputs.api-fingerprint }}
      - name: Build consumers (only runs if cache miss)
        run: make build-consumers
```

The build step is a no-op on cache hit. The cache key rotates automatically when the `auth-api` boundary fingerprint changes — and only then.

### Conditional downstream trigger

```bash
# Read fingerprint from lockfile
CURRENT_FP=$(python -c "
import json
lock = json.load(open('boundary.lock.json'))
print(lock['slices']['auth-api']['fingerprint'])
")

# Compare to last-known fingerprint stored as a CI artifact or env var
if [ "$CURRENT_FP" != "$LAST_KNOWN_FP" ]; then
  echo "API boundary changed — triggering downstream pipeline"
  # curl to trigger another pipeline, dispatch a workflow, etc.
fi
```

---

## GitLab CI

```yaml
boundary-verify:
  stage: validate
  image: python:3.11-slim
  script:
    - pip install boundver
    - boundver verify
  only:
    - merge_requests
```

---

## Handling merge conflicts in the lockfile

The lockfile is a JSON file. Merge conflicts happen when two branches update different components concurrently. See [LOCKFILE_MERGE.md](LOCKFILE_MERGE.md) for the recommended merge driver setup.

Quick summary: use `boundver generate` on the merged result and let the tool recompute from source truth rather than trying to resolve JSON conflicts by hand.

---

## JSON output for scripting

All commands that produce output support `--format json` for machine-readable results:

```bash
# Check if lockfile is up to date and capture result
RESULT=$(boundver verify --format json)
OK=$(echo "$RESULT" | python -c "import json,sys; print(json.load(sys.stdin)['ok'])")

if [ "$OK" != "True" ]; then
  echo "Lockfile out of date"
  echo "$RESULT" | python -c "import json,sys; [print(i) for i in json.load(sys.stdin)['issues']]"
  exit 1
fi
```

```bash
# Get slice fingerprint as a single value
python -c "
import json
lock = json.load(open('boundary.lock.json'))
print(lock['slices']['my-slice']['fingerprint'])
"
```

---

## Pre-commit hook

Add a pre-commit hook to catch stale lockfiles before they're pushed:

```bash
# .git/hooks/pre-commit
#!/bin/bash
set -e
if [ -f boundary.config.json ]; then
  boundver verify --source working-tree || {
    echo "Lockfile is stale. Run: boundver generate --source working-tree"
    exit 1
  }
fi
```

Or use the pre-commit framework:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: boundver-verify
        name: Verify boundary lockfile
        entry: boundver verify --source working-tree
        language: system
        pass_filenames: false
        always_run: true
```

---

## Skipping verification for non-component changes

If your repo has docs, CI config, or root-level files that shouldn't trigger a lockfile update, scope your `verify` call to components actually relevant to the change:

```bash
# Only verify components changed since the base branch
boundver verify --changed-from origin/main
```

Components whose paths weren't touched are silently skipped, so unrelated PRs (e.g., README edits) pass without needing a lockfile update.
