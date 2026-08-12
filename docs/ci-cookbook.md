# CI cookbook

These recipes keep the source snapshot, lockfile, and enforcement policy explicit. For pull requests, `head` compares the committed PR tree with the committed lockfile.

## GitHub Actions: recommended boundary gate

```yaml
# .github/workflows/boundary-check.yml
name: Contract boundary
on: [pull_request]

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: yzm1/boundver@v0
        with:
          config: boundary.config.json
          lock: boundary.lock.json
          source: head
          facets: boundary,compat
```

This gate permits exact-only internal refactors while failing declared boundary and compatibility changes. The public Action installs the boundver version bundled with `yzm1/boundver@v0`; no separate Python setup or install step is needed.

The four shown inputs are the portable baseline:

- `config`: configuration path relative to the checkout root
- `lock`: lockfile path relative to the checkout root
- `source`: `head`, `index`, or `working-tree`
- `facets`: comma-separated fingerprints that should fail the job

The Action also supports `components`, `changed-from`, `fail-fast`, `update`, and `python-version` for specialized workflows.

## Pick a signal-to-noise policy

The most useful first gate is usually:

```yaml
facets: boundary,compat
```

Other common policies are:

| Policy | Facets | What fails |
|---|---|---|
| Consumer-facing | `boundary,compat` | Declared API and compatibility drift |
| Behavior-sensitive | `behavior,boundary,compat` | Runtime contract, API, and compatibility drift |
| Full lock hygiene | `exact,behavior,boundary,compat` | Any tracked component drift |

You can keep policy in the repository instead of the workflow:

```json
{
  "defaults": {
    "verify_facets": ["boundary", "compat"]
  }
}
```

Then omit `facets` when invoking the CLI directly. The Action intentionally defaults to `boundary,compat`.

Drift outside the gate is returned as a non-gating observation. Structural, metadata, and digest errors remain failures because they make the comparison unreliable.

## Verify only components touched by a pull request

Fetch history and compare with the base branch:

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0

- uses: yzm1/boundver@v0
  with:
    config: boundary.config.json
    lock: boundary.lock.json
    source: head
    facets: boundary,compat
    changed-from: origin/${{ github.base_ref }}
```

`--changed-from` reports which components and slices are affected by Git-tracked
paths, while still recomputing the full lock for integrity. This deliberately
prevents an unchanged path set from hiding stale provider metadata or a tampered
entry. An invalid or unavailable ref is a usage error rather than a silent pass,
and configuration changes affect every component because they can add, remove,
or redefine contracts independently of source-file paths.

## Explicit install instead of the Action

Pin the package version for reproducibility:

```yaml
steps:
  - uses: actions/checkout@v4
    with:
      fetch-depth: 0
  - uses: actions/setup-python@v6
    with:
      python-version: "3.12"
  - run: python -m pip install "boundver==0.10.0"
  - run: boundver verify --source head --facets boundary,compat
```

Use this form when your organization mirrors PyPI or centrally manages Python environments.

## Inspect failures without regenerating in CI

Lockfile updates are review decisions, so a PR gate should normally report drift and leave the checkout unchanged:

```yaml
- name: Verify contracts
  run: boundver verify --source head --facets boundary,compat

- name: Print machine-readable details
  if: failure()
  run: boundver verify --source head --facets boundary,compat --format json
```

Locally, the author can inspect and accept a gated change in one step:

```bash
boundver verify --source working-tree --facets boundary,compat
boundver why payment-api --source working-tree
boundver verify --source working-tree --facets boundary,compat --update
git diff -- boundary.lock.json
```

To refresh non-gating exact and behavior drift too, pass all four facets to the update command.

## Exit-code-aware automation

`verify` returns the highest selected severity:

| Code | Meaning |
|---:|---|
| `0` | Clean |
| `1` | Exact or metadata drift |
| `2` | Usage or configuration error |
| `3` | Behavior drift |
| `4` | Boundary drift |
| `5` | Compatibility drift |

For example, a shell job can distinguish an invalid invocation from contract drift:

```bash
set +e
boundver verify --source head --facets behavior,boundary,compat --format json > boundver-result.json
code=$?
set -e

case "$code" in
  0) echo "Declared contracts are current" ;;
  2) echo "boundver could not perform a reliable check" >&2; exit 2 ;;
  3) echo "Behavior contract changed" >&2; exit 3 ;;
  4) echo "Boundary changed; re-verify consumers" >&2; exit 4 ;;
  5) echo "Compatibility family changed" >&2; exit 5 ;;
  *) echo "Unexpected boundver result: $code" >&2; exit "$code" ;;
esac
```

The Action exposes `exit-code`, newline-separated `issues`, and `observations` outputs for workflows that use `continue-on-error` and apply their own policy.

## GitLab CI

```yaml
boundary-verify:
  stage: test
  image: python:3.12-slim
  before_script:
    - python -m pip install "boundver==0.10.0"
  script:
    - boundver verify --source head --facets boundary,compat
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
```

## Pre-commit

Working-tree verification should be paired with working-tree updates:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: boundver-verify
        name: Verify declared boundaries
        entry: boundver verify --source working-tree --facets boundary,compat
        language: system
        pass_filenames: false
        always_run: true
```

If the hook finds intentional drift, review it and run:

```bash
boundver verify --source working-tree --facets boundary,compat --update
git add boundary.lock.json
```

New files must already be known to Git for working-tree and index modes.

## Use a slice fingerprint as a cache key

A slice combines one facet from several components. Read its committed fingerprint into a workflow output:

```yaml
jobs:
  contract-key:
    runs-on: ubuntu-latest
    outputs:
      fingerprint: ${{ steps.key.outputs.fingerprint }}
    steps:
      - uses: actions/checkout@v4
      - id: key
        shell: bash
        run: |
          value=$(python -c 'import json; print(json.load(open("boundary.lock.json"))["slices"]["checkout-contracts"]["fingerprint"])')
          echo "fingerprint=$value" >> "$GITHUB_OUTPUT"

  build-consumers:
    needs: contract-key
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/cache@v4
        with:
          path: consumer-build
          key: consumer-${{ needs.contract-key.outputs.fingerprint }}
      - run: make build-consumers
```

A boundary-mode slice rotates only when one of its member boundaries changes. It is a deterministic cache key, not a substitute for running consumer tests after a contract change.

## Concurrent lockfile updates

Do not hand-edit JSON conflict hunks and do not run generation inside a Git merge driver. Finish merging source and configuration, then regenerate from the materialized working tree. See [Lockfile merge strategy](LOCKFILE_MERGE.md) for the command sequence and an optional post-merge hook.
