# Deployment-reference comparison: v0.17 contract

Development CLI, backend and output schema are under test. They are not in a
published release. This contract guides
[issue #166](https://github.com/yzm1/boundver/issues/166).

Compare current contracts with one explicitly supplied local reference. Do not
deploy, contact an environment, move a ref or write a lock. The environment
label and ref are operator assertions, not deployment evidence.

## CLI

```sh
boundver compare-deployment --environment production --ref refs/deploy/production \
  --source head --format json
```

`--environment` and `--ref` are required. `--source` selects current `head`
(default), `index` or `working-tree`. `--config` selects the current config;
`--reference-config` defaults to the same selector and handles historical path
renames explicitly. `--reference-lock` defaults to `boundary.lock.json`.
All config/lock selectors are repository-relative, not external filesystem paths.
Environment labels are trimmed nonempty text of at most 128 characters, without
terminal control characters; they are metadata, not account identifiers.

`--facets` uses the existing facet names and current configuration's verify
policy: an explicit selection overrides component policies; otherwise each
component override wins over defaults. Slice gates follow the same member-policy
rule as verification: selecting its mode for any member gates a changed aggregate,
even when the member whose bytes moved does not itself select that facet.
`--format text|json` defaults to text. `--limit 0..10000` defaults to
1000 displayed changed rows across components and slices; `--summary` suppresses them.
Presentation limits never reduce analysis or gating. Custom-provider imports
still require explicit `--allow-custom-providers`; no new trust authorization
is inferred from either endpoint's config.

The first version compares the whole declared component set with one ref.
Mixed per-component deployment commits, implicit environment mappings, remote
lookup, merge-base inference and automatic declaration reconciliation are not
supported.

## Two independent checks

The command computes current fingerprints read-only; it does not refresh or
accept the current lock. Normal current-lock verification remains a separate
mandatory CI step:

```sh
boundver verify --source head &&
boundver compare-deployment --environment production --ref refs/deploy/production --source head
```

For a review, the comparison can run independently to show proposed contract
movement even while the current lock is stale. Its success never certifies
current-lock integrity. JSON explicitly says that check was not performed.
Existing violation-debt `--baseline` files are unrelated and are not consumed.

## Reference and compatibility checks

Resolve the supplied ref once, then read config/lock and verify content through
that immutable commit/tree. Never fetch missing objects or follow a moved ref
again. Capture the current HEAD/index once too; working-tree input remains live
and must not be labelled an immutable snapshot.

Validate the reference config and complete lock structure. Require current v4
lock/digest contracts and verify all reference facets against the referenced
source, regardless of the comparison's selected gating facets. Config digest,
component membership, graph/slices and stored metadata must reconcile there.
Installed provider code and local tag/version resolution can affect this check;
an unexplained historical mismatch is fatal, not silently accepted.

Semantic configuration must agree between reference and current inputs for v1
facet comparison. Graph edits, component/path/provider changes or policy changes
are reported as unreconciled declarations: exit 2, `complete=false`. Do not
infer renames, suppress config integrity faults or quietly choose one graph.
Nonsemantic annotations do not cause this refusal. A later reconciliation model
needs its own review; blanket acknowledgement is not that model.

Missing refs/blobs, insufficient local history, unsupported historical locks,
invalid declarations, incomplete digests or unreconciled reference state also
exit 2. Diagnostics must identify the failing endpoint and a safe next action:
choose an existing verified checkpoint, supply the right historical path or
inspect the upgrade. Never recommend regenerating a deployment record inside
this command or rewriting the asserted deployment ref. History acquisition, if
needed, is a separate operator action; the command remains offline.

## Output and exits

JSON schema identity: `boundver-deployment-comparison/v1`. Required fields:

- `environment`, `evidence` (operator assertion, `attested=false`).
- `current` and `reference` provenance: source mode, exact available commit/tree,
  config selector, and reference ref/lock selector and digest-contract identities.
- `current_lock_verification="not-performed"`.
- `complete`, `ok`, `exit_code`, selected `facets`, aggregate `counts`, bounded
  component `changes`, `slice_changes`, `presentation` and bounded endpoint `issues`.
- `facet_policy` describes effective component and slice gates after current
  configuration validation; it is null before that point or on output-budget
  refusal, when policy details are discarded with the rows. Without an explicit
  selection, `facets` is their union, not a replacement for per-subject gating.

`ok` means a complete comparison with no selected-facet drift, not deployment
attestation or a clean current lock. Counts cover all analyzed components and
facets; presentation reports shown/omitted rows and truncation independently.
Slice counts are separate from component counts. Component rows are sorted first,
then slice rows, sharing one presentation limit; hiding a slice cannot hide its gate.
On failure, unresolved provenance remains null rather than being invented.
The serialized result is capped at 4 MiB; `--summary` can omit detail rows
without omitting counts or gating. Endpoint issue previews are capped at 100
entries of 1024 characters each, with explicit omission counts.

Use existing drift exits: exact 1, behavior 3, boundary 4, compat 5; the highest
selected drift wins. Any integrity, compatibility or analysis-budget failure
takes precedence with exit 2. Complete agreement exits 0. A ref label alone is
never evidence that a contract was deployed or that a consumer remains safe.

## Required regression evidence

Distinct dev/staging/production refs; ref movement after capture; content reverts;
dirty/deleted working files and held index locks under HEAD; malformed or stale
reference locks; graph/config drift; unsupported v3 digests; unavailable local
objects and shallow history; provider authorization; all source modes; hidden
drift under summary/row limits; and preservation of refs, index entries, config
and lock. Explicit index capture may update Git's cache-tree metadata; HEAD
comparison must not require an index write or disturb a held index lock.
Tests must not dispatch hosted CI to build performance fixtures.
