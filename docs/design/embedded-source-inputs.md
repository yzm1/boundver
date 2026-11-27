# Declared embedded source inputs

Status: proposal for the v0.18 design milestone. Implementation and migration
require separate review and validation of a worked example by an adopter.
See [issue #173](https://github.com/yzm1/boundver/issues/173).

## The gap

Consider a repository with `shared/contract.txt` and `service/contract.txt`.
Both directories are components and `shared` declares `service` as a consumer.
A packaging step copies the tracked shared file into the service artifact.

On v0.17.0, changing only the shared file changes the shared component and
reports service impact. The service component entry is byte-equivalent before
and after the edit. Adding `../shared/contract.txt` to the service's boundary
selectors fails schema validation, as intended. The declared graph routes
work, while a component fingerprint describes that component's own source
envelope. Neither independently describes the service's combined build inputs.

An explicit source input should let the service's exact fingerprint account
for that file, even when the packaging step writes an artifact outside the
repository. The fingerprint records what was selected for a source snapshot.
A claim about what a deployed artifact actually embeds needs independently
verified build provenance or a receipt bound to that artifact.

## Proposed declaration

A component may list individually named repository-relative files:

```json
{
  "path": "service",
  "boundary": {"provider": "path-hash", "paths": ["contract.txt"]},
  "embedded_inputs": ["shared/contract.txt", "vendor/runtime/settings.toml"]
}
```

- Require canonical, portable repository-relative paths to regular files
  outside the consumer's component root. Reject directory paths, globs,
  duplicates, symlinks, special files and submodule entries. Report a missing,
  deleted or untracked input against the selected source and component.
- Capture the inputs through the operation's existing source accessor.
  HEAD and index resolve paths against one captured tree. Working-tree mode
  follows the existing captured tracking and bounded file-read rules.
  A changed or unverifiable read fails closed.
- Attach inputs explicitly to each consumer. Ownership and coverage reporting
  still apply to the file at its original path. An existing owner may also
  declare consumer edges. An attachment adds no graph edges, provider
  authority, or traversal into the owning component's tree.
- Reject files declared as derivation outputs in the initial contract.
  Supporting generated inputs requires an explicit decision on validating
  derivation receipts in the same operation and on how those receipts bind to
  any build artifact. A snapshot read still needs verification when a file
  happens to be named like an output or manifest.

The first implementation would affect **exact** fingerprints only. Providers
continue to consume their existing component-local behavior and boundary
inputs. Compatibility and version sources keep their existing contracts.
The selected source should appear in the change explanation, alongside the
declared repository path, without implying import discovery or build execution.
A change to an attached file should make its explicitly declared consumer
eligible for changed-component reporting, even when the file belongs to a
different component. It does not extend an explicit component selection.

## Identity and migration

For a component with attachments, frame the existing local exact digest and
the sorted attached file identities in a versioned, domain-separated
composition. Each attached identity includes its canonical repository path,
Git mode and raw content digest. An equal-content revert at the same path
and mode reproduces the fingerprint. Commit IDs, timestamps, graph edges and
filesystem inode numbers have no role in that digest.

A declaration changes semantic configuration identity. The feature needs a
reviewed semantic-config contract revision and an explicit migration for
configured repositories. Adding or changing attachments requires deliberate
lock refresh. Existing configurations must keep current config and fingerprint
identities. Preserve existing lock decoding and version semantics where they
can describe the new composition unambiguously. If the lock must record
additional identities for explanations or migration, revise the lock schema
explicitly before implementation. Older runtimes must reject the new
declaration as unsupported. Provider digest changes are outside this proposal.

Apply finite per-component attachment-count, repository-wide attachment and
total path-byte limits before reading files. Count attached file bytes and
file visits against the operation's existing read and exact-fingerprint
budgets. Shared bytes may be reused inside one immutable operation if
per-consumer accounting remains enforced. Set numeric limits from the
100- and 250-component workload before merging an implementation. A breach
must prevent a reliable verification result or lock publication.

## Acceptance before implementation

- Replay the shared-file example with two services, only one of which declares
  the input. Verify that a changed file affects the selected component and
  does not affect its peer's exact digest. Cover a vendored path with and
  without a separate owning component and multiple consumers sharing one
  file.
- Test a revert, rename, content-equivalent file with a different mode,
  missing/deleted file, symlink, untracked working-tree input, derivation
  output, and a read that fails mid-operation. Cover relationship to existing
  selectors and changed-component reporting.
- Confirm that an old lock still verifies for existing declarations, and
  that an attached-input configuration requires an explicit lock refresh.
  Compare the existing CLI and machine output contracts before deciding how
  to explain attachments.
- Use a stale build receipt in the worked example. Source verification can
  report that the selected content changed, while the artifact claim remains
  independently unverified. Record adopter feedback on the worked example
  before deciding whether to implement the feature.

Automatic import closure, repository code execution and external fetch are
outside this design. An eventual advisory analysis of import reachability
would need to show its limitations, especially for dynamic imports, and would
carry no authority to add inputs or grant provider access.
