# Roadmap

Boundver plans work in public
[GitHub milestones](https://github.com/yzm1/boundver/milestones). A milestone is
an intent and prioritization aid, not a promised date or compatibility
guarantee. The changelog records what actually shipped.

## Near-term direction

- **v0.16 — declaration integrity and assurance.** Generated-artifact freshness,
  compatibility identities shared across components, declaration-coverage
  reporting, clearer source diagnostics, and tiered testing obligations.
- **v0.17 — isolated semantic providers.** A reviewed provider ABI and sandbox
  come before parsers for language-specific surfaces. This work remains
  decoupled from v0.16 and does not weaken the explicit trust boundary around
  current Python custom providers.

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
