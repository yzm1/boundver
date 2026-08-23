# Getting started with boundver

This guide takes a Git repository from no configuration to a reviewed v3
lockfile and a useful pull-request gate.

> This guide describes boundver 0.13's v3/semantic-config-v2 contract. Version
> 0.11 writes v3/v1 locks and 0.10.x writes v2 locks; see
> [Upgrade to 0.12](#upgrade-to-012) before combining an existing lock with
> these instructions.

## Prerequisites

- Python 3.9 or newer
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
python -m pip install --upgrade "boundver[schema,yaml]==0.13.0"
python -c "import boundver; assert boundver.__version__ == '0.13.0', boundver.__version__"
```

Run persistent automation through `python -m boundver ...` with that same
interpreter so an older `boundver` executable on `PATH` cannot be selected.

## 2. Discover a starting point

Preview tracked manifests before writing anything:

```bash
boundver discover
boundver init --discover
```

Discovery recognizes Python, JavaScript/TypeScript, Rust, and Go manifests. It
uses Git-tracked paths rather than crawling ignored dependency or build trees.
It proposes a component root and boundary provider; it cannot decide which
artifacts truly form your contract, so review every result.

A repository-root manifest is not itself a safe component root because the
repository lockfile would become part of that component's exact fingerprint.
For a root manifest, discovery uses one unambiguous tracked Python package or a
conventional tracked `src`, `lib`, or `app` directory. The root manifest is
outside that component, so its version source is left unset for manual review.
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
  "$schema": "https://raw.githubusercontent.com/yzm1/boundver/v0.13.0/boundary.config.schema.json",
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
`.`. A complete `**` segment matches zero or more directories. Raw providers,
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

Do not add an executable generator command to repository config. Command trust,
tool versions, and source materialization are outside the current derivation
contract; first-class declarative support remains roadmap work.

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
      - uses: yzm1/boundver@v0.13.0
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
boundver verify --source working-tree
boundver why payment-api --source working-tree
boundver verify --source working-tree --update
git diff -- boundary.lock.json
```

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

## Source-mode checklist

| Source | Path set and content | Use |
|---|---|---|
| `head` | One captured commit tree | CI and committed verification |
| `index` | One captured index tree | Pre-commit verification |
| `working-tree` | Disk bytes for a captured tracked path set | Local editing |

- Generate and verify with the same source.
- Stage new files before `index`; make them Git-known before `working-tree`.
- `head` and `index` read the config and verification lock from the same
  captured source. Stage config/source changes before index generation, then
  stage the generated lock before index verification.
- Fetch history before using `--changed-from` or tag-based version sources.
- Treat `--changed-from` as reporting/scheduling information: boundver still
  recomputes full lock integrity so unchanged paths cannot hide stale metadata.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Selected facets match |
| `1` | Exact or metadata drift |
| `2` | Usage, configuration, or digest error |
| `3` | Behavior drift |
| `4` | Boundary drift |
| `5` | Compatibility-family drift |

The highest selected severity wins. `--fail-fast` limits the returned report,
not the safety evaluation. `--format json` exposes issues, observations,
selected facets, component-selection information, and update status.
`status --format json` remains available; 0.11 also adds structured output for
`why --format json` and `slice --format json`.

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
must define exactly one of `components` or `closure_of`. The selected mode must
be available for every member during strict generation; `exact` is the portable
choice for heterogeneous closures.

## Upgrade to 0.12

Version 0.11's v3 lock carries semantic-config/v1; 0.12 uses v2 so
presentation-only annotations no longer alter that identity. Version 0.10's
v2 lock also does not bind file mode/type or the complete semantic
configuration. There is no safe metadata-only migration for either source:

```bash
python -m pip install --upgrade "boundver[schema,yaml]==0.13.0"
boundver validate-config
# Stage changed config and every changed/newly selected contract input.
git add boundary.config.json services/payment/openapi/new-route.yaml
boundver generate --source index
git add boundary.lock.json
boundver verify --source index
git diff --cached -- boundary.config.json boundary.lock.json
```

“No metadata-only migration” means the lock must be recomputed, not that every
content fingerprint must rotate. When the selected source bytes and effective
selectors are unchanged, v3/v1 to v3/v2 regeneration and v0.12's provider
metadata bumps are expected to preserve component facet and slice digest
values. Review the changed semantic-config/provider metadata, and investigate
any facet or slice value that does change. A deliberate `json-file-raw` to
`path-hash` change has the same digest-neutral expectation under identical raw
paths and options, while provider/config metadata changes.

When upgrading directly from 0.10, review selector changes carefully: the
corrected `*`/`**` grammar may add or remove matches. Update every writer and
verifier together, then commit the regenerated v3/v2 lock. `boundver
migrate-lock` deliberately rejects v1/v2 hash-bearing locks and v3 locks with
semantic-config/v1, directing you to regenerate from content.
The source path is illustrative; stage every changed or newly selected input.
Omit `boundary.config.json` from `git add` if it did not change. Alternatively,
commit config/source changes first and only then generate from `head`.

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
