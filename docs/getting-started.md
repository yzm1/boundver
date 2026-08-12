# Getting started with boundver

This guide takes a Git repository from no configuration to a reviewed lockfile and a useful pull-request gate.

## Prerequisites

- Python 3.9 or newer
- Git
- At least one tracked component or manifest

Run every command below from the repository root.

## 1. Install

```bash
python -m pip install boundver
boundver --version
```

Optional extras add a full JSON Schema validator and robust YAML parsing:

```bash
python -m pip install "boundver[schema,yaml]"
```

## 2. Discover components

For an existing repository, start with tracked manifests:

```bash
boundver discover
boundver init --discover
```

Discovery uses Git instead of crawling ignored dependency trees. It recognizes common Python, JavaScript/TypeScript, Rust, and Go manifests. Review the generated `boundary.config.json`; discovery cannot decide which files form your public contract.

If discovery finds nothing useful, create a minimal scaffold:

```bash
boundver init
```

`init` is non-interactive. Edit the placeholder component before continuing.

## 3. Declare the contract and its consumers

Here is a useful starting configuration:

```json
{
  "$schema": "https://raw.githubusercontent.com/yzm1/boundver/main/boundary.config.schema.json",
  "project": "checkout-platform",
  "defaults": {
    "compat_mode": "major",
    "verify_facets": ["boundary", "compat"]
  },
  "components": {
    "payment-api": {
      "path": "services/payment",
      "version_source": {"file": "package.json", "field": "version"},
      "boundary": {
        "provider": "openapi",
        "paths": ["openapi/*.yaml", "*.service-definition.json"]
      },
      "behavior": {
        "paths": ["openapi/*.yaml", "*.service-definition.json", "config/*.json"]
      },
      "consumers": ["checkout-web", "admin-portal"]
    }
  },
  "slices": {
    "checkout-contracts": {
      "description": "Contracts used by checkout",
      "mode": "boundary",
      "components": ["payment-api"]
    }
  }
}
```

Component `path` values are relative to the repository root. Boundary and behavior paths are relative to the component; they may be literal files or glob patterns. `**` matches recursively. A pattern that matches nothing stops generation instead of silently hashing an empty contract.

The four facets have different jobs:

| Facet | Tracks | Typical policy |
|---|---|---|
| `exact` | All tracked component content | Observe or gate release hygiene |
| `behavior` | Declared behavior-relevant artifacts | Gate runtime-contract-sensitive systems |
| `boundary` | Declared public contract artifacts | Gate consumer-facing changes |
| `compat` | The configured version family | Gate coordinated breaking changes |

`behavior.paths` should normally be a superset of `boundary.paths`. `consumers` declares direct downstream systems. When a producer's boundary or compatibility facet changes, boundver names those consumers in the verification result.

`defaults.verify_facets` controls what fails a plain `boundver verify`. Starting with `boundary` and `compat` prevents internal refactors from making the check noisy. A command-line `--facets` value overrides the default.

## 4. Validate before hashing

```bash
boundver validate-config
```

Fix every reported error. Validation checks the schema, component roots, providers, unsafe paths, slices, consumers, and source declarations.

## 5. Generate a local baseline

Use `working-tree` for both local generation and local verification:

```bash
boundver generate --source working-tree
boundver status
boundver verify --source working-tree --facets boundary,compat
```

Working-tree mode reads on-disk content but only for files known to Git. If you added a component or contract file, stage it before generating:

```bash
git add services/payment/openapi/new-route.yaml
boundver generate --source working-tree
```

Inspect the generated `boundary.lock.json`; it should contain non-null exact and declared boundary fingerprints. Generation fails if a required digest cannot be computed.

## 6. Commit the baseline

```bash
git add boundary.config.json boundary.lock.json
git commit -m "chore: add boundver contract baseline"
```

After the commit, a source-consistent HEAD check should pass:

```bash
boundver verify --source head --facets boundary,compat
```

Do not compare a lock generated from uncommitted working-tree content against `head`; the snapshots intentionally differ.

## 7. Add the pull-request gate

Create `.github/workflows/boundary-check.yml`:

```yaml
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

`head` is correct here because both the source and lockfile are committed in the pull request. The selected gate passes for an internal-only refactor and fails when a declared boundary or compatibility family drifts.

## Daily workflow

First inspect a local change against the matching snapshot:

```bash
boundver verify --source working-tree --facets boundary,compat
boundver why payment-api --source working-tree
```

If the gated change is intentional, review its consumer impact and update in one step:

```bash
boundver verify --source working-tree --facets boundary,compat --update
git diff -- boundary.lock.json
```

To refresh exact and behavior observations even when they are not part of your normal gate, explicitly include all facets:

```bash
boundver verify \
  --source working-tree \
  --facets exact,behavior,boundary,compat \
  --update
```

Commit the source change and updated lockfile together. After committing, verify them together with `--source head`.

## Understand exit codes

The highest-severity selected drift determines the process status:

| Code | Meaning |
|---:|---|
| `0` | Selected facets match |
| `1` | Exact or metadata drift |
| `2` | Usage or configuration error |
| `3` | Behavior drift |
| `4` | Boundary drift |
| `5` | Compatibility drift |

This lets CI warn or fail differently without parsing human-readable output. `--format json` also returns structured issues, non-gating observations, selected facets, and update status.

## Important limitation

boundver proves that the declared files produced the same fingerprints. It does not prove semantic compatibility, exercise consumers, or replace contract tests. The canonical JSON and OpenAPI providers reduce formatting and documentation noise, but you still decide which artifacts represent the real boundary and which consumer checks to run when it moves.

## Next steps

- Choose a provider in the [provider guide](public-vs-custom-providers.md).
- Add components gradually with the [adoption guide](gradual-adoption.md).
- Copy a pipeline pattern from the [CI cookbook](ci-cookbook.md).
- Explore runnable configurations in the [examples index](../examples/README.md).
- Resolve concurrent updates with the [lockfile merge strategy](LOCKFILE_MERGE.md).
