# CI Re-enable Plan (v1.0.0 Gate)

## Current State (as of 2026-05-02)
- `.github/workflows/ci.yml` stays manual-only (`workflow_dispatch`).
- Automated PR/push CI is intentionally disabled to prevent runaway GitHub Actions costs during rapid development.

## Re-enable Preconditions
1. **Release gate reached**: version branch/tag for `v1.0.0` is being cut.
2. **Budget controls set**:
   - Monthly Actions spend alert configured.
   - Workflow concurrency enabled to cancel superseded runs.
   - Path filters and trigger scope reviewed to avoid unnecessary runs.
3. **Owner assigned**: one maintainer accountable for CI cost/health rollback decisions.

## Re-enable Steps
1. Restore PR/push triggers for `main`.
2. Enable required checks:
   - `pytest -q`
   - packaging smoke build/install (`wheel` + `sdist` + `boundver --help`)
3. Keep optional checks (lint/type) non-blocking for initial rollout week.
4. Add weekly spend review for first 4 weeks post-enable.

## Rollback Policy
- If spend exceeds expected weekly envelope or queue contention impacts productivity,
  revert to manual-only dispatch and open a follow-up issue with run breakdown.

## Success Criteria
- CI is auto-triggered on PR/push with stable pass/fail signal.
- Spend remains within budget envelope for 4 consecutive weeks.
- Required checks block regressions without repeated emergency disablement.
