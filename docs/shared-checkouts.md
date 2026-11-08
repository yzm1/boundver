# Work safely in a shared checkout

For a committed-state configuration check:

```sh
boundver validate-config --source head
boundver verify --source head
```

The first command validates declarations; it does not replace verification.
HEAD reads use captured committed content and do not require `git write-tree`.
Tests run concurrent HEAD validation with dirty disk files, unresolved merge
stages and another writer's `.git/index.lock` without changing that index.

## Why a default validation can encounter `index.lock`

`validate-config` defaults to `working-tree`. Although its config comes from
disk, tracked-path validation can require a coherent index-backed membership
snapshot. `_SourceAccessor` captures that metadata through the index capture
path, whose `git write-tree` may update Git's cache-tree metadata and acquire
`index.lock`. A real held-lock regression reproduces that failure.

This is not validation rewriting your source. Nor is it permission to remove
another session's lock. Let that operation finish, use an isolated checkout,
or explicitly select HEAD **only when committed state is what you mean**.
Selecting HEAD would not validate an edited on-disk config.

## Which inputs does each source use?

For commands that read each input, the selected view is:

| Source | Config, lock, contract/version files and tracked derivation receipts | Membership and index behavior |
| --- | --- | --- |
| `head` | Bytes from one captured HEAD tree | Reads the committed tree, not the live index; does not call `write-tree` |
| `index` | Bytes from one coherently captured staged tree | Captures/validates the index; can update cache-tree metadata; refuses contention or unmerged stages |
| `working-tree` | Live disk bytes with tracking/type safeguards | Uses index-backed tracked membership; not an atomic disk snapshot; can encounter the same index contention |

`validate-config` reads declarations and validates their provider/tracking
inputs; it does not read the lock or compare fingerprints. `verify` additionally
reads the selected lock and computes the comparison.

Generated lock output is always an explicit working-tree write, not automatic
staging. Baseline files used by verification come from the selected source;
explicit baseline create/update operations are guarded disk writes.

Version constants and inheritance come from the selected config. Component-
relative and repository-relative manifests use the selected content source.
Tag-based versions use local tags resolved in the captured HEAD context and
cached per prefix; a tree snapshot does not freeze the entire tag namespace.
Do not mutate tags during a comparison. These commands do not fetch for you.

Derivation checks read the selected tracked sources, outputs and receipts; they
do not run the generator command. Refresh an artifact and its receipt explicitly
in the intended source before relocking.

Existing first-run fallback rules for unborn/non-Git library use still apply;
a filesystem approximation is not an immutable snapshot. Native custom
providers remain trusted code, not sandboxed or forced to use the supplied
source-read hooks. See [source guarantees](reference.md#source-modes).

## Keep the workflow explicit

- For proposed staged declarations, use `validate-config --source index`, then
  generate/verify from the same view. Do not silently switch to HEAD after an
  index failure.
- Follow the [merge/re-lock workflow](LOCKFILE_MERGE.md). Stage only reviewed
  paths; a generated lock can still be unstaged after publication.
- For narrower diagnostics, use `verify --components NAME --format scoped-json`.
  `--changed-from REF` is a selection hint with full integrity verification,
  not an incremental-performance promise.
- Known category debt uses a separate [verification baseline](migration-and-ratcheting.md#establish-a-new-only-verification-gate),
  not a blanket config/graph acknowledgement. Deployment comparisons use
  explicit local refs and do not turn committed state into deployed state.

Serialize staging/commit activity or use separate checkouts. Source selection
does not make several independent commands a transaction, and a successful
atomic file replacement is not a collaboration lock.
