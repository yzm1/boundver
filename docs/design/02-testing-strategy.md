# 02 — Testing Strategy Design

## Goal
Raise confidence from smoke-level checks to production-grade coverage.

## Scope
- Unit, integration, snapshot, and edge-case testing for fingerprint correctness.

## Design
1. **Unit tests**
   - Canonical JSON and hash determinism.
   - SemVer parsing modes.
   - TOML/YAML/JSON version extraction.
2. **Integration tests (temp git repos)**
   - `git init` + commits and tag workflows.
   - Source mode behavior (`head`, `index`, `working-tree`).
3. **Snapshot tests**
   - Golden lockfile fixtures for representative repositories.
4. **Edge cases**
   - Missing/implicit boundaries, empty slices, no commits, unknown components.

## Deliverables
- Test matrix with required cases and expected outcomes.
- Stable fixture generators for temporary repos.
- Coverage targets for critical modules.
