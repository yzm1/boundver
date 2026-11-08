# Why did a slice move?

Development feature for v0.17; not yet released.

```sh
boundver why --slice platform-api --source head
boundver why --slice platform-api --source head --format json --summary
```

Choose one component or `--slice NAME`, not both. Slice explanations show the
aggregate fingerprint, contributing members, all changed member facets, and
version movement. They do not write a lock or contact a deployment environment.

## Read the three results separately

- **Slice changed:** its aggregate fingerprint differs from the stored lock.
- **Contributing member:** that member's digest changed in the slice's mode.
  Changes in another facet remain visible but do not contribute to this slice.
- **Version changed:** a member's recorded previous version differs from its
  currently resolved version. This is not proof of compatibility or deployment.

For example, a version-file edit can move a member's exact fingerprint and
resolved version while leaving a boundary slice unchanged. A missing or invalid
version has status `unavailable`, not `unchanged`; two nulls are not equal
resolved versions.

Component explanations expose the same distinction. For example,
`boundver why service --format json` includes `version_change` with `previous`,
`current` and `status` fields. The previous value comes from the stored lock;
the current value is resolved from the selected source. The legacy `version`
field remains for compatibility and is not a version comparison. Text output
shows both values and their status. Neither form identifies a deployment or
proves that two versions are semantically compatible.

Exit 0 means no gated slice drift; exit 1 means gated slice drift; exit 2 means
invalid or incomplete comparison. Gating follows the effective member policies,
as in normal verification. `why` is an explanation, not a substitute for the
repository-wide `verify` gate.

## Scope, limits and provenance

`--source head|index|working-tree` selects current inputs. HEAD/index use one
captured snapshot; working-tree reads remain live. Current computation retains
the full declaration context, including inherited versions and derivations;
this command is not an incremental-verification performance guarantee.

Rows are sorted by member name. `--limit 0..10000` defaults to 1000; `--summary`
shows no rows. Full counts and drift decisions do not depend on rows shown.
The serialized report has a 4 MiB budget; exceeding it refuses the comparison
with guidance to lower the limit. Slice names are limited to 16,384 characters.

The lock does not identify its generating source commit. The report says that
provenance is unavailable; it never infers it from lock-storage history. Slice
mode does not accept `--base-ref` or `--transitive`. Use an explicit range review
for historical checkpoint comparisons. See [diagnostic bases](reference.md#diagnostic-bases).

Changed declarations, graph/provider contracts, inconsistent stored aggregates,
missing required facets and computation errors are not reported as ordinary
slice drift. They produce an incomplete comparison. Unsupported old digest
contracts are not relabelled or silently accepted.

JSON uses `boundver-slice-explanation/v1`, described by
[cli-output.slice-explanation.schema.json](https://github.com/yzm1/boundver/blob/main/spec/cli-output.slice-explanation.schema.json).
Its current source identities describe the comparison input, not a deployed
environment or an independently verified old source.
