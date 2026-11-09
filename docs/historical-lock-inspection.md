# Inspect an old lock before upgrading

Development feature for v0.17; not yet released.

```sh
boundver diff old.lock.json new.lock.json --format upgrade-json
boundver diff old.lock.json new.lock.json --format upgrade-json --summary
```

This is read-only. It parses supported v3/v4 records under the normal strict
JSON, storage and structural limits. It never fetches history, imports a
provider, rewrites a file or accepts historical digests in normal verification.
It works outside a Git checkout because it inspects records, not their sources.

## What the report establishes

- `comparison` identifies each schema, semantic-config contract and config
  digest. `changed_headers` lists changed header fields separately.
- Component rows distinguish equal recorded values from comparable contracts.
  They show fingerprint differences, metadata field names, provider identities
  and version values. Unknown versions remain unavailable.
- Slice rows show aggregate fingerprints, modes, value equality and changed
  metadata fields. Equal component records do not imply equal slices or headers.
- Full counts survive `--summary` and `--limit`; the shared 4 MiB output budget
  still fails closed. See [generation previews](generation-previews.md).

Recorded computation errors are `unavailable`, not ordinary differences, when
the contracts otherwise permit comparison. Their affected slices remain
unavailable too. Version availability is counted separately.

Across incompatible digest/config contracts, declaration equivalence is unknown,
not inferred from equal digest strings. Common records are marked incomparable
even when every recorded component value is equal. Within matching supported
contracts, ordinary value comparison is possible, but it is still comparison
of records rather than independently verified old source content.

Exit 0 means inspection completed, including incomparable results; it is not a
migration approval or compatibility verdict. Exit 2 means invalid input or an
incomplete report. Schema, semantic-config, provider and recorded error states
must not be dismissed just because some fingerprint strings match.

## Why equal-entry trials are not a general migration

An adopter's trial found equal component entries after regeneration across an
upgrade. That is useful evidence about that trial, not proof that all old locks
can be converted by changing their schema label. Headers, slices, declaration
digests and runtime/provider contracts can change independently. This command
does not determine whether an observed difference came from source changes or
an algorithm change when the generating inputs cannot be proved.

Locks do not identify their generating source commit. The report says its
provenance is unavailable even if Git history exists nearby. A mixed deployment
cannot be reconstructed from a whole-repository lock. Do not infer one from
lock-storage commits, hashes or a manually assembled component list.

## Adopt a new pin, config and lock together

Start in an isolated checkout at an explicitly chosen, reviewed checkpoint.
If it must represent deployed state, verify that independently first. Do not
re-lock a shared dirty checkout containing other sessions' undeployed changes.
Leave existing deployment refs unchanged during this preparation.

Install the reviewed new tool version and update its package/Action/container
pin and any required config/schema declarations. Inspect changes before staging.
For example, if the dependency pin lives in `requirements.txt`:

```sh
git add -- requirements.txt boundary.config.json
boundver validate-config --source index
boundver generate --source index --out boundary.lock.json
git add -- boundary.lock.json
boundver verify --source index
git diff --cached --check
git diff --cached
```

Substitute only the actual reviewed pin file(s); do not stage every local file.
Review and commit the pin, config and lock together. Generation uses the staged
view here deliberately: HEAD still contains the old declarations. The lock
output must stay repository-contained and outside fingerprinted inputs.
Publication uses the existing guarded atomic writer; this is not a promise that
live working-tree reads or several separate Git commands form one transaction.

Neither inspection nor regeneration marks a deployment as completed. Run the
normal integrity, application and deployment checks before assigning a new
deployment reference. Content-assisted historical rewriting remains design-only:
it would require validated historical source, config and provider identities.
