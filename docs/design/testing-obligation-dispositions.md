# v0.16 testing-obligation dispositions

Issue [#137](https://github.com/yzm1/boundver/issues/137) is the authority for
integrating the testing-obligations survey. The survey itself is review
evidence, not user documentation. Its raw narratives and mutation recipes are
kept outside the release tree; this document and
`spec/testing-obligations-summary.json` are the maintained public record.

## Outcome

The source survey recorded 620 obligations, 160 candidate findings, and 178
tests marked `unittest.expectedFailure`. For v0.16 every marker was triaged and
retired:

- 171 were closed through product fixes or corrected regression oracles;
- 2 were closed by making an existing CLI or Python API boundary explicit;
- 5 were rejected because their requirement was contradictory, inexpressible
  at the named layer, or demanded stale wording rather than the safe outcome;
- 0 are deferred and 0 expected-failure markers remain.

The five rejected obligations are enumerated with reasons in the machine
summary. Their positive scope tests remain: strict loaders still verify
baseline identity and ordering, semantic validation still names repairable
identifiers, reparse points are still refused, baseline integrity remains
gated, and optional schema validation remains additive without changing the
verdict.

## Release policy

New tests run in the required core tier unless explicitly assigned to the
bounded exhaustive tier in `spec/test-tiers.json`. The exhaustive tier runs on
schedule and in release verification. A release candidate must fail if an
unlinked expected-failure marker appears, the tier catalog is stale, or the
machine summary disagrees with the test corpus.

The required pull-request core tier has a 45-minute per-job CI limit across the
supported Python and operating-system matrix. That limit is the documented
upper bound for required test execution, not a claim that a typical run should
take the full period. Exhaustive and mutation work stays outside pull requests.

`python -I scripts/test_tiers.py check` prints the source survey's asserted,
example-only, uncovered, and unassessed counts alongside the retired and current
expected-failure totals and the maintained release-mutant count. The primary CI
job runs that report on pull requests and pushes; exhaustive and mutation jobs
then publish their own test and survivor results.

The private survey also produced 750 mutation recipes. Remediation made 91 of
their source anchors stale, so that catalog is retained as historical review
evidence rather than presented as an evergreen gate. The public
`spec/release-mutations.json` selects 12 high-risk, format-neutral faults whose
anchors and named tests are checked on every load. Every selected mutant must be
killed. CI runs this bounded catalog after the normal main or scheduled test
job, and the exact release-candidate verifier runs it again before packaging.

Security-sensitive evidence is routed through GitHub private vulnerability
reporting until a fix is safe to disclose. No unresolved high-risk survey
finding is accepted by this disposition. Boundver's CLI remains telemetry-free;
the assurance tooling reads only the local repository and explicitly invoked
GitHub release controls.
