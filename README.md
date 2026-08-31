# boundver

<p align="center">
  <a href="https://yzm1.github.io/boundver/">
    <img src="https://yzm1.github.io/boundver/assets/logo.png" alt="boundver logo" width="128">
  </a>
</p>

> Know which contracts changed — and which consumers to verify.

[![CI](https://github.com/yzm1/boundver/actions/workflows/ci.yml/badge.svg)](https://github.com/yzm1/boundver/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-4051b5)](https://yzm1.github.io/boundver/)
[![PyPI](https://img.shields.io/pypi/v/boundver)](https://pypi.org/project/boundver/)
[![GitHub Marketplace](https://img.shields.io/badge/Marketplace-boundver-blue?logo=github)](https://github.com/marketplace/actions/boundver)
[![GitLab Catalog](https://img.shields.io/badge/GitLab%20Catalog-boundver-FC6D26?logo=gitlab)](https://gitlab.com/boundver-project/boundver)
[![Python 3.9+](https://img.shields.io/pypi/pyversions/boundver)](https://pypi.org/project/boundver/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](https://github.com/yzm1/boundver/blob/main/LICENSE)

**boundver classifies declared contract drift and downstream impact across
polyglot repositories.** It records four identities per component—exact,
behavior, boundary, and compatibility family—then tells CI what changed and
which declared consumers may need re-verification.

Use it when APIs, schemas, generated contracts, configuration, or package
surfaces cross language and build-system boundaries. Internal refactors stay
visible without being confused with public-contract changes.

Compilers, schema checkers, affected-build graphs, tests, and release tools each
answer one part of that workflow. Boundver supplies the portable, Git-backed
evidence between them: which declared contract family drifted and which direct
or transitive consumers should be re-verified. It complements those tools
rather than replacing them; see the [comparison and integration
guide](https://yzm1.github.io/boundver/comparison/).

[![A boundver verification shows boundary drift and affected consumers](https://yzm1.github.io/boundver/assets/verify-demo.svg)](https://yzm1.github.io/boundver/demo/)

The install and Action examples below target the exact v0.14.1 release so local
writers and CI verifiers use the same contract implementation.

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
In an unborn repository with an empty index, discovery asks Git for its
non-ignored bootstrap files; a non-Git directory uses a bounded approximation
and prints a warning.
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
Use `exact` for a pathless implicit component, a leaf without another declared
signal, or an unversioned component, and as the portable CLI-wide gate over all
tracked component bytes and file identities. `leaf` suppresses only the
`boundary` fingerprint: a leaf can still provide `behavior` from
`behavior.paths` and `compat` from a `version_source`.

boundver detects drift in **declared artifacts**. It does not prove semantic or
backward compatibility, execute consumer tests, or infer every runtime behavior.
A clean result means the declared inputs and their recorded identities agree.

## Where boundver fits

| Need | Use | Relationship to boundver |
|---|---|---|
| Determine affected projects and tasks from a build graph | Nx, Pants, Bazel | These tools discover build impact; boundver classifies declared contract drift across build systems. |
| Prove schema-specific compatibility | oasdiff, Buf, GraphQL Inspector | Run these semantic checkers alongside boundver for the formats they understand. |
| Plan package versions and release notes | Changesets, semantic-release | These automate releases; boundver supplies review and CI signals before promotion. |
| Detect contract-family drift and route downstream verification | boundver | Language-neutral, Git-aware fingerprints plus an explicit consumer graph. |

See the full [comparison and integration guide](https://yzm1.github.io/boundver/comparison/).

## A practical configuration

```json
{
  "$schema": "https://raw.githubusercontent.com/yzm1/boundver/v0.14.1/boundary.config.schema.json",
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

In v0.13, component `ecosystem`, component `note`, and boundary `note` are
presentation-only: use them for classification and review rationale, not hidden
contract selection, and editing them does not rotate `config_digest`.

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
`**` segment matches zero or more directories. A wildcard-bearing segment is
limited to 4,096 UTF-8 bytes and 256 wildcard metacharacters; matching is
budgeted and fails closed if the work limit is exceeded. Every
declared literal or pattern must select at least one file; otherwise strict
generation fails instead of hashing an empty contract.

### Consumers and behavior

`consumers` contains unique names of **direct downstream configured
components**. They are validated as graph edges, so a typo cannot silently
shrink impact. `external_consumers` contains unique opaque terminal labels for
systems outside this config. Boundary and compatibility drift reports direct
impact by default; add `--transitive` to `verify` or `why` to walk internal
edges and include external terminals found along that downstream closure.
Configured component names cannot contain commas or leading/trailing
whitespace: the CLI, GitHub Action, and GitLab Catalog use comma-separated
component filters and trim each token. Existing configs with such a name must
rename it and update its consumer edges, slice references, and lockfile.
The machine-output bounds are documented in the
[reference](docs/reference.md#consumer-graph-limits); validation rejects an
oversized graph rather than emitting a partial impact closure.

A slice may use an explicit `components` array or
`"closure_of": "payment-api"`. The latter resolves to the seed plus all configured components
reachable through `consumers`, and stores the resolved membership in the lock.
The traversal is deterministic and cycle-safe. Exactly one membership form is
allowed, and an explicit `components` array must contain at least one configured
component; an empty slice would be a permanent fingerprint over nothing. Choose
a slice mode supplied by every resolved member (often `exact` for a
heterogeneous closure), or use `--allow-partial` only when null member inputs
are intentional.

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
Add the missing facet input or select only facets the component supplies;
`--update` cannot manufacture an unavailable facet and leaves the lock
unchanged.

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
trusted command-execution boundary. Run the generator's deterministic check
before verifying; see
[docs/reference.md](docs/reference.md#generated-artifacts-are-not-bound-to-their-generator).

## Source modes

The default source is `head`: committed state, not unstaged local edits. Use
`head` for committed CI state, `index` for staged pre-commit state, and
`working-tree` while reviewing local edits. Generate and verify from the same
source. The normative snapshot and tracked-file rules live in the
[specification](spec/spec.md#source-modes); practical staging examples live in
the [CI cookbook](docs/ci-cookbook.md#match-source-mode-to-the-lifecycle).

## GitHub Actions

Pin the Action to the same lock-contract release used by local writers (for
the latest stable release, `yzm1/boundver@v0.14.1`). The
[CI cookbook](docs/ci-cookbook.md#github-actions-recommended-contract-gate) is
the canonical workflow recipe and covers outputs, changed-path reporting,
GitLab, pre-commit, and caching.

## Providers

| Provider | Selection and meaning |
|---|---|
| `path-hash` | Format-neutral raw bytes from any declared artifact, such as SQL or protobuf files |
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
[upgrade procedure](docs/reference.md#upgrading).

Regeneration is still mandatory even when its content fingerprints are
digest-neutral. With the same source bytes and effective selectors, a v3/v1 to
v3/v2 regeneration and v0.12's built-in provider-metadata updates are expected
to retain component facet and slice digest values; the semantic-config and
provider metadata still change. Investigate any facet/slice value change rather
than treating it as metadata churn. The equivalent raw-provider case for
`json-file-raw` and `path-hash` is documented in the
[provider guide](docs/public-vs-custom-providers.md#provider-versions-and-v3-locks).
The v0.13 `diff` command can compare canonical `boundary-lock/v3`
semantic-config/v1 and v2 locks read-only so this regeneration remains
reviewable. Full generation recomputes and emits v2 without trusting the old
lock; verification and generation paths that reuse an existing lock reject v1.

## Useful commands

The inspection commands include discovery/config comparison, migration
analysis, verification baselines, and repeatable discovery exclusions.

```bash
boundver discover
boundver discover --diff-config --exclude legacy/vendor
boundver status --format json
boundver verify --changed-from origin/main --transitive
boundver verify --components payment-api --update
boundver diff old.lock.json boundary.lock.json
boundver migrate-lock --explain --source head --format json
boundver verify --write-baseline .boundver-verify-baseline.json
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

For a reused developer environment, system pre-commit hook, or prebuilt
container, install the repository's exact pin with `--upgrade` and assert the
imported version before generating or verifying a lock:

```bash
python -m pip install --upgrade "boundver[schema,yaml]==0.14.1"
python -c "import boundver; assert boundver.__version__ == '0.14.1', boundver.__version__"
```

In persistent automation, invoke commands as `python -m boundver ...` with that
same interpreter so an older executable elsewhere on `PATH` cannot take over.

You can also use the release container, Homebrew tap, or GitLab CI/CD component:

```bash
docker run --rm -v "$PWD:/repo:ro" -w /repo \
  ghcr.io/yzm1/boundver:<version> verify --source head
brew install yzm1/boundver/boundver
```

Exact-version and digest-pinned examples are in the
[distribution guide](https://yzm1.github.io/boundver/distribution/).

- [Documentation](https://yzm1.github.io/boundver/)
- [Getting started](https://yzm1.github.io/boundver/getting-started/)
- [Runnable demo](https://yzm1.github.io/boundver/demo/)
- [Examples](https://yzm1.github.io/boundver/examples/)
- [CI cookbook](https://yzm1.github.io/boundver/ci-cookbook/)
- [Gradual adoption](https://yzm1.github.io/boundver/gradual-adoption/)
- [Migration inspection and verification ratchets](https://yzm1.github.io/boundver/migration-and-ratcheting/)
- [Why boundver?](https://yzm1.github.io/boundver/WHY_BOUNDVER/)
- [Lockfile merge strategy](https://yzm1.github.io/boundver/LOCKFILE_MERGE/)
- [Maintainer release runbook](https://github.com/yzm1/boundver/blob/main/docs/RELEASING.md)
- [Changelog](https://github.com/yzm1/boundver/blob/main/CHANGELOG.md)
- [Support](https://github.com/yzm1/boundver/blob/main/SUPPORT.md), [contributing](https://github.com/yzm1/boundver/blob/main/CONTRIBUTING.md), and [security](https://github.com/yzm1/boundver/blob/main/SECURITY.md)

MIT licensed.
