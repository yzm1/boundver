# boundver Spec Overview (v3)

## Lockfile schema

- Lockfile schema identifier: `boundary-lock/v3`.
- Canonical schema file: `spec/boundary.lock.schema.json`.
- v1 and v2 hash-bearing locks require regeneration; migration cannot infer
  v3 mode/type or semantic-config inputs from an old lockfile.
- Every lock records `boundver-semantic-config/v1` and a digest covering all
  contract-relevant config, including boundary paths/globs/options, behavior paths,
  version sources, vendored copies, compatibility mode, internal and external
  consumers, provider declarations, slice declarations, and default/component
  verification policy.

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

Strict generation rejects a slice whose selected member facet is null.
`generate --allow-partial` relaxes only that intentional null slice input; it
does not suppress missing declarations, provider/version errors, or vendored
copy failures.

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
`diff`, `discover`, `why`, and `slice` support `--format json`. `status` JSON
predates v3; `why` and `slice` are the v3 additions.

## Derived artifacts

The v3 contract fingerprints declared artifacts but has no first-class
source-to-derived-output relationship and executes no generator command from
config. A workflow must run a deterministic generator freshness check before
verification. Declarative derivation semantics are reserved for a future
contract revision.

## Forward approach

- `boundary` terminology is canonical; there is no legacy alias expansion in
  spec language.
- A semantic change to stored data or hashing uses a new schema/hash contract
  rather than silently overloading v3.
