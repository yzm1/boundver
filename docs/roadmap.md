# Roadmap

Boundver plans work in public
[GitHub milestones](https://github.com/yzm1/boundver/milestones). A milestone is
an intent and prioritization aid, not a promised date or compatibility
guarantee. The changelog records what actually shipped.

## Near-term direction

- **v0.17 — practical monorepo operation.** Measured generation performance,
  scalable coverage, deployment-reference comparisons, safe reviewed-drift
  design, clearer scoped/slice/version diagnostics, repository-relative version
  files, shared-checkout safety, and historical upgrade inspection. The
  [release plan](https://github.com/yzm1/boundver/issues/174) defines the required
  work, conditional design gates, test obligations and release acceptance.
- **v0.18–v0.19 — follow-on operations and review evidence.** Provisional scope
  includes caching after profiling, doctor, computed-target review, explicit
  embedded-input design, portable review bundles and advisory routing/graph
  analysis. These are not v0.17 release requirements.
- **v0.20 — isolated semantic providers.** Deferred until independent proposal
  acceptance and security gates pass. A reviewed protocol and sandbox precede
  first-party language bundles. Catalog graduation is provisional v0.21 work,
  conditional on delivery and field evidence. Neither milestone is a date or
  permission to bypass review or the current Python-provider trust boundary.

## How scope changes

Accepted bugs and documentation corrections may enter the next patch release.
New contracts, lock semantics, or trust boundaries belong in a minor release.
Security work can pre-empt either. Open proposals as GitHub issues so the
trade-off, compatibility impact, and test obligation are visible before code is
written.

The active issue set is the authoritative plan:

- [Open milestones](https://github.com/yzm1/boundver/milestones)
- [Open issues](https://github.com/yzm1/boundver/issues)
- [Changelog](https://github.com/yzm1/boundver/blob/main/CHANGELOG.md)
