# Reviewed drift: v0.17 decision and proposed evidence contract

Status: proposed contract, awaiting review. No new acknowledgement format or
integrity bypass is implemented. Issue [#167](https://github.com/yzm1/boundver/issues/167)
requires a contract and regression corpus; implementation is conditional on
review of the contract and its negative tests.

## What exists today

`boundver-verify-baseline/v1` is a category ratchet, not an exact-change approval.
Its identity hashes the schema, kind, subject and facet. It does **not** hash
the observed new digest. A second change to the same component/facet remains
acknowledged. Keep this behavior explicit; silently changing the meaning of
existing files would be a compatibility break.

The context binds the canonical entire lock digest, project, lock schema,
semantic-config contract, source mode, component/facet selection, transitive
setting and effective policy digest. It does not attest deployment or bind the
current tree. Baselines are read from the selected source. Creation is
create-only; update is shrinking-only and uses exact live-byte comparison plus
the existing guarded publication protocol. Neither operation authenticates the
human reviewer.

Only component-facet and slice-facet mismatches are independent debt. Consumer
impact annotations accompany an acknowledged boundary/compat owner. Version
and parsed-semver metadata may accompany that same owner's compat mismatch;
arbitrary metadata cannot become debt through that exception.

Malformed locks, unsupported contracts, config-digest or graph inconsistency,
missing inputs, provider failures, unavailable facets and incomplete analysis
remain errors. No baseline entry can waive these categories.

## Candidate exact-change acknowledgement (not implemented)

Use a new schema and explicit opt-in, never reinterpret v1 files. An accepted
record would need all of the following machine-computed evidence:

- Source mode, captured old/new tree identities, exact config and lock blob
  identities, lock semantic digest and digest/config contract versions.
- Equal validated semantic declarations, graph and policy identities at both
  endpoints. Configuration reconciliation is outside this initial model.
- Structured component/facet identity and both computed old/new digests.
  No identities parsed from human diagnostic text; no wildcard subjects.
- Provider selection/options and reproducible runtime identity sufficient to
  explain the result. A provider class name alone is not sufficient. Native
  custom-provider evidence remains unsupported until runtime binding is defined.
- A domain-separated, length-delimited record hash of that entire evidence.
  This hash binds bytes, not a reviewer's identity or authorization.

Initially support only fully captured Git-backed sources; live working-tree
reads cannot supply an atomic new-tree identity. Null/unavailable fingerprints
are not acknowledgeable. Missing history, unsupported old digest contracts,
failed recomputation or exceeded budgets invalidate the operation before any
acknowledgement is considered. Existing normal lock/config integrity checks
remain mandatory; a deployment comparison is not a substitute.

Initial acceptance would be explicit create-only capture **after** displaying
the complete valid-source drift. Application would require exact evidence
equality. A second digest change, source change, graph-edge removal, selector
edit, provider change or policy change must not inherit acceptance. Even an
unrelated new-tree change invalidates the initial conservative whole-tree
binding: convenient reuse is not more important than unambiguous evidence.

An update could remove resolved entries only. It must refuse additions,
rebindings and concurrent live-byte changes; use the existing safe baseline
publication machinery rather than inventing a weaker replacement. Repaired
drift produces stale IDs, visibly reported until removed. Unknown kinds,
duplicate IDs, bad hashes, extra fields and corrupt JSON fail closed. A schema
upgrade would require a separately reviewed capture, not relabelling v1.

Retain existing exit meanings: invalid/incomplete evidence is 2; new drift uses
its facet severity; 0 requires a complete check with no unacknowledged selected
drift. JSON must separate acknowledged drift, new drift, stale records and
integrity errors without describing acknowledgement as proof of safety.

## Regression corpus and review gate

`tests/test_reviewed_drift_contract.py` pins existing v1 semantics and refusal
categories. It is **not** a test of the unimplemented candidate above.
Before implementing or shipping that candidate, add end-to-end evidence cases:

| Case | Required candidate result |
| --- | --- |
| Same name/facet, second new digest | New drift, never inherited approval |
| Graph-edge removal or config/provider/policy edit | Integrity/context error 2 |
| Metadata edited to resemble reviewed drift | Error 2, no standalone waiver |
| New component or facet drift | New drift at its severity |
| Repaired drift | Stale record; shrink-only removal allowed |
| Unknown, forged, duplicated or corrupt record | Error 2 |
| Competing baseline update or source movement | Refuse write; retain reviewed bytes |
| Missing old objects, provider failure or budget failure | Incomplete/error 2 |

Existing end-to-end regressions for consumer/compat annotation binding and
publication races remain in `test_baseline_annotation_binding.py`,
`test_baseline_claim_preservation.py` and `test_baseline_safety.py`. These do
not prove the proposed evidence model is reviewed or implemented.

## Usable workflow without rewriting deployment history

Keep normal `verify --source head` as the integrity gate. For known deployment
differences, run the separate read-only `compare-deployment` command against an
explicit existing verified local checkpoint; do not regenerate a deployment
lock to make its differences disappear. Follow
[migration and ratcheting](migration-and-ratcheting.md#establish-a-new-only-verification-gate)
only when category-level debt is actually the intended policy. Commit and review
the baseline independently; do not call it an exact-change approval.

For v0.17, retain the current fail-closed integrity behavior unless the new
contract, evidence implementation and negative corpus pass review. The
limitation is intentional: there is no safe config-digest-ignore switch.
