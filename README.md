# boundver — see contract drift before consumers do

[![CI](https://github.com/yzm1/boundver/actions/workflows/ci.yml/badge.svg)](https://github.com/yzm1/boundver/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/boundver)](https://pypi.org/project/boundver/)
[![GitHub Marketplace](https://img.shields.io/badge/Marketplace-boundver-blue?logo=github)](https://github.com/marketplace/actions/boundver)
[![Python 3.9+](https://img.shields.io/pypi/pyversions/boundver)](https://pypi.org/project/boundver/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](https://github.com/yzm1/boundver/blob/main/LICENSE)

**boundver is Git-aware contract-drift detection for polyglot repositories.**
It records four fingerprints per component—exact, behavior, boundary, and
compatibility—so CI can block consumer-facing changes without rejecting every
internal refactor.

> Version 0.12 uses `boundary-lock/v3` with
> `boundver-semantic-config/v2`. Version 0.11 writes v3/v1 locks and 0.10.x
> writes v2 locks; both require regeneration before using these instructions.
> See the [migration note](#upgrade-to-012).

## Try it in one minute

For a Git repository with tracked code below `src/`, run from the repository
root:

```bash
python -m pip install "boundver[schema,yaml]"
boundver init
boundver validate-config
boundver generate --source working-tree
boundver verify --source working-tree --facets exact
```

Review and commit `boundary.config.json` and `boundary.lock.json` together. If
you prefer a best-effort scaffold for a manifest-based repository, use
`boundver init --discover`; it exits without writing when no safe component
root can be inferred. Either way, review the component path and boundary
declaration before generation.
The initial scaffold uses an implicit boundary and no version source, so only
`exact` is available. Declare a real boundary and version source before gating
`boundary` or `compat`.

## What the four facets mean

| Facet | Question | Input |
|---|---|---|
| `exact` | Did any tracked component content or file identity change? | Every tracked file below the component path |
| `behavior` | Did declared observable behavior change? | Declared behavior paths, cryptographically bound to the boundary digest |
| `boundary` | Did a declared public artifact change? | Output of the configured boundary provider |
| `compat` | Did the configured compatibility family change? | Component version and compatibility mode |

A useful policy for a component that provides both signals is
`boundary,compat`: internal and behavior-only drift stays visible as
observations, while public-contract and compatibility drift fails the gate.
Use `exact` for an implicit, leaf, or unversioned component, or as the portable
CLI-wide gate over all tracked component bytes and file identities.

boundver detects drift in **declared artifacts**. It does not prove semantic or
backward compatibility, execute consumer tests, or infer every runtime behavior.
A clean result means the declared inputs and their recorded identities agree.

## A practical configuration

```json
{
  "$schema": "https://raw.githubusercontent.com/yzm1/boundver/v0.12.0/boundary.config.schema.json",
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
        "provider": "openapi-canonical",
        "paths": ["openapi/**/*.yaml"]
      },
      "behavior": {
        "paths": [
          "openapi/**/*.yaml",
          "config/**/*.json"
        ]
      },
      "consumers": ["checkout-web"],
      "external_consumers": ["external-risk-service"]
    },
    "checkout-web": {
      "path": "apps/checkout",
      "version_source": {"file": "package.json", "field": "version"},
      "boundary": {"provider": "leaf", "paths": []},
      "verify_facets": ["exact"]
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

Component paths are repository-relative. Boundary, behavior, and version-source
files are component-relative. Use POSIX `/` separators.

### Glob rules

All raw and canonical path-based providers, behavior selection, validation, and
explain output use the same case-sensitive grammar:

| Pattern | Matches |
|---|---|
| `*.yaml` | YAML files at the component root only |
| `api/*.yaml` | Direct children of `api` only |
| `**/*.yaml` | YAML files at the root or any depth |
| `api/**/*.yaml` | YAML files directly below `api` or deeper |

Within one segment, `*`, `?`, and character classes such as `[ab]` use
case-sensitive character matching; wildcards may match a leading `.`. A complete
`**` segment matches zero or more directories. Every
declared literal or pattern must select at least one file; otherwise strict
generation fails instead of hashing an empty contract.

### Consumers and behavior

`consumers` contains unique names of **direct downstream configured
components**. They are validated as graph edges, so a typo cannot silently
shrink impact. `external_consumers` contains unique opaque terminal labels for
systems outside this config. Boundary and compatibility drift reports direct
impact by default; add `--transitive` to `verify` or `why` to walk internal
edges and include external terminals found along that downstream closure.

A slice may use an explicit `components` array or
`"closure_of": "payment-api"`. The latter resolves to the seed plus all configured components
reachable through `consumers`, and stores the resolved membership in the lock.
The traversal is deterministic and cycle-safe. Exactly one membership form is
allowed. Choose a slice mode supplied by every resolved member (often `exact`
for a heterogeneous closure), or use `--allow-partial` only when null member
inputs are intentional.

`behavior.paths` should list the runtime-relevant contract files reviewers want
to see, normally including the boundary selectors plus configuration, defaults,
migrations, or contract tests. In v3 the behavior digest also includes the
boundary digest, so a configured behavior fingerprint cannot stay unchanged
when its boundary changes.

## Review and accept intentional drift

```bash
# Inspect the matching local snapshot.
boundver verify --source working-tree
boundver why payment-api --source working-tree

# After consumer-impact review, update and inspect the lock diff.
boundver verify --source working-tree --update
git diff -- boundary.lock.json
```

`--facets` decides which fingerprint mismatches fail and which are reported as
observations. It does **not** limit which fields are regenerated. `--update`
writes a coherent component entry containing all four facets and its metadata.
Without `--components`, it regenerates the complete lock. With
`--components payment-api`, it updates that component and recomputes all slices
after proving every unselected entry is already current; otherwise it refuses
the partial update.

When `--facets` is omitted, policy precedence is component
`verify_facets`, then `defaults.verify_facets`, then an implicit gate over all
facets that are available for that component. A CLI `--facets` value overrides
every component. Explicitly selecting an unavailable facet—for example
`compat` on a component with no `version_source`, or `boundary` on a `leaf`
component—is a usage error (exit `2`), not a successful null comparison.

`generate --allow-partial` is narrower: it permits an intentional null facet
to appear as a slice input. Missing declared files, provider failures, version
read errors, and vendored-copy failures remain fatal, so the option cannot
write a lock that fails verification merely because extraction failed.

## Generated boundary artifacts

boundver currently fingerprints a generated artifact but does not know which
source or generator produced it. Make freshness an explicit prerequisite:

```bash
python ci/generate_platform_openapi.py --check
boundver verify --source head
```

Run the generator itself before accepting intentional changes. For an index
baseline, stage the generator source and derived output together with any
config change, generate from that staged snapshot, then stage the lock:

```bash
python ci/generate_platform_openapi.py
git add ci/generate_platform_openapi.py platform/main.yaml \
  platform/infrastructure/openapi.yaml
# If changed, stage boundary.config.json in the same step.
boundver generate --source index
git add boundary.lock.json
boundver verify --source index
```

There is no executable `derived_from` config hook: repository config is not a
trusted command-execution boundary. A future declarative derivation contract is
roadmap work.

## Source modes

Use `head` for committed CI state, `index` for staged pre-commit state, and
`working-tree` while reviewing local edits. Generate and verify from the same
source. The normative snapshot and tracked-file rules live in the
[specification](spec/spec.md#source-modes); practical staging examples live in
the [CI cookbook](docs/ci-cookbook.md#match-source-mode-to-the-lifecycle).

## GitHub Actions

Pin the Action to the same lock-contract release used by local writers (for
this release, `yzm1/boundver@v0.12.0`). The
[CI cookbook](docs/ci-cookbook.md#github-actions-recommended-contract-gate) is
the canonical workflow recipe and covers outputs, changed-path reporting,
GitLab, pre-commit, and caching.

## Providers

| Provider | Selection and meaning |
|---|---|
| `path-hash` | Raw bytes for arbitrary declared artifacts |
| `openapi` / `openapi-raw` | Raw OpenAPI or Swagger bytes |
| `openapi-canonical` | Parsed OpenAPI contract with selected documentation noise removed |
| `json-file` / `json-file-raw` | Raw JSON contract bytes |
| `json-canonical` | Strictly parsed, deterministic JSON value |
| `python-exports` | Raw Python export files such as `__init__.py` |
| `typescript-exports` | Raw declarations or export barrels |
| `implicit` | Exact tracking before a boundary is declared |
| `leaf` | An intentional component with no published boundary |

Canonical means less formatting/documentation noise, not compatibility
analysis. Custom Python providers require explicit trusted-code opt-in; a
checked-out config cannot authorize imports. See
[public and custom providers](https://github.com/yzm1/boundver/blob/main/docs/public-vs-custom-providers.md).

## Exit codes

The stable `0` through `5` contract distinguishes clean verification, exact or
metadata drift, usage errors, and behavior/boundary/compatibility drift. See
[CI exit-code handling](docs/ci-cookbook.md#exit-code-aware-automation) for the
authoritative table and shell examples.

## Upgrade to 0.12

Version 0.11's `boundary-lock/v3` uses semantic-config/v1, whose digest meaning
differs from 0.12's semantic-config/v2. Version 0.10's `boundary-lock/v2` also
omits identities required by v3. Neither lock can be relabelled safely: upgrade
writers and verifiers together and regenerate from the snapshot CI will
verify. Follow the canonical
[0.12 upgrade procedure](docs/gradual-adoption.md#upgrading-to-012).

## Useful commands

```bash
boundver discover
boundver status --format json
boundver verify --changed-from origin/main --transitive
boundver verify --components payment-api --update
boundver diff old.lock.json boundary.lock.json
boundver why payment-api --transitive --format json
boundver slice checkout-contracts --format json
boundver completions --shell bash
```

## Installation and documentation

boundver requires Git and Python 3.9 or newer.

```bash
python -m pip install boundver
python -m pip install "boundver[schema,yaml]"  # recommended for strict JSON Schema and YAML
```

- [Getting started](https://github.com/yzm1/boundver/blob/main/docs/getting-started.md)
- [Examples](https://github.com/yzm1/boundver/blob/main/examples/README.md)
- [CI cookbook](https://github.com/yzm1/boundver/blob/main/docs/ci-cookbook.md)
- [Gradual adoption](https://github.com/yzm1/boundver/blob/main/docs/gradual-adoption.md)
- [Why boundver?](https://github.com/yzm1/boundver/blob/main/docs/WHY_BOUNDVER.md)
- [Lockfile merge strategy](https://github.com/yzm1/boundver/blob/main/docs/LOCKFILE_MERGE.md)
- [Maintainer release runbook](https://github.com/yzm1/boundver/blob/main/docs/RELEASING.md)
- [Changelog](https://github.com/yzm1/boundver/blob/main/CHANGELOG.md)
- [Support](https://github.com/yzm1/boundver/blob/main/SUPPORT.md), [contributing](https://github.com/yzm1/boundver/blob/main/CONTRIBUTING.md), and [security](https://github.com/yzm1/boundver/blob/main/SECURITY.md)

MIT licensed.
