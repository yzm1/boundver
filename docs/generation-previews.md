# Preview lock changes before writing

Development feature for v0.17; not yet released.

```sh
boundver generate --source head --dry-run --format changes-json
boundver generate --source index --dry-run --format changes-json --summary
boundver diff old.lock.json new.lock.json --format changes-json
```

`generate --format changes-json` requires `--dry-run`. It cannot publish a lock.
The existing `--format json` still returns the candidate lock itself.

The report lists component status, differing facets, changed metadata field
names, previous/current version values and slice changes. Version status is
`changed`, `unchanged` or `unavailable`; two missing versions are not equal
resolved versions. These values do not prove compatibility or deployment.

## Which old state?

Generation reads the previous `--out` lock from the selected source: committed
bytes for HEAD, staged bytes for index, disk bytes for working-tree. A dirty
disk copy does not replace a committed lock during a HEAD preview. If no old
lock exists in the selected source, the report identifies a bootstrap and
lists added components. Scoped `--components` generation still requires an
existing selected-source lock.

HEAD/index inputs use the captured snapshot. Working-tree inputs remain live;
preview reads validate ancestors and do not follow a symlink to an external
old lock. The CLI never stages files or refreshes a lock to obtain a preview.

`--components` previews the merged candidate: unselected records can be retained
from the previous lock without recomputation. `source.components_filter`
records that scope. An unchanged candidate entry is not proof that the current
repository component was re-evaluated. File diffs likewise compare stored
records, not freshly computed repository contracts.

## Changed versus incomparable

Equal supported digest contracts and declarations allow ordinary value
comparison. Declaration or provider-runtime changes make affected common
records `incomparable`; recorded computation errors make otherwise comparable
records `unavailable`. Raw differences remain visible,
but are not classified as ordinary contract drift. Changing declarations
conservatively invalidates comparability for all common component records.

Counts separate added/removed records, comparable changed/unchanged records,
incomparable/unavailable records and raw differences between common records. Slice record
changes can include metadata changes; they are not automatically breaking API
changes. Unsupported historical contracts fail closed; they are not upgraded
or accepted by this format.

Use [historical lock inspection](historical-lock-inspection.md) and
`diff --format upgrade-json` for explicitly read-only v3/v4 comparisons.

Exit 0 means the report completed, including any reported incomparable records;
exit 2 means invalid input or incomplete reporting. The report explicitly says
`current_lock_verification: not-performed`. Keep `boundver verify` as the
integrity gate. No old generating-source commit is inferred from a lock file.

`--limit 0..10000` defaults to 1000 total component/slice rows; `--summary` shows
no rows. Counts still include hidden records. Components precede slices and
names are sorted. A 4 MiB serialized-output budget fails with guidance to lower
the limit. Timed generation retains its existing computation deadline; final
report construction/output is not a hard wall-time guarantee.

JSON uses `boundver-lock-changes/v1`; see
[cli-output.lock-changes.schema.json](https://github.com/yzm1/boundver/blob/main/spec/cli-output.lock-changes.schema.json).
