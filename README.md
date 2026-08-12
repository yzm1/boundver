# boundver — know whether a change is internal, behavioral, API-facing, or breaking

[![CI](https://github.com/yzm1/boundver/actions/workflows/ci.yml/badge.svg)](https://github.com/yzm1/boundver/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/boundver)](https://pypi.org/project/boundver/)
[![Python 3.9+](https://img.shields.io/pypi/pyversions/boundver)](https://pypi.org/project/boundver/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](https://github.com/yzm1/boundver/blob/main/LICENSE)

**boundver is Git-aware API contract and breaking-change detection for polyglot repositories.** It classifies component drift into exact, behavior, boundary, and compatibility facets, so CI can block consumer-impacting changes without rejecting every internal refactor.

## Try it in one minute

Run these commands from your repository root:

```bash
python -m pip install boundver
boundver init --discover
boundver validate-config
boundver generate --source working-tree
boundver verify --source working-tree --facets boundary,compat
```

Review and commit `boundary.config.json` and `boundary.lock.json`. In CI, verify the committed snapshot with `source: head`.

No useful manifests discovered? `boundver init` creates a minimal scaffold you can edit.

## Why teams use it

A service, package, schema, or config-driven component often has consumers that no compiler can verify. A generic “files changed” check is too noisy; a handwritten list of affected systems is easy to forget. boundver records deterministic fingerprints for the parts that matter and reports the direct consumers of a changed contract.

- Gate only on the risk you care about: `boundver verify --facets boundary,compat`.
- Match contract families with globs such as `*.service-definition.json`; newly added matching files cannot stay invisible.
- Declare `consumers` beside each producer to expose the immediate blast radius.
- Refresh an intentional change with `boundver verify --update` after review.
- Use severity-specific exit codes without parsing console text.
- Keep polyglot monorepos on one small, Git-based contract.

## The four facets

| Facet | Question | Input |
|---|---|---|
| `exact` | Did any tracked component content change? | Every tracked file below the component path |
| `behavior` | Did declared observable behavior change? | Boundary files plus config, migrations, contract tests, or other declared paths |
| `boundary` | Did a declared API or contract artifact change? | Provider output for `boundary.paths` |
| `compat` | Did the compatibility family change? | The configured version and compatibility mode |

The facets let an internal edit remain visible without making it a merge blocker. For example, a team can record exact drift while requiring only boundary and compatibility stability in CI.

> boundver detects drift in **declared artifacts**. It is not proof that two implementations are semantically compatible, and it does not replace consumer tests. Canonical providers can remove non-contract noise, but a passing fingerprint check means the declared inputs stayed stable—not that every runtime behavior is equivalent.

## A practical configuration

```json
{
  "$schema": "https://raw.githubusercontent.com/yzm1/boundver/main/boundary.config.schema.json",
  "project": "payments-platform",
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
      "consumers": ["admin-portal", "checkout-web"]
    }
  },
  "slices": {
    "checkout-contracts": {
      "description": "Contracts required by checkout",
      "mode": "boundary",
      "components": ["payment-api"]
    }
  }
}
```

Paths are relative to the component. `*`, `?`, character classes, and recursive `**` patterns are supported by path-hashing providers. A glob that matches nothing is an error, making accidental omissions visible. Canonical JSON/OpenAPI providers currently require explicit files.

`behavior.paths` normally includes every boundary path plus runtime-relevant configuration. `consumers` names direct downstream systems; boundary and compatibility failures report them so reviewers know whom to re-verify.

## Choose the CI policy

The command line overrides `defaults.verify_facets`:

```bash
# Recommended starting gate: internal refactors do not fail CI
boundver verify --facets boundary,compat

# Stricter contract gate
boundver verify --facets behavior,boundary,compat

# Audit every recorded facet
boundver verify --facets exact,behavior,boundary,compat
```

Drift outside the selected gate is reported as a non-gating observation. Config, lockfile structure, digest errors, and metadata integrity remain safety checks.

## GitHub Actions

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

The Action installs the version bundled with its release and exposes issues, observations, and the exit code. See the [CI cookbook](https://github.com/yzm1/boundver/blob/main/docs/ci-cookbook.md) for changed-component checks, GitLab, pre-commit, and cache recipes.

## Review and accept intentional drift

```bash
# See the affected facet and direct consumers
boundver verify --source working-tree --facets boundary,compat
boundver why payment-api --source working-tree

# After review, regenerate in the same source mode
boundver verify --source working-tree --facets boundary,compat --update
git diff -- boundary.lock.json
```

Use the same source mode for generation and verification. If you want to refresh non-gating exact or behavior drift too, include those facets in the update command.

## Source modes

| Mode | Snapshot | Typical use |
|---|---|---|
| `head` | Files committed at `HEAD` | CI and clean local checkouts |
| `index` | Files staged in Git | Pre-commit workflows |
| `working-tree` | On-disk content of tracked files | Reviewing local edits |

Untracked files are intentionally excluded after the repository has its first commit. In an unborn repository, `working-tree` uses a bounded filesystem fallback so initial setup can succeed; review and stage those files before committing the lock. Stage a new contract file before using `index`, or `git add` it before relying on tracked working-tree discovery.

## Providers

| Provider | Use it for |
|---|---|
| `openapi` | Raw OpenAPI or Swagger artifacts |
| `openapi-canonical` | OpenAPI structure with documentation noise removed |
| `json-file` | Raw JSON contracts |
| `json-canonical` | Formatting-insensitive JSON contracts |
| `python-exports` | Python export files such as `__init__.py` |
| `typescript-exports` | TypeScript declarations or export barrels |
| `leaf` | A component with no downstream contract |
| `implicit` | Exact tracking before a boundary is declared |

Custom providers are supported only with an explicit trusted-code opt-in. See [public and custom providers](https://github.com/yzm1/boundver/blob/main/docs/public-vs-custom-providers.md).

## Exit codes

| Code | Highest selected failure |
|---:|---|
| `0` | Clean; selected facets match |
| `1` | Exact or metadata drift |
| `2` | Usage or configuration error |
| `3` | Behavior drift |
| `4` | Boundary drift |
| `5` | Compatibility drift |

When several selected facets drift, the highest-severity code wins.

## Useful commands

```bash
boundver discover                    # preview Git-tracked manifest discovery
boundver status                      # summarize a lockfile
boundver verify --changed-from main  # show changed components; verify the full lock
boundver verify --update             # accept reviewed drift in one step
boundver diff old.lock.json boundary.lock.json
boundver slice checkout-contracts
boundver completions --shell bash
```

## Installation and requirements

boundver supports Python 3.9+ and Git. It has no third-party dependency on Python 3.11+; Python 3.9–3.10 install `tomli` for TOML support.

```bash
python -m pip install boundver
python -m pip install "boundver[schema,yaml]"  # optional validation/YAML support
```

## Learn more

- [Getting started](https://github.com/yzm1/boundver/blob/main/docs/getting-started.md)
- [Examples](https://github.com/yzm1/boundver/blob/main/examples/README.md)
- [CI cookbook](https://github.com/yzm1/boundver/blob/main/docs/ci-cookbook.md)
- [Gradual adoption](https://github.com/yzm1/boundver/blob/main/docs/gradual-adoption.md)
- [Why boundver?](https://github.com/yzm1/boundver/blob/main/docs/WHY_BOUNDVER.md)
- [Lockfile merge strategy](https://github.com/yzm1/boundver/blob/main/docs/LOCKFILE_MERGE.md)
- [Changelog](https://github.com/yzm1/boundver/blob/main/CHANGELOG.md)

For questions and ideas, see [support](https://github.com/yzm1/boundver/blob/main/SUPPORT.md); bugs belong in [GitHub Issues](https://github.com/yzm1/boundver/issues). Contributions are welcome—start with the [contributing guide](https://github.com/yzm1/boundver/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/yzm1/boundver/blob/main/SECURITY.md).

MIT licensed.
