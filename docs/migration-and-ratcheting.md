# Migration inspection and verification ratchets

This guide covers the review tools intended for an existing repository rather
than a first-time setup: selector analysis for a 0.10 upgrade, discovery gaps,
and an explicit baseline that lets CI reject new verification debt.

The inspection and ratcheting tools in this guide are available in v0.13 and
later. They are not available in the v0.12 package or Action.

All commands that accept `--source` default to `head`. That means committed
state, not unstaged local edits. Use `index` for a staged review and
`working-tree` for tracked files on disk; use the same source when generating
and verifying a lock.

## Interpret a 0.12 lock regeneration

This subsection documents released v0.12 migration semantics; it does not make
the v0.13 inspection or baseline commands below available in v0.12.

Moving a v3 lock from semantic-config/v1 to v2 and accepting v0.12's built-in
provider metadata still requires full regeneration from the reviewed source.
Do not relabel the old lock or copy digest fields into a new structure. When
the selected source bytes and effective selector configuration are unchanged,
however, the recomputed component facet and slice digest values are expected to
be identical. The lock diff should instead show the new semantic-config
contract/digest and provider/config metadata. A changed facet or slice value is
evidence of a content, selector, glob, mode/type, or other effective-input
change and deserves separate review.

The deliberate raw-provider transition from `json-file-raw` to `path-hash` has
the same expectation in v0.12: identical paths, options, and selected bytes
produce the same facet and slice digest values, while provider identity and the
semantic configuration metadata change. Regenerate and review the lock rather
than editing those metadata fields by hand.

## Inspect 0.10 selector changes before regeneration

Boundver 0.10 matched glob declarations with Python's case-sensitive whole-path
`fnmatch`: `api/*.yaml` could therefore include `api/nested/route.yaml`.
Current matching is segment-aware, so `*` never crosses `/` and a complete
`**` segment is required for recursive selection.

Run the analyzer before replacing an old lock:

```bash
boundver migrate-lock --lock boundary.lock.json --explain \
  --config boundary.config.json --source head

boundver migrate-lock --lock boundary.lock.json --explain \
  --config boundary.config.json --source head --format json
```

`--explain` never writes the lock. It audits every boundary and behavior
declaration against one selected source. Raw-provider and implicit boundary
selectors, plus all behavior selectors, receive old-only and current-only match
counts with bounded examples. Literal selectors on the two canonical providers
are also comparable. Other cases are reported explicitly without invented
counts: 0.10 rejected canonical-provider globs, `leaf` ignored boundary paths,
`path-hash` was not registered as a public provider in 0.10, current releases
reject some declarations that 0.10 trimmed or otherwise accepted, and
custom-provider selection is provider-specific. Text output lists changed and
non-comparable declarations plus a concise unchanged count; JSON retains the
complete deterministic audit and records the captured tree object for
`head` or `index`. Analysis is limited to 2,000 declarations, five million
aggregate matching-work units, and five examples in each direction per
declaration; exceeding a limit fails closed.

Component roots are enumerated using 0.10's trim-and-normalize behavior, so a
legacy path spelling such as `" svc "` does not prevent selector analysis.
Current config validation still reports that spelling for correction before
regeneration.

The analyzer does not make an old hash contract migratable. A
`boundary-lock/v1` or `boundary-lock/v2` lock, or a v3 lock with the previous
semantic-config contract, still requires content-based regeneration. The v0.13
`boundver diff` path is deliberately read-only and can compare canonical
`boundary-lock/v3` locks using the known semantic-config/v1 and v2 contracts;
its metadata report includes the contract transition. Verification and any
generation/update path that reuses an existing lock remain v2-only and reject
v1 input; full generation recomputes the repository and emits a new v2 lock.
Different lock schemas and unknown semantic contracts produce one compatibility
diagnostic instead of a misleading list of current-schema structural errors.

## Find roots missing from configuration

Discovery can compare its manifest-derived component paths with registered
component roots without modifying the config:

```bash
boundver discover --diff-config
boundver discover --diff-config --config boundary.config.yaml --format json
```

The comparison separates:

- discovered paths already represented in the config;
- discovered-but-unregistered paths; and
- configured paths for which discovery found no supported manifest.

The last category is informational: manually configured components need not
have a discoverable package manifest.

## Validate strict and intentionally partial slices

`boundver validate-config` now checks the same slice-facet availability needed
by normal strict generation. For example, a boundary slice containing a `leaf`
component is rejected before generation. If null slice inputs are deliberate,
use the same opt-in on both commands:

```bash
boundver validate-config --allow-partial
boundver generate --allow-partial --source head
```

The option does not forgive missing files, provider failures, invalid source
state, or malformed configuration.

## Establish a new-only verification gate

A verification baseline is separate from `boundary.lock.json`. The lock records
reviewed repository state; the baseline records stable identities for known
gated drift while CI rejects any new identity.

Initial capture is deliberately create-only:

```bash
# First inspect the complete failure report.
boundver verify --source head

# Then explicitly capture reviewed, baselinable drift at a new path.
boundver verify --source head \
  --write-baseline .boundver-verify-baseline.json --format json

git diff -- .boundver-verify-baseline.json
git add .boundver-verify-baseline.json
```

The baseline must be a JSON file inside the repository but outside every
component and vendored-copy root, and it cannot overwrite the config or lock.
Capture refuses integrity, configuration, ordinary metadata,
digest-computation, unavailable-facet, and unknown diagnostics. A current
component `compat` mismatch also covers that same component's ancillary
`version` and `semver` metadata lines; those lines are never stored or accepted
as independent identities. An existing path is never overwritten by
`--write-baseline`.

Apply the reviewed ratchet in CI:

```bash
boundver verify --source head \
  --baseline .boundver-verify-baseline.json
```

The baseline is read from the same selected view as the config and lock:
committed bytes for `head`, staged bytes for `index`, and the file on disk for
`working-tree`. Baseline create/update destinations are explicit working-tree
writes; an update compares against the stored baseline in the selected view.
The live destination must still contain those exact bytes when the update is
published, so unstaged edits or a competing writer cause a controlled refusal.

The command exits successfully when every current gated violation has a known
identity. Digest values and human wording are not part of the identity, so a
second change to the same component/facet remains known; drift in another
component or facet is new and fails with its normal severity code. JSON output
separates `issues`, `baselined_issues`, and stale baseline IDs.

Baseline context binds the exact canonical digest of the reviewed lock as well
as the project, lock schema, semantic-config contract,
source mode, component/facet selection, transitive-consumer choice, and
effective facet policy. A context change requires a new, separately reviewed
capture rather than silently reusing unrelated debt.

Updates are shrink-only:

```bash
# After fixing one or more known violations:
boundver verify --source head \
  --update-baseline .boundver-verify-baseline.json --format json
git diff -- .boundver-verify-baseline.json
```

`--update-baseline` removes resolved identities. If the current result contains
any new identity, it refuses to write. It cannot be combined with lock
`--update` or `--fail-fast`, because ratcheting requires a complete reviewed
issue set. To deliberately replace all accepted debt, remove the old file and
repeat the create workflow under review.

The machine-readable contracts are:

- `spec/verify-baseline.schema.json` for the stored baseline;
- `spec/cli-output.verify.schema.json` for baseline-aware verification output;
- `spec/cli-output.discover.schema.json` for discovery comparison output; and
- `spec/cli-output.migrate-lock.schema.json` for selector analysis output.
