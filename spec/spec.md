# boundver Spec Overview (v3)

## Lockfile schema

- Lockfile schema identifier: `boundary-lock/v3`.
- Canonical schema file: `spec/boundary.lock.schema.json`.
- v1 and v2 hash-bearing locks require regeneration; migration cannot infer
  v3 mode/type or semantic-config inputs from an old lockfile. A v3 lock with
  `boundver-semantic-config/v1` also requires regeneration because its digest
  has a different semantic field set and cannot be relabelled as v2.
- Every lock records `boundver-semantic-config/v2` and a digest covering all
  contract-relevant config, including boundary paths/globs/options, behavior paths,
  version sources, vendored copies, compatibility mode, internal and external
  consumers, provider declarations, slice declarations, and default/component
  verification policy. Presentation-only component `ecosystem` and `note`
  annotations, plus boundary `note` annotations, are excluded from v2 alongside
  `$schema`.

## Component facets

Each component includes four fingerprints:

- `exact`: every selected tracked file in the component path, binding path,
  Git mode/type, and content.
- `behavior`: a cryptographic envelope over declared behavioral entries and
  the component boundary digest. It is `null` when behavior is not configured.
- `boundary`: the configured provider's public contract selection. Raw path
  providers bind path, Git mode/type, and content; canonical providers bind
  their deterministic semantic representation.
- `compat`: digest derived from compatibility identity (SemVer family mode),
  or `null` when no version source supplies an identity.

The intended containment hierarchy is exact ⊇ behavior ⊇ boundary. The
behavior envelope enforces its boundary dependency cryptographically: any
boundary digest change changes a configured behavior digest even when the
declared behavior paths are disjoint.

## Contract version axes

- `boundary-lock/v3` identifies the persisted lock shape and the core v3
  entry/framing domains used by exact, behavior, compatibility, and slices.
- `boundver-semantic-config/v2` independently identifies the normalized
  configuration field set and canonical digest domain.
- Each built-in provider independently versions its selection, validation,
  normalization, and output identity in `boundary_provider_version`.

A release may advance one axis without changing the others. An identifier is
never reused after its meaning changes; verification requires every recorded
axis to match, and non-equivalent digests must be regenerated from source.

## Version sources

- File-backed version fields are textual identifiers. TOML version values must
  be quoted strings; numeric TOML values are rejected so extraction cannot vary
  with the parser or Python's process-wide decimal conversion limit.
- JSON and YAML retain bounded numeric-version compatibility. Integers use the
  JSON decimal grammar (no plus sign, leading zeroes, separators, alternate
  bases, or sexagesimal notation) and are limited to 4,300 decimal digits.
- Version values must be strings or finite numbers; booleans, nulls, mappings,
  and sequences are rejected. TOML parsing is provided by the standard library
  or the required `tomli` compatibility dependency. YAML version sources
  require the `yaml` extra. If an authoritative parser is unavailable,
  extraction fails closed rather than applying a partial regex grammar.
- A numeric run longer than 640 significant digits anywhere in a TOML config
  or version-source document is rejected before parsing, including when the
  selected version field is a valid string. Digits inside strings and comments
  are not numeric tokens.

## Slice model

- A slice has a mode (`exact`, `behavior`, `boundary`, or `compat`) and exactly
  one membership declaration: an explicit `components` set, or `closure_of` a
  configured component.
- `closure_of` resolves to the seed plus every configured component reachable
  by following downstream `consumers` edges. Resolution is sorted and
  cycle-safe, and the resolved membership is persisted in the lock entry.
- Its fingerprint is stable for unchanged selected component digests.
- Adding unrelated components does not change a slice unless its declaration
  or membership changes; those declaration changes are still visible through
  the lock's semantic config digest.

Strict configuration validation and generation reject a slice whose selected
member facet is null. Validation resolves `closure_of` before checking every
member, so a downstream component cannot introduce a late generation failure.
`generate --allow-partial` relaxes only that intentional null slice input; it
does not suppress missing declarations, provider/version errors, or vendored
copy failures. `validate-config --allow-partial` applies the same static slice
availability relaxation without generating a lock.

## Consumer graph

- `components.<name>.consumers` is a unique list of configured component names
  directly downstream of `<name>`. Unknown names and self-edges are invalid.
- `external_consumers` is a unique list of opaque non-component terminal
  labels. A terminal cannot alias a configured component.
- Direct consumer reporting is the default. `verify --transitive` and
  `why --transitive` follow internal edges, include external terminals declared
  by the source and every reached component, deduplicate results, and terminate
  safely on cycles.

## Verification policy

- An explicit CLI `--facets` list applies to every component.
- Otherwise `components.<name>.verify_facets` overrides
  `defaults.verify_facets` for that component.
- With no explicit/configured policy, the implicit fallback is every facet
  available for the component; intentional null facets do not make that
  fallback fail.
- If CLI, component, or default policy selects an unavailable locked/current
  facet, verification is a usage error (exit `2`).
- A slice is gated when its mode is selected by the CLI policy, or, without a
  CLI policy, by at least one resolved member's effective component policy.

### Verification baseline ratchet

- `verify --write-baseline PATH` is an explicit create-only operation.
  `--update-baseline PATH` is shrink-only and refuses any newly observed
  violation identity. Neither operation silently updates the lock.
- Only component-facet mismatches, slice-facet mismatches, and their affected-
  consumer annotations are baselinable. Metadata, configuration, structure,
  provider/digest, vendored-copy, unavailable-facet, and unclassified failures
  are never independently acknowledged. When a current component `compat`
  mismatch is known, its same-component `version` and `semver` metadata lines
  are ancillary observations of that mismatch and are covered only while the
  compat mismatch is present; no metadata identity is stored.
- Identities exclude changing digest values but the baseline binds the complete
  canonical lock digest, project, lock/config contract identifiers, source,
  verification scope, consumer traversal, and effective facet policy.
- Applying a baseline removes known identities from the gated failure set.
  Newly observed identities retain their ordinary exit severity; stale stored
  identities are reported for explicit shrink-only cleanup.
- The stored format is `boundver-verify-baseline/v1`, described by
  `spec/verify-baseline.schema.json`, and is bounded to 10,000 identities.

## Determinism

- Hash framing, Git modes/types, source snapshots, path/glob selection,
  ordering, canonicalization, failure behavior, and semantic config hashing are
  defined in `spec/HASHING.md`.
- Any implementation following that contract produces matching digests for an
  equivalent config and repository/source state.

## Source modes

- `head`: one captured committed tree resolved from HEAD at operation start.
- `index`: one captured index tree written at operation start.
- `working-tree`: one captured tracked path set with current disk content and
  mode/type observations.

For `head` and `index`, config and the verification lock are read from that
same captured source, not from a different working-tree view. Generation writes
the new lock to the working tree. An index workflow therefore stages source,
derived output, and config before generation, then stages the lock before
verification.

## Machine-readable CLI output

Canonical schemas live in `spec/cli-output.*.schema.json`. `verify`, `status`,
`diff`, `discover`, `why`, `slice`, and historical `review` support
`--format json`;
`migrate-lock --explain` also supports a bounded JSON selector audit.
`discover --diff-config` adds a deterministic registered/unregistered path
comparison to the discovery payload. Baseline-aware verification adds its
action, acknowledged issues, stale identities, and shrink delta under the
optional `baseline` member.

`diff` is a non-mutating review surface. It accepts canonical
`boundary-lock/v3` inputs using the explicitly supported semantic-config/v1 or
v2 contracts, reports a contract transition under `changed_metadata`, and does
not relabel or trust a historical digest as current. Different lock schemas,
unknown semantic contracts, and structures whose changes cannot be represented
by the diff output contract are rejected before comparison. Generation and
verification outputs continue to use semantic-config/v2: full generation
recomputes it from repository content, while verification and generation paths
that reuse an existing lock reject semantic-config/v1 input.

Historical range output uses the versioned `boundver-review/v1` contract. It
binds two reconciled v3/v2 config-lock pairs to explicit immutable commit/tree
identities, compares every facet, and records conservative base/target consumer
edge provenance plus slice impact. Boundary transitions also carry a typed,
provider/version/digest-bound structural report. The first supported report is
canonical OpenAPI JSON-tree drift: value-free RFC 6901 paths classified as
added, removed, or changed. Unsupported and over-budget explanations contain no
partial rows and have their own completeness state; they do not weaken the
complete facet/impact result. Structural evidence is not a compatibility
verdict. The command is read-only and returns success for any complete range
analysis; ordinary verification remains the integrity gate.

`review --format plan` projects that same captured result into the versioned
`boundver-plan/v1` CI contract. It preserves endpoint provenance, policy,
changed facets, conservative consumer/slice impact, structural completeness,
and deterministic changed/impacted/test selections while omitting historical
fingerprint pairs that a scheduler does not need. Its claim is routing evidence
only. Bounded human summaries and Action outputs may truncate explicitly; the
complete plan file never contains a partial closure.

## Derived artifacts

The v3 contract fingerprints declared artifacts but has no first-class
source-to-derived-output relationship and executes no generator command from
config. A workflow must run a deterministic generator freshness check before
verification. Declarative derivation semantics are reserved for a future
contract revision.

## Forward approach

- `boundary` terminology is canonical; there is no legacy alias expansion in
  spec language.
- A semantic change advances the narrowest recorded contract axis described
  above; no lock, semantic-config, or provider identifier is silently
  overloaded.
