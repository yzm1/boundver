# Getting started with boundver

This guide takes a Git repository from no configuration to a reviewed v3
lockfile and a useful pull-request gate.

> This guide describes the current v3/semantic-config-v2 contract used by
> boundver 0.13 and newer. Version 0.11 writes v3/v1 locks and 0.10.x writes
> v2 locks; see
> [Upgrading](reference.md#upgrading) before combining an existing lock with
> these instructions.

## Prerequisites

- Python 3.10 or newer
- Git
- At least one component in a non-root directory

Run the commands below from the repository root.

## 1. Install

```bash
python -m pip install "boundver[schema,yaml]"
boundver --version
```

The base install can validate JSON configuration without third-party packages.
The extras add full JSON Schema validation and YAML parsing.

If this is a reused developer environment rather than a disposable virtual
environment, replace the install above with an exact upgraded pin and assert
what Python imports before writing a lock:

```bash
python -m pip install --upgrade "boundver[schema,yaml]==0.14.1"
python -c "import boundver; assert boundver.__version__ == '0.14.1', boundver.__version__"
```

Run persistent automation through `python -m boundver ...` with that same
interpreter so an older `boundver` executable on `PATH` cannot be selected.

## 2. Discover a starting point

Preview the Git-selected manifest corpus before writing anything:

```bash
boundver discover
boundver init --discover
```

Discovery recognizes Python, JavaScript/TypeScript, Rust, and Go manifests. In
an established repository it uses indexed paths rather than crawling ignored
dependency or build trees. Before the first commit, when the index is still
empty, it asks Git for non-ignored bootstrap files; root and nested ignore
files, negation, global excludes, and embedded repositories therefore retain
Git's installed-version semantics. A directory that is not a Git repository
uses a bounded filesystem approximation and prints a warning that ignore
semantics may differ. If a `.git` marker exists but Git cannot read the
repository, discovery fails closed instead of treating repository metadata as
ordinary files.
Use repeatable repository-relative `--exclude PATH` prefixes when tracked
legacy, fixture, or vendored manifests are intentionally outside the component
corpus:

```bash
boundver discover --exclude legacy --exclude test/fixtures
```

It proposes a component root and boundary provider; it cannot decide which
artifacts truly form your contract, so review every result.

A repository-root manifest is not itself a safe component root because the
repository lockfile would become part of that component's exact fingerprint.
For a root manifest, discovery uses one unambiguous Git-selected Python package
or a conventional selected `src`, `lib`, or `app` directory. The root manifest
is outside that component, so its version source is left unset for manual review.
If no safe directory can be inferred, `init --discover` exits without writing
an invalid config.

Use the manual scaffold in that case:

```bash
boundver init
```

Replace the placeholder component path before validating.

## 3. Declare contracts and consumer edges

```json
{
  "$schema": "https://raw.githubusercontent.com/yzm1/boundver/v0.14.1/boundary.config.schema.json",
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
        "provider": "openapi-canonical",
        "paths": ["openapi/**/*.yaml"]
      },
      "behavior": {
        "paths": ["openapi/**/*.yaml", "config/**/*.json"]
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
      "description": "Contracts used by checkout",
      "mode": "boundary",
      "components": ["payment-api"]
    }
  }
}
```

Component `path` values are relative to the repository. Boundary, behavior,
vendored-copy, and file version-source paths use `/` separators and are relative
to the scope documented by the schema. Empty, absolute, traversing, and
backslash-separated declarations are rejected.

v0.13 treats component `ecosystem`, component `note`, and `boundary.note` as
presentation-only, so they can record classification, ownership, migration,
and review rationale without rotating `config_digest`; do not use them to hide
contract selection or policy.

Path selectors are case-sensitive:

- `*.yaml` matches only component-root YAML files.
- `api/*.yaml` matches direct children of `api`, not deeper descendants.
- `**/*.yaml` matches root and nested YAML files.
- `api/**/*.yaml` matches direct and deeper YAML files below `api`.

`*`, `?`, and character classes stay within one segment and may match a leading
`.`. A complete `**` segment matches zero or more directories. A
wildcard-bearing segment is limited to 4,096 UTF-8 bytes and 256 wildcard
metacharacters. One match is limited to 100,000 compile/match steps, and each
provider selection, component validation expansion, or change-analysis
operation has a 10,000,000-step aggregate budget across every pattern and
candidate. Patterns compile once per operation. Exceeding either limit fails
closed with guidance to reduce wildcard declarations or split the component.
See [selector work limits](reference.md#selector-work-limits) for the normative
contract.
Raw providers,
canonical providers, behavior paths, validation, and explain output share this
grammar. Every
declaration must match at least one selected file during strict generation.

The four facets serve different review decisions:

| Facet | Tracks | Typical policy |
|---|---|---|
| `exact` | All tracked content and file identities under the component | Observe release hygiene |
| `behavior` | Declared behavior inputs plus the boundary digest | Gate observable runtime contracts |
| `boundary` | Declared provider output | Gate consumer-facing artifacts |
| `compat` | The configured version family | Gate coordinated compatibility changes |

For a component with behavior tracking, the v3 behavior digest includes its
boundary digest. Keep the boundary patterns in `behavior.paths` as well so
diagnostics show the intended containment and so the additional behavior input
set remains understandable.

`consumers` contains unique configured component names for immediate downstream
edges. Unknown names are rejected. Use `external_consumers` for unique opaque
terminal labels outside this config. Boundary and compatibility drift reports
the direct names by default; `verify --transitive` and `why --transitive` walk
the internal graph and include external terminals declared along the closure.

The effective facet gate follows `--facets` (when supplied), then a component's
`verify_facets`, then `defaults.verify_facets`. With none of those configured,
boundver gates all facets available for each component. Explicitly selecting a
facet that cannot exist is a usage error (exit `2`): `compat` needs a
`version_source`, `behavior` needs behavior inputs, `leaf` never provides a
boundary digest, and `implicit` provides one only when it declares paths.
Per-component policy is therefore the right way to combine heterogeneous
component types in one config.

## 4. Validate before hashing

```bash
boundver validate-config
```

Fix every error and review every warning. Validation rejects unknown fields even
without the optional schema engine, checks path safety and component roots, and
validates providers, versions, consumers, vendored paths, and slices. It
resolves closure slices and rejects a strict slice when any resolved component
cannot supply its selected facet, before digest generation starts. The installed
package's bundled schema is authoritative; a checkout cannot replace it with a
same-named local file.

## 5. Generate a local baseline

```bash
boundver generate --source working-tree
boundver status --source working-tree
boundver verify --source working-tree
```

In an established repository, working-tree mode reads current on-disk bytes only
for paths known to Git. Add a new contract file to Git before generating so a
glob can see it:

```bash
git add services/payment/openapi/new-route.yaml
boundver generate --source working-tree
```

Strict generation fails if a declared digest, version input, or vendored-copy
comparison cannot be computed. Inspect the generated lock; it should use
`boundary-lock/v3` and contain `config_contract` and `config_digest`.

`--allow-partial` does not suppress those computation errors. It only permits
an intentionally unavailable component facet to be stored as a null input in a
slice. A declared path that selects nothing, a provider failure, a broken
version source, or a missing/divergent vendored copy remains fatal. That command
uses partial-compatible validation deliberately. The standalone
`validate-config` command checks the normal strict-generation contract unless
you explicitly give it the matching `--allow-partial` flag.

### Generated contracts need their own freshness check

If the selected OpenAPI document is generated from code or infrastructure,
boundver sees the output but cannot prove that it is current. Put the generator's
check before boundver in every gate:

```bash
python ci/generate_platform_openapi.py --check
boundver verify --source working-tree
```

Do not add an executable generator command to repository config: command trust,
tool versions, and source materialization are outside the current derivation
contract. See
[reference](reference.md#generated-artifacts-are-not-bound-to-their-generator).

## 6. Commit one source-consistent baseline

```bash
git add boundary.config.json boundary.lock.json
git commit -m "chore: record boundver contract baseline"
boundver verify --source head
```

Commit any source or contract files represented by the lock in the same commit.
A lock generated from uncommitted working-tree bytes will not match `head` until
those bytes are committed.

## 7. Add the pull-request gate

```yaml
# .github/workflows/boundary-check.yml
name: Contract boundary
on: [pull_request]

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 0
      # Pin the writer and verifier to the lock contract used by the repository.
      - uses: yzm1/boundver@v0.14.1
        with:
          config: boundary.config.json
          lock: boundary.lock.json
          source: head
```

`head` captures the committed pull-request tree once. With `facets` omitted,
the Action honors the component/default policy in the config. In this example,
consumer-facing drift gates `payment-api` while every tracked `checkout-web`
change gates through its exact-only override.

## Daily review and update

```bash
boundver review origin/main..HEAD --merge-base --transitive
boundver verify --source working-tree
boundver why payment-api --source working-tree
boundver verify --source working-tree --update
git diff -- boundary.lock.json
```

The first command answers the branch-history question from reconciled lock
state at both committed endpoints. It is a query and returns `0` for a complete
analysis even when facets moved. The following `verify` command remains the
integrity gate for the source snapshot you are about to accept. See
[historical range review](reference.md#historical-range-review) for endpoint,
merge-base, and shallow-history rules.

Facets select the gate and report classification. They are not an update mask:
an update replaces the complete entry, including all fingerprints and metadata.
With no component filter, boundver regenerates the full lock. A component-scoped
update such as this:

```bash
boundver verify \
  --source working-tree \
  --components payment-api \
  --facets boundary,compat \
  --update
```

recomputes the whole candidate lock first, refuses the update if an unselected
component is stale, then replaces the selected entry and recomputes all slices.
This prevents a focused command from silently blessing unrelated drift.

## Source modes and exit codes

You have now used all three sources: `working-tree` for the local baseline and
`head` for the committed gate. `index` is the third, for pre-commit checks.

The rule that matters most is that you generate and verify with the *same*
source, and that `head` and `index` read the config and the lock from the
captured snapshot too — so a staged workflow must stage the lock before it
verifies.

For the complete source-mode table, the exit-code table, and an exit-code-aware
CI script, see [reference](reference.md).

## Consumer closures and slices

Use an explicit slice when membership is curated independently of the graph:

```json
{"mode": "boundary", "components": ["payment-api"]}
```

Use `closure_of` when the desired membership is the seed and its complete
downstream configured-component closure:

```json
{"mode": "exact", "closure_of": "payment-api"}
```

The resolved, sorted, cycle-safe component set is stored in the lock. A slice
must define exactly one of `components` or `closure_of`, and an explicit
`components` array must name at least one configured component. Empty slices
are rejected because their stable fingerprint would observe no repository
change. The selected mode must be available for every member during strict
generation; `exact` is the portable choice for heterogeneous closures.

## Upgrading

Locks are regenerated, never relabelled. The upgrade procedure and the
`migrate-lock` rejection rules are in [reference](reference.md#upgrading);
version-specific expectations for what the regeneration diff should show are in
[migration and ratcheting](migration-and-ratcheting.md).

## Important limitation

boundver proves that declared inputs produce recorded fingerprints. Canonical
providers can reduce formatting or documentation noise, but they do not prove
backward compatibility or replace consumer, schema-evolution, or integration
tests.

## Next steps

- [Choose a provider](public-vs-custom-providers.md).
- [Adopt one component at a time](gradual-adoption.md).
- [Copy a CI recipe](ci-cookbook.md).
- [Explore example configurations](examples.md).
- [Resolve concurrent lock updates](LOCKFILE_MERGE.md).
