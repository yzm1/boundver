# Reference: sources, exit codes, and facet availability

This page is the single source of truth for the mechanical rules that every
other guide depends on. The tutorials and recipes link here rather than
restating them, so there is one place to correct when behavior changes.

## Source modes

Every command that accepts `--source` defaults to `head`: committed state, not
your unstaged edits.

| Source | Path set and content | Use it for |
|---|---|---|
| `head` | One captured commit tree | Pull-request and post-commit CI |
| `index` | One captured index tree | Pre-commit verification |
| `working-tree` | Disk bytes for a captured tracked path set | Local editing before you stage |

Five rules follow from that model:

1. **Source selection is per invocation.** `--source` is not shared shell
   state. A bare command immediately after `--source working-tree` defaults to
   `head` again. Pass the source explicitly to each adjacent `verify`,
   `status`, `explain`, or `why` command when you want them to inspect the same
   view.
2. **Generate and verify with the same source.** A lock generated from one
   snapshot does not describe another.
3. **Make files Git-known first.** After the first commit, untracked files are
   excluded from every source. Stage new files before `index`; make them
   Git-known before relying on working-tree glob expansion.
4. **`head` and `index` bind the config and the lock too**, not just component
   content. Both are read from the same captured snapshot. This is stricter
   than 0.10, which could combine staged artifacts with unstaged config.
   `verify --format json` records the exact locations and captured object IDs
   under `inputs`; text output names the selected config and lock explicitly.
5. **Fetch history** before using `--changed-from` or a `git_tag_prefix`
   version source. Shallow clones break both.

A complete staged refresh therefore looks like this:

```bash
# Stage every changed input; include boundary.config.json only when it changed.
git add services/payment/main.yaml services/payment/openapi.generated.yaml
boundver generate --source index
git add boundary.lock.json
boundver verify --source index
```

`--changed-from` is reporting and scheduling information, not a shortcut.
Boundver still recomputes full lock integrity, so unchanged paths cannot hide
stale metadata in a component you did not touch.
Ordinary text output always names the resolved changed-component paths and
prints an explicit zero result; structured output carries the same selection in
`changed_components`.

### Python `load_config` contract

`boundver.load_config(config_path="boundary.config.json", source="working-tree")`
loads and validates a config relative to the current Git repository. Use
`source="head"` or `source="index"` to read and validate the config and its
declared files from one immutable Git snapshot. The default preserves the
working-tree behavior of the original API.

The function returns only a semantically valid config. A lockfile, malformed
document, empty component map, unknown field/provider, unsafe or missing path,
missing config file, unreadable Git source, or unsupported source mode raises
the exported `boundver.ConfigError` (also a `ValueError`). A `$schema` field is
optional: the packaged schema is authoritative, so a repository cannot weaken
validation by replacing a local schema file.

Custom-provider declarations receive dependency-free structural and reference
validation, but `load_config` never imports or instantiates their Python code.
Use the explicit trusted opt-in on `generate` or `verify` when runtime provider
validation and execution are intended. The internal `load_config_file` helper
is parse-only and is not part of this validated public contract.

```python
import boundver

try:
    config = boundver.load_config(source="index")
except boundver.ConfigError as error:
    raise SystemExit(f"invalid Boundver config: {error}")
```

### Diagnostic bases

Fingerprint drift accumulates from the source represented by the lock, not
necessarily from the previous commit. For `source=head`, `why` and `explain`
therefore inspect bounded lock history and default their changed-file comparison
to the commit that introduced the component's current lock entry. This remains
correct when a later partial update changes another component in the same lock.
They print the inferred base and its origin. If bounded history cannot establish
that entry-specific point, they report and use a broader root/lock-history
fallback instead of silently claiming that the previous commit is authoritative.
The inference avoids adding a commit SHA that would make identical locks differ
between `head`, `index`, and `working-tree` generation.

Use `--base-ref REF` to override the inference. `index` and `working-tree`
default to `HEAD`, because their staged or on-disk source does not yet have a
commit identity. Inference is diagnostic evidence, not lock integrity: verify
still recomputes every selected fingerprint from the requested source.

When `explain` finds no changes in the selected source, text output names the
other exact `--source` forms without silently reading those additional views.
This keeps the result bound to one snapshot while making an adjacent source
switch visible.

### Text and JSON presentation

Human diagnostics name their selected source. Consumer edges recorded in the
lock have a separate declared-edge section in `status`, while actual
direct/transitive routing caused by boundary or compatibility drift appears
under `Consumer impact` in `verify` and `why`. The corresponding typed arrays
remain available in JSON.

Actionable selections and routing shown in JSON have a text equivalent.
Machine-oriented details such as the complete lockfile object, facet-policy
maps, full object identities, and stable field names remain JSON-only where the
text command already provides a bounded summary. JSON source/provenance fields
and command defaults are unchanged.

## Selector work limits

Path-glob matching is case-sensitive and segment-aware. A wildcard-bearing
segment is limited to 4,096 UTF-8 bytes and 256 wildcard metacharacters. One
compile-and-match invocation may consume at most 100,000 matcher steps.

That primitive ceiling is not reset into an unbounded declaration/file
cross-product. Each built-in provider selection, component validation
expansion (shared by boundary and behavior), and boundary change-analysis
operation gets one 10,000,000-step aggregate budget. Every normalized pattern
is compiled once in that operation, and its compilation plus every candidate
transition consumes the shared budget. Exceeding it fails closed and asks the
user to reduce wildcard declarations or split the component. Literal paths do
not enter the glob matcher.

## Historical range review

`verify --changed-from` maps changed paths while proving current lock
integrity. It does not compare historical identities. Use `review` when the
question is which recorded facets moved across a branch and which consumers or
slices need re-verification, including after the target lock has already been
reconciled:

```bash
boundver review origin/main..HEAD --merge-base --transitive
boundver review \
  --base origin/main \
  --target HEAD \
  --merge-base \
  --format json
```

The positional and explicit forms are equivalent. `BASE..TARGET` always means
the two named endpoints; three-dot syntax is rejected. Add `--merge-base` to
replace the effective base with the unique common ancestor of the resolved
base and target commits. Output records both the requested base commit and the
effective base commit so that choice cannot be hidden.

Before reading content, Boundver resolves each caller-supplied ref exactly once
to an unambiguous commit ID. It then reads the config and lock from immutable
trees reached by those IDs. A ref moving concurrently cannot create a hybrid
result. Text and JSON output identify the requested refs, resolved commits,
tree IDs, and exact `COMMIT:path` config and lock inputs for both endpoints.

Both endpoints must contain a valid `boundary-lock/v3` /
`boundver-semantic-config/v2` pair whose `config_digest`, component set,
consumer graph, slice membership, and project agree. This deliberately rejects
an unreconciled partial update, legacy or incompatible contracts, malformed
digests, and a graph that cannot be reconstructed reliably. Boundver also
recomputes every component and slice from each immutable endpoint and rejects a
stored lock that lags its source tree, even when its config digest is current.
A component-scoped
generation produced by Boundver is accepted because that operation proves all
unselected entries current before writing its coherent result.

Historical recomputation does not import repository-declared Python by
default. A config using custom providers therefore fails closed unless trusted
automation explicitly adds `--allow-custom-providers`; checking out a branch
never grants that authority.

All four identities are compared. `--facets` changes only the recorded
effective policy selection; it never hides an identity transition. Consumer
impact is triggered by boundary/compat transitions or a consumer-graph edit.
For every affected edge, output says whether it exists at the base endpoint,
target endpoint, or both. This conservative union prevents a removed edge from
silently erasing historical impact. Direct impact is the default;
`--transitive` follows the complete, deterministic downstream closure and maps
changed or impacted components into slices from either endpoint.

For every boundary-facet transition, the machine result also contains one
provider-bound structural report. Each report repeats the base and target
requested ref/commit, effective commit/tree, component path, provider
name/version, and boundary digest so its evidence cannot be detached from the
reviewed artifacts. The provider capability and result identify
`boundver-structural-diff/v1`. In v0.15,
`openapi-canonical` is the first built-in that implements this optional
interface. It compares the provider's canonical JSON trees and emits
deterministic RFC 6901 paths classified as `added`, `removed`, or `changed`,
with only the before/after JSON types. It does not copy contract values into
the result. A newly added or removed subtree is represented once at its root;
arrays are compared positionally.

Raw providers remain raw: review reports `provider-unsupported` rather than
parsing bytes under a new meaning. Component/provider additions, removals, or
version transitions are similarly explicit instead of being compared across
incompatible identities. Structural output is explanatory evidence only. It
does not determine whether an OpenAPI change is backward compatible; use a
format-specific compatibility checker and consumer tests for that decision.

The top-level range `complete` flag describes the authoritative facet, graph,
and slice comparison. `structural_changes.complete` separately describes the
optional provider explanations. An unsupported provider therefore does not
invalidate the range review. A structural budget failure sets that report to
`complete: false` and `truncated: true`, records `limit-exceeded`, and emits an
empty document list rather than presenting partial rows as complete.

Graph traversal and slice mapping share one aggregate 250,000-step work budget
and a 100,000-row construction ceiling. Complete JSON and text documents are
capped at 64 MiB. Exceeding any limit
fails with exit `2` before emitting a partial result; a truncated closure is
never presented as an authoritative review.

Supported structural explanations have independent aggregate ceilings across
the review: 512 MiB of canonical provider input, 250,000 traversal steps,
20,000 change rows, 16 MiB of retained structural output, 64 nesting levels,
and 16 KiB per JSON pointer. The canonical provider's existing per-resolution
source and output limits still apply before this diagnostic pass.

The complete machine result uses `schema: boundver-review/v1` and is validated
by
[`spec/cli-output.review.schema.json`](https://github.com/yzm1/boundver/blob/main/spec/cli-output.review.schema.json).
For CI routing, `--format plan` projects that same in-memory result into
`boundver-plan/v1`; it does not resolve either ref again:

```bash
boundver review \
  --base origin/main \
  --target HEAD \
  --merge-base \
  --transitive \
  --format plan \
  --summary-file boundver-summary.md \
  > boundver-plan.json
```

The plan keeps exact endpoint provenance and effective policy, changed
components/facets, conservative consumer and slice impact, structural evidence,
and deterministic `selection` arrays for changed, impacted, and combined test
components/slices. Its `claim` is `routing-evidence-only`. The contract is
validated by
[`spec/cli-output.plan.schema.json`](https://github.com/yzm1/boundver/blob/main/spec/cli-output.plan.schema.json).
The optional Markdown summary is capped at 50 routing rows and 64 KiB. It says
whether its presentation is complete or truncated and always points back to the
complete JSON as the machine-authoritative result. Structural incompleteness is
reported separately and never disguised as plan incompleteness.

Successful analysis exits `0` even when identities changed because `review` is
a read-only query. Unreliable analysis exits `2`. Run ordinary `verify`
separately as the current-candidate integrity gate.

Direct endpoint comparison needs both endpoint commits and trees, not every
intervening commit. Merge-base mode additionally needs a unique common
ancestor. Output always states whether Git reports a shallow repository and
the effective history requirement. If an endpoint or merge base is absent,
fetch complete history before retrying:

```yaml
# GitHub Actions
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
  with:
    fetch-depth: 0
```

```yaml
# GitLab CI/CD
variables:
  GIT_DEPTH: 0
```

## Exit codes

| Code | Highest selected result |
|---:|---|
| `0` | Selected facets match |
| `1` | Exact or metadata drift |
| `2` | Usage, configuration, or digest error |
| `3` | Behavior drift |
| `4` | Boundary drift |
| `5` | Compatibility-family drift |

Those are `verify` and verification-derived exit meanings. A complete
historical `review` returns `0` regardless of whether transitions are present;
it returns `2` when an endpoint, history, config, lock, or graph cannot be
reconstructed reliably.

When several selected facets drift, the highest severity wins. `--fail-fast`
limits the returned report to one issue; it does not stop boundver from
evaluating every other component first, so the exit code is still the global
result.

Exit `2` is categorically different from `1` and `3`–`5`. The latter mean
"boundver checked, and something drifted." Exit `2` means boundver could not
perform a reliable check at all. Automation should treat it as a build error,
never as drift to be accepted:

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

`--format json` exposes issues, non-gating observations, selected facets,
component selection, update status, exact input provenance, and typed
`consumer_impact`. `review`, `status`, `why`, `diff`, `slice`, and `discover`
also have `--format json`. `why` distinguishes `observed_drift` from policy-gated
`drifted`; an exact-only observation does not recommend regeneration when
`exact` is not gated.

Human-readable output renders every value as one terminal-safe line. Embedded
newlines, carriage returns, C0/C1 controls, DEL, ANSI/OSC introducers, and
Unicode line separators appear as visible escapes; a value beginning with
`::` cannot become a GitHub Actions workflow command. Ordinary Unicode remains
readable. The composite Action applies the same rule to each `issues` and
`observations` item before joining items with newlines. Structured JSON output,
including `consumer_impact`, preserves the exact machine data instead.

### Diagnostic limits

Configuration, lock-generation, and verification failures retain at most 256
diagnostic entries and 256 KiB of UTF-8 text in total; each entry is capped at
8 KiB. When either aggregate limit is reached, boundver stops collecting and
adds one `DIAGNOSTICS TRUNCATED` sentinel. The operation remains failed and
`verify --format json` carries the sentinel in `issues`, so the composite
Action exposes the same condition rather than turning omitted failures into a
successful result. Exit code `2` is used when truncation prevents a reliable
classification.

### Consumer graph limits

Machine-readable impact remains complete and schema-valid: a config may contain
at most 10,000 configured components and 10,000 distinct external-consumer
labels repository-wide, and each component name or external label may contain
at most 16,384 characters. `validate-config` rejects larger graphs before
generation or verification; boundver never emits a partial transitive closure.

Configured component names and internal references to them cannot contain a
comma or leading/trailing whitespace. `--components`, the GitHub Action, and
the GitLab Catalog all use a comma-separated filter and trim tokens, so allowing
those spellings would create components that no scoped command could address.
Opaque `external_consumers` labels are not component selectors and retain their
broader string contract.

## What each facet needs

A facet is *available* for a component only when its declaration can produce a
digest. Gating a facet a component cannot supply is a configuration error, not
drift — boundver reports `UNAVAILABLE FACET` and exits `2` rather than
inventing an identity.

| Facet | Available when |
|---|---|
| `exact` | Always, for any component with tracked files |
| `behavior` | `behavior.paths` is declared and non-empty |
| `boundary` | The provider publishes a boundary — any provider except `leaf`, and `implicit` only when it declares paths |
| `compat` | `version_source` is declared |

Two consequences are worth internalizing before you write a policy:

- **A `leaf` component never supplies `boundary`.** That is the point of
  declaring it a leaf: it consumes a contract without publishing one. It still
  supplies `exact`, and it can supply `behavior` when `behavior.paths` is
  non-empty and `compat` when `version_source` is declared.
- **A slice inherits this constraint from its members.** A slice in
  `mode: boundary` needs a boundary digest from *every* member. This bites
  most often with `closure_of`, where you do not choose the membership: the
  resolved downstream closure can pull in exactly the leaf consumers that
  cannot supply the mode. Use `exact` for heterogeneous closures.

`validate-config` applies these rules by default, so an unsatisfiable policy is
reported before `generate` runs.

The current compatibility identity must come from a declared version file or
a reachable `git_tag_prefix`. Inheriting a sibling component's identity or
declaring a validated constant is tracked in
[GitHub issue #40](https://github.com/yzm1/boundver/issues/40); neither spelling
is accepted by the v2 semantic-config contract.

`git_tag_prefix` is a literal prefix, not a glob. It is limited to 4,096
characters and must be able to form a valid `refs/tags/<prefix><semver>` name.
Git-forbidden whitespace and controls, `~`, `^`, `:`, `?`, `*`, `[`, `\`,
`..`, `@{`, empty path components, dot-prefixed components, and completed
components ending in `.lock` are rejected during config validation. Unicode
and slash-separated namespaces are supported; a final `/`, `.`, or `.lock`
is valid only when appending the version completes a valid tag component.

A valid prefix can still have no reachable tag, especially in a shallow
clone. That is a source-history error reported during generation, distinct
from an invalid declaration. Fetch the required tags and commit history; do
not replace the literal prefix with wildcard syntax.

### `--allow-partial`

`--allow-partial` permits *intentional* null slice inputs — a boundary slice
that temporarily includes an `implicit` component during adoption, for example.

It does not suppress failures. Missing declared paths, provider errors, broken
version sources, and vendored-copy mismatches remain fatal with or without it.

## Generated artifacts are not bound to their generator

Boundver hashes a generated contract. It does not know that the file was
generated, what produced it, or whether regenerating would change it. A stale
committed artifact therefore verifies clean.

Give the generator a deterministic `--check` mode and run it *before*
verification:

```yaml
- name: Check generated OpenAPI is current
  run: python ci/generate_platform_openapi.py --check
- name: Verify recorded boundaries
  run: boundver verify --source head
```

The check must fail when regeneration would change the tracked output. For
index workflows, stage the derivation source and the generated output together,
then generate and stage the lock.

There is deliberately no executable `derived_from` field. A checked-out config
is not authorization to execute repository commands, and a sound design also
has to bind tool identity and source materialization. Declarative
derived-artifact support is tracked in
[GitHub issue #39](https://github.com/yzm1/boundver/issues/39).

## Upgrading

Boundver pins configuration and CLI-output schemas to the release tag. A
persisted lock points to the immutable canonical publication of its structural
lock schema (`boundary-lock/v3` currently uses the v0.13.0 schema URL), so a
digest-neutral package upgrade does not dirty the lock merely to rotate a
schema annotation. A structural lock change must advance the lock schema and
select a new canonical publication. The upgrade procedure is:

```bash
python -m pip install --upgrade "boundver[schema,yaml]==0.14.1"
boundver validate-config
# Stage changed config and every changed or newly selected contract input.
git add boundary.config.json services/payment/openapi/new-route.yaml
boundver generate --source index
git add boundary.lock.json
boundver verify --source index
git diff --cached -- boundary.config.json boundary.lock.json
```

Replace the illustrative source path with your own changed inputs, and omit
`boundary.config.json` when it did not change. If you prefer a `head` baseline,
commit the config and source changes first and generate afterwards. Update CI,
local tooling, and automation in the same change.

Hash-bearing locks are never relabelled. `boundver migrate-lock` rejects
`boundary-lock/v1` and `v2`, and rejects v3 locks carrying
`boundver-semantic-config/v1`, directing you to regenerate from repository
content instead. For a current v3 lock, the command only removes supported
legacy metadata or fills supported missing maps. An already-normalized lock is
a true no-op: its representation and file metadata are left untouched.
`--dry-run` prints prospective normalized JSON only when data would change and
otherwise reports the no-op without rewriting or reformatting the lock.

For what a regeneration diff *should* look like on each upgrade path — and how
to inspect 0.10 selector changes before you commit to one — see
[migration and ratcheting](migration-and-ratcheting.md).

## Related

- [Why boundver?](WHY_BOUNDVER.md) — the model, and what it cannot detect
- [Getting started](getting-started.md) — first configuration and baseline
- [Gradual adoption](gradual-adoption.md) — staged rollout in an existing repo
- [CI cookbook](ci-cookbook.md) — pipeline recipes
