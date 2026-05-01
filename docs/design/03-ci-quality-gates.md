# 03 — CI Quality Gates Design

## Goal
Provide predictable quality gates while controlling cost during rapid development.

## Scope
- Define staged CI re-enable plan and required checks.

## Design
1. **Stage A (current / cost-controlled)**
   - Manual (`workflow_dispatch`) CI only.
   - Keep tests runnable locally and in ad hoc CI.
2. **Stage B (pre-v1 hardening)**
   - PR-triggered tests on key versions.
   - Lint/type checks as non-blocking signals.
3. **Stage C (v1.0 and after)**
   - Required PR checks (test + lint + type).
   - Optional nightly broader matrix.

## Deliverables
- Cost-aware CI schedule.
- Policy for when checks become required.
- Rollback/fallback if CI spend spikes.
