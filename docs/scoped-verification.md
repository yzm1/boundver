# Selected components and repository integrity

Development feature for v0.17, not yet released.

```sh
boundver verify --components kb-schema --source head --format scoped-json
boundver verify --changed-from origin/main --source head --format scoped-json
```

The aggregate command still fails when a repository-level check fails. A clean
selected component does not make a changed graph, inconsistent config or stale
slice acceptable. Conversely, a global error does not mean that component's
fingerprints changed.

`scoped-json` emits `boundver-verification-scope/v1`. Existing `--format json`
keeps its existing shape. The new wrapper contains:

- `verification`: the unchanged aggregate result, including source identities,
  issues, observations, baseline handling and consumer impact. This is the
  authoritative verdict; use the process exit code as before.
- `requested`: the original component argument and optional change-base ref.
- `selection`: selected names, names actually compared and structured
  fingerprint findings. `fingerprint_drift: false` means the completed selected
  fingerprint comparison found no movement, not that integrity checks passed.
  Incomplete or unavailable comparisons have a null drift result.
- `analysis`: actual fingerprinted components, directly compared components,
  version-source dependencies and compared slices. Traversal completion is not
  a successful verification verdict.
- `other_component_findings`: fingerprint movement outside the requested change
  selection when full integrity verification evaluates more components.
- `repository_issues`: remaining aggregate issues not attributed to component
  facet drift or its consumer annotations. This includes slice, metadata,
  configuration and integrity diagnostics. Unknown diagnostics stay here;
  attribution must never make them disappear from `verification.issues`.

The schema is [cli-output.verification-scope.schema.json](https://github.com/yzm1/boundver/blob/main/spec/cli-output.verification-scope.schema.json).
Its sibling verify schema describes the nested aggregate result. Offline
validators should register both local schema resources rather than fetching
them during validation. The CLI itself performs no schema fetch.

## What gets evaluated

`--components` compares those components and affected slices. Existing
computation may fingerprint other slice members, and inherited versions can
read another component's version source without fingerprinting that component.
Configured derivation freshness and global config/lock checks still apply.

`--changed-from` is a selection hint, not an incremental verification engine.
It computes the changed-path selection (intersecting `--components` if given),
then verifies full lock integrity. An empty changed selection still performs
that integrity check. Do not assume it makes verification cheaper.

Slice drift is reported at repository/slice level, not assigned to every member
as local fingerprint drift. Graph and ownership edits remain declaration
integrity changes. Shared version sources and derivations keep their existing
dependency and freshness checks; selection does not waive them.

The receipt describes the comparison performed, before any requested repair.
If `verification.updated` is true, its resolved issues describe the repair;
the receipt is not a second post-publication verification. Baseline-acknowledged
fingerprint movement remains visible in the receipt even if no new gated issue
remains. Neither mode is deployment evidence.
