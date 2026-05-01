# 04 — Runtime Version Support Design

## Goal
Define supported Python versions and upgrade cadence.

## Scope
- Current supported floor (3.8+) and future expansion.

## Design
1. **Support tiers**
   - **Tier 1 (required CI)**: Python 3.8–3.12.
   - **Tier 2 (forward-compat validation)**: Python 3.13–3.14.
2. **Compatibility policy**
   - Avoid runtime dependencies where possible.
   - Keep stdlib-only runtime behavior.
3. **Release criteria**
   - Any v1.0 release must pass Tier 1 and Tier 2 smoke tests.

## Deliverables
- Version support table in README/docs.
- CI matrix plan including 3.13 and 3.14 before v1.0 finalization.
