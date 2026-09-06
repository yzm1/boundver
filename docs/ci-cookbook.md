# CI cookbook

These recipes make the source snapshot, lock schema, and gate policy explicit.
The recipes describe boundver 0.13's v3/semantic-config-v2 contract.
Boundver 0.11 writes v3/v1 locks and 0.10.x writes v2 locks; both require
regeneration and must not be mixed with these writers.

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
      - uses: yzm1/boundver@v0.15.1
        with:
          config: boundary.config.json
          lock: boundary.lock.json
          source: head
```

`head` is the committed pull-request tree. The tagged Action installs the
boundver source bundled with that release, including schema and YAML extras, and
returns `exit-code`, `issues`, `observations`, and compact JSON
`consumer-impact` outputs. Each potentially repository-sized payload is capped
at 64 KiB measured as UTF-16 so the Action stays well below GitHub's
[1 MB per-job output limit](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idoutputs).
`truncated-outputs` is a compact JSON array naming any bounded or unavailable
values. `result-file` points to the complete runner-local verify JSON for
inspection or artifact upload. If verification fails before emitting usable
JSON, all three repository-sized outputs are named in `truncated-outputs` and
`result-file` contains a valid diagnostic JSON with `ok: false` and an
`action_transport.reason`. Treat an incomplete `consumer-impact` as a
fail-closed routing condition; its bounded value is `[]`, never an incomplete
downstream closure.

The Action inputs are:

- `config` and `lock`: paths relative to the checkout root.
- `source`: `head`, `index`, or `working-tree`.
- `facets`: optional comma-separated CLI-wide override. Leave it blank to honor
  component/default policy.
- `components`: optional comma-separated component names. Configured names
  cannot contain commas or surrounding whitespace.
- `changed-from`: optional Git ref for changed-path reporting.
- `transitive`: set to `true` to include the downstream consumer closure in
  impact output.
- `fail-fast`: report only the highest-severity issue after safe evaluation.
- `update`: regenerate after successful computation; normally leave this false
  in a pull-request gate.
- `python-version`: Action runtime, defaulting to 3.12.

### Verification baselines

The `baseline` Action input and the CLI's create-only `--write-baseline` and
shrink-only `--update-baseline` flags are available in v0.13. The Action
applies a supplied baseline read-only and never creates or updates baseline
debt. See [migration inspection and verification
ratchets](migration-and-ratcheting.md#establish-a-new-only-verification-gate)
for the complete workflow and safety constraints.

## Pick a signal-to-noise policy

| Policy | Facets | What fails |
|---|---|---|
| Consumer-facing | `boundary,compat` | Declared public artifact or compatibility-family drift; requires both facets on every selected component |
| Behavior-sensitive | `behavior,boundary,compat` | Observable behavior, boundary, or compatibility drift; requires all three |
| Portable tracked-source hygiene | `exact` | Tracked content, paths, and file identities; text CRLF/LF are equivalent; works for leaf and unversioned components |

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
`leaf` component or a pathless `implicit` component has no boundary digest.
Give heterogeneous components their own policies instead of choosing one loose
global policy:

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

- uses: yzm1/boundver@v0.15.1
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

## Route pull-request work from an immutable range plan

From v0.15, the maintained Action can compare two reconciled commits and emit
the smaller `boundver-plan/v1` routing contract. This is a historical query;
keep an ordinary `verify` job as the current-tree integrity gate.

The example below assumes the base commit and pull-request tip both contain
locks reconciled to those exact trees. Require the branch to update and commit
its lock before this job runs. If locks are updated only periodically, an
unreconciled pull-request tip is not a valid target; `--merge-base` does not
change that. Keep conservative test routing and use `verify --changed-from` for
current-tree integrity and path reporting until a complete plan is available.

```yaml
jobs:
  contract-plan:
    runs-on: ubuntu-latest
    outputs:
      selection-complete: ${{ steps.review.outputs.selection-complete }}
      test-components: ${{ steps.review.outputs.test-components }}
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0
          persist-credentials: false
      - id: review
        uses: yzm1/boundver@v0.15.1
        with:
          operation: review
          base: ${{ github.event.pull_request.base.sha }}
          target: HEAD
          merge-base: true
          transitive: true
          upload-artifact: true
          artifact-name: boundver-review-${{ github.run_id }}
      - name: Refuse a partial output projection
        if: steps.review.outputs.selection-complete != 'true'
        run: exit 1

  affected-consumers:
    needs: contract-plan
    if: >-
      needs.contract-plan.outputs.selection-complete == 'true' &&
      needs.contract-plan.outputs.test-components != '[]'
    strategy:
      matrix:
        component: ${{ fromJSON(needs.contract-plan.outputs.test-components) }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          persist-credentials: false
      - env:
          BOUNDVER_TEST_COMPONENT: ${{ matrix.component }}
        run: ./ci/test-component "$BOUNDVER_TEST_COMPONENT"
```

On success, `result-file` is the complete runner-local plan. An unreliable
review instead sets `transport-complete` and `selection-complete` to `false`
and preserves a diagnostic result; it never substitutes a partial plan. The
name-array outputs are separately bounded for GitHub's job-output limit; a
bounded array becomes `[]`, is named in `truncated-outputs`, and sets
`selection-complete` to `false` instead of returning a partial closure. The
optional uploaded artifact contains the full JSON or failure diagnostic and
the bounded Markdown summary. The Step Summary always labels presentation
truncation. File annotations are emitted only for exact structural target
files when the reviewed target is the checked-out `HEAD`.
Repository-controlled component names are passed through the environment in
the example instead of being interpolated into shell source. Platform matrix
job-count limits still apply; batch a very large selection or route from the
complete artifact rather than treating `selection-complete` as a waiver of
those limits.

The GitLab Catalog component exposes the same endpoint and policy inputs. Set
depth `0` at component expansion time so the runner fetches history before the
script starts:

```yaml
include:
  - component: gitlab.com/boundver-project/boundver/boundver@0.15.1
    inputs:
      job-name: boundver-review
      operation: review
      base: $CI_MERGE_REQUEST_DIFF_BASE_SHA
      target: $CI_COMMIT_SHA
      merge-base: true
      transitive: true
      history-depth: "0"

consumer-tests:
  stage: test
  needs:
    - job: boundver-review
      artifacts: true
  script:
    - python -c 'import json; print(json.load(open("boundver-result.json"))["selection"]["test_components"])'
```

The generated job retains `boundver-result.json` and `boundver-summary.md` even
on failure. Missing refs in a shallow checkout fail before endpoint content is
read and print the exact `fetch-depth: 0` / `GIT_DEPTH: 0` remediation.

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
  - run: python -m pip install --upgrade "boundver[schema,yaml]==0.15.1"
  - run: python -c "import boundver; assert boundver.__version__ == '0.15.1', boundver.__version__"
  - run: python -m boundver verify --source head
```

Use this form for a PyPI mirror or centrally managed Python environment. Do not
let one job write an old v2 or v3/semantic-config-v1 lock while another
verifies the current v3/semantic-config-v2 contract.

The exact upgraded install and import assertion are mandatory when the
environment persists across runs: reused developer virtual environments,
`language: system` pre-commit hooks, and prebuilt container images must not
trust an ambient `boundver` executable. Put the install/assert pair in the
environment or image build. Retain the assertion at invocation so a stale
layer fails before it can write or verify a lock. Invoke `python -m boundver`
with that same interpreter so `PATH` cannot select a different executable. A
disposable runner should still use the exact pin; the tagged composite Action
already binds its bundled implementation to the Action tag.

## Match source mode to the lifecycle

| Lifecycle | Source |
|---|---|
| Pull request / post-commit CI | `head` |
| Pre-commit | `index` |
| Local review before staging | `working-tree` |

Config and lock are bound to the snapshot for `head` and `index`, so a staged
pipeline must stage the lock before it verifies. The full rules, including the
staged-refresh command sequence, are in
[reference](reference.md#source-modes).

## Check generated artifacts before verifying them

Boundver hashes a generated contract but does not bind it to its generator, so
a stale committed artifact verifies clean. Run the generator's deterministic
check first:

```yaml
- name: Check generated OpenAPI is current
  run: python ci/generate_platform_openapi.py --check
- name: Verify recorded boundaries
  run: boundver verify --source head
```

See [reference](reference.md#generated-artifacts-are-not-bound-to-their-generator)
for why there is no executable `derived_from` field.

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

Boundver's exit code carries the drift class, so a pipeline can route on it
directly. The code table and a complete `case` dispatcher are in
[reference](reference.md#exit-codes).

The one distinction worth building your pipeline around: exit `2` means
boundver could not perform a reliable check, not that something drifted. Fail
the build on `2` — never treat it as an acceptable result:

```bash
code=0
boundver verify --source head --facets behavior,boundary,compat || code=$?
if [ "$code" -eq 2 ]; then
  echo "boundver could not perform a reliable check" >&2
  exit 2
fi
exit "$code"
```

## GitLab CI

```yaml
boundary-verify:
  stage: test
  image: python:3.12-slim
  before_script:
    - apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
    - python -m pip install --upgrade "boundver[schema,yaml]==0.15.1"
    - python -c "import boundver; assert boundver.__version__ == '0.15.1', boundver.__version__"
  script:
    - python -m boundver verify --source head
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
    rev: v0.15.1
    hooks:
      - id: boundver-verify       # pre-commit: source=index, portable exact gate
      - id: boundver-verify-push  # pre-push: source=head, portable exact gate
```

The published hooks above run from the exact `rev` in pre-commit's managed
environment. If a repository instead uses a local `language: system` hook or a
shared hook environment, bootstrap it with the exact `--upgrade` install and
version assertion from [Pin a package instead of the
Action](#pin-a-package-instead-of-the-action). Then invoke it through
`python -m boundver` from that interpreter.

Use an exact patch tag such as `v0.15.1` for reproducible hook execution. A
two-component alias such as `v0.15` is intentionally mutable and advances to
the newest patch release in that line. Do not pin the hook to one patch while a
separate CI assertion or system installation expects another; update those
version identities together.

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

The stable `consumer_impact` array is intended for CI fan-out. Each row names
the drifted producer, the gated `boundary`/`compat` facets, configured component
consumers, external terminals, and whether traversal was transitive. For a
component-only matrix:

```bash
boundver verify --source head --transitive --format json > boundver-result.json || rc=$?
jq -r '.consumer_impact[].components[]' boundver-result.json | sort -u
exit "${rc:-0}"
```

The composite Action exposes the same array as the compact JSON output
`consumer-impact`; use `continue-on-error` when a later step or job must route
work from an intentionally failing drift gate. Before routing, require that
`fromJSON(steps.<id>.outputs.truncated-outputs)` does not contain
`consumer-impact`; otherwise inspect or upload `steps.<id>.outputs.result-file`.

Traversal is deterministic and cycle-safe and includes external terminals
attached to any reached component. It follows only declared graph edges; it
does not discover build-system or runtime dependencies.

## Concurrent lockfile updates

Do not hand-edit JSON conflict hunks and do not regenerate inside a Git merge
driver. Finish merging source and configuration, then regenerate from the
materialized snapshot. See the [lockfile merge strategy](LOCKFILE_MERGE.md).
