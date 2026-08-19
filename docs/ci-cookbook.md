# CI cookbook

These recipes make the source snapshot, lock schema, and gate policy explicit.
They describe boundver 0.12's v3/semantic-config-v2 contract. Boundver 0.11
writes v3/v1 locks and 0.10.x writes v2 locks; both require regeneration and
must not be mixed with these writers.

## GitHub Actions: recommended contract gate

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

      # Keep the writer and verifier on the repository's lock-contract version.
      - uses: yzm1/boundver@v0.12.0
        with:
          config: boundary.config.json
          lock: boundary.lock.json
          source: head
```

`head` is the committed pull-request tree. The tagged Action installs the
boundver source bundled with that release, including schema and YAML extras, and
returns `exit-code`, `issues`, and `observations` outputs.

The Action inputs are:

- `config` and `lock`: paths relative to the checkout root.
- `source`: `head`, `index`, or `working-tree`.
- `facets`: optional comma-separated CLI-wide override. Leave it blank to honor
  component/default policy.
- `components`: optional comma-separated component names.
- `changed-from`: optional Git ref for changed-path reporting.
- `transitive`: set to `true` to include the downstream consumer closure in
  impact output.
- `fail-fast`: report only the highest-severity issue after safe evaluation.
- `update`: regenerate after successful computation; normally leave this false
  in a pull-request gate.
- `python-version`: Action runtime, defaulting to 3.12.

## Pick a signal-to-noise policy

| Policy | Facets | What fails |
|---|---|---|
| Consumer-facing | `boundary,compat` | Declared public artifact or compatibility-family drift; requires both facets on every selected component |
| Behavior-sensitive | `behavior,boundary,compat` | Observable behavior, boundary, or compatibility drift; requires all three |
| Portable tracked-source hygiene | `exact` | Every tracked component byte, path, and file identity; works for leaf and unversioned components |

Policy can live in configuration:

```json
{
  "defaults": {
    "verify_facets": ["boundary", "compat"]
  }
}
```

The Action leaves the override blank by default. An explicit `--facets` value
overrides every component; otherwise a component's
`verify_facets` overrides `defaults.verify_facets`. With no configured policy,
the implicit default gates all facets available for each component. Fingerprint
drift outside the effective gate is returned as an observation.

An explicitly selected but unavailable facet is a usage error (exit `2`), not a
clean null comparison. For example, `compat` requires a `version_source`, and a
`leaf` or `implicit` component has no boundary digest. Give heterogeneous
components their own policies instead of choosing one loose global policy:

```json
{
  "project": "payments-platform",
  "defaults": {"verify_facets": ["boundary", "compat"]},
  "components": {
    "public-api": {
      "path": "services/api",
      "version_source": {"file": "package.json", "field": "version"},
      "boundary": {"provider": "openapi", "paths": ["openapi.yaml"]}
    },
    "website": {
      "path": "apps/website",
      "version_source": null,
      "boundary": {"provider": "leaf", "paths": []},
      "verify_facets": ["exact"]
    }
  },
  "slices": {}
}
```

Structural, semantic-config, provider-metadata, and digest errors remain
failures because the comparison is otherwise unreliable.

Facets classify reporting and exit policy; they do not select fields to hash or
write. Every computed component entry contains all four facets.
Exact-only is the portable strict source gate because every component has an
exact fingerprint. It does not turn canonicalization into compatibility
analysis or create a compatibility identity for an unversioned component.

## Changed-path reporting without an integrity shortcut

```yaml
- uses: actions/checkout@v6
  with:
    fetch-depth: 0

- uses: yzm1/boundver@v0.12.0
  with:
    source: head
    changed-from: origin/${{ github.base_ref }}
```

`--changed-from` reports the components and slices mapped from Git-tracked path
changes. It still recomputes the full lock for integrity, including provider
versions, semantic configuration, tag-derived versions, and stored metadata.
An invalid ref is an input error. A config-file change selects all components
for reporting because it can redefine contracts without touching their source
directories.

Use `components` only when the repository intentionally owns a narrower check:

```yaml
with:
  source: head
  components: payment-api,billing-api
```

The affected slices are checked as well. A component filter narrows ordinary
verification, so use an unfiltered gate somewhere in the repository unless the
remaining components have an independent owner.

## Pin a package instead of the Action

Pin the writer/verifier version that matches the committed lock schema:

```yaml
steps:
  - uses: actions/checkout@v6
    with:
      fetch-depth: 0
  - uses: actions/setup-python@v6
    with:
      python-version: "3.12"
  - run: python -m pip install "boundver[schema,yaml]==0.12.0"
  - run: boundver verify --source head
```

Use this form for a PyPI mirror or centrally managed Python environment. Do not
let one job write an old v2 or v3/semantic-config-v1 lock while another
verifies the current v3/semantic-config-v2 contract.

## Match source mode to the lifecycle

| Lifecycle | Source | Reason |
|---|---|---|
| Pull request / post-commit CI | `head` | Captures one immutable commit tree |
| Pre-commit | `index` | Captures exactly the staged tree |
| Local review before staging | `working-tree` | Reads disk bytes for Git-known paths |

After the first commit, untracked files are excluded. Stage new files before
`index`; make them Git-known before relying on working-tree glob expansion. Use
the same mode for generation and the verification it is meant to satisfy.

Configuration and the verification lock are source-bound too: `head` reads
both from the captured commit, and `index` reads both from the captured staged
tree. This is a breaking correction from 0.10, which could combine staged
artifacts with unstaged config/lock content. A complete staged refresh is:

```bash
# Stage every changed input; include boundary.config.json only when changed.
git add services/payment/main.yaml services/payment/openapi.generated.yaml
boundver generate --source index
git add boundary.lock.json
boundver verify --source index
```

## Check generated artifacts before verifying them

boundver hashes a generated contract but does not currently bind it to its
source or generator. Run a deterministic freshness check first:

```yaml
- name: Check generated OpenAPI is current
  run: python ci/generate_platform_openapi.py --check
- name: Verify recorded boundaries
  run: boundver verify --source head
```

The check must fail when regeneration would change the tracked output. For
index workflows, stage the derivation source and generated output together,
then generate and stage the lock as shown above. There is no executable
`derived_from` config field; command execution from untrusted repository config
is deliberately not part of the current model.

## Report in CI; update during review

A CI gate should normally leave the checkout unchanged:

```yaml
- name: Verify contracts
  run: boundver verify --source head

- name: Print machine-readable details
  if: failure()
  run: boundver verify --source head --format json
```

`status --format json` continues to expose lock state. Structured `why` and
`slice` output are also available:

```bash
boundver why payment-api --source head --transitive --format json
boundver slice checkout-contracts --format json
```

The author reviews and accepts drift locally:

```bash
boundver verify --source working-tree
boundver why payment-api --source working-tree
boundver verify --source working-tree --update
git diff -- boundary.lock.json
```

An unfiltered update regenerates the full lock. A component-scoped update first
recomputes every current entry, refuses stale unselected entries, replaces each
selected entry as one unit, and recomputes all slices:

```bash
boundver verify \
  --source working-tree \
  --components payment-api \
  --facets boundary,compat \
  --update
```

This command updates exact and behavior data for `payment-api` too; `--facets`
does not preserve old non-gating fields.

## Exit-code-aware automation

| Code | Highest selected result |
|---:|---|
| `0` | Clean |
| `1` | Exact or metadata drift |
| `2` | Usage, configuration, or digest error |
| `3` | Behavior drift |
| `4` | Boundary drift |
| `5` | Compatibility-family drift |

```bash
set +e
boundver verify \
  --source head \
  --facets behavior,boundary,compat \
  --format json > boundver-result.json
code=$?
set -e

case "$code" in
  0) echo "Declared contracts are current" ;;
  2) echo "boundver could not perform a reliable check" >&2; exit 2 ;;
  3) echo "Behavior contract changed" >&2; exit 3 ;;
  4) echo "Boundary changed; re-verify affected consumers" >&2; exit 4 ;;
  5) echo "Compatibility family changed" >&2; exit 5 ;;
  *) echo "Unexpected boundver result: $code" >&2; exit "$code" ;;
esac
```

If several selected facets drift, the highest severity wins. `--fail-fast`
limits output to one highest-severity issue rather than stopping before other
components have been evaluated.

## GitLab CI

```yaml
boundary-verify:
  stage: test
  image: python:3.12-slim
  before_script:
    - apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
    - python -m pip install "boundver[schema,yaml]==0.12.0"
  script:
    - boundver verify --source head
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
```

Ensure the checkout contains the required history when using
`--changed-from` or tag-based version sources.

## Pre-commit and pre-push

Use the published hook definitions so the staged and committed lifecycles are
not conflated:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/yzm1/boundver
    rev: v0.12.0
    hooks:
      - id: boundver-verify       # pre-commit: source=index, portable exact gate
      - id: boundver-verify-push  # pre-push: source=head, portable exact gate
```

When the staged check finds intentional drift, regenerate from the index and
stage the result. Because the config is read from the staged snapshot, stage a
config change before generation:

```bash
# Include boundary.config.json here when it changed.
git add services/payment/openapi.yaml
boundver generate --source index
git add boundary.lock.json
boundver verify --source index
```

If you want the hook to generate automatically, add `boundver-generate`, but
always inspect the resulting lock diff before committing.

## Slice fingerprints as cache keys

A slice combines one facet from several components. Membership can be an
explicit `components` list or `closure_of`, which resolves the seed plus its
transitive downstream configured-component graph. The resolved membership is
stored in the lock. Read the committed value into a workflow output:

```yaml
jobs:
  contract-key:
    runs-on: ubuntu-latest
    outputs:
      fingerprint: ${{ steps.key.outputs.fingerprint }}
    steps:
      - uses: actions/checkout@v6
      - id: key
        shell: bash
        run: |
          value=$(python -c 'import json; print(json.load(open("boundary.lock.json"))["slices"]["checkout-contracts"]["fingerprint"])')
          echo "fingerprint=$value" >> "$GITHUB_OUTPUT"

  build-consumers:
    needs: contract-key
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/cache@v4
        with:
          path: consumer-build
          key: consumer-${{ needs.contract-key.outputs.fingerprint }}
      - run: make build-consumers
```

A boundary slice rotates when a member boundary changes. It is a deterministic
cache key, not a substitute for consumer tests.

## Schedule transitive consumer work

`consumers` edges name configured downstream components;
`external_consumers` names opaque terminals outside the config. Direct impact
is the default. Add `--transitive` when the CI scheduler needs the complete
declared downstream closure:

```bash
boundver verify --source head --transitive --format json
```

Traversal is deterministic and cycle-safe and includes external terminals
attached to any reached component. It follows only declared graph edges; it
does not discover build-system or runtime dependencies.

## Concurrent lockfile updates

Do not hand-edit JSON conflict hunks and do not regenerate inside a Git merge
driver. Finish merging source and configuration, then regenerate from the
materialized snapshot. See the [lockfile merge strategy](LOCKFILE_MERGE.md).
