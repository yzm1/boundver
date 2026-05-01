# 06 — Config Schema & Validation Design

## Goal
Prevent invalid configurations early with schema + semantic validation.

## Scope
- JSON Schema publishing, editor support, and runtime validation messages.

## Design
1. **Schema layer**
   - Publish `boundary.config.schema.json` with `$schema` support.
   - Validate structure, required fields, enums, and types.
2. **Semantic validation layer**
   - Cross-reference slices/components.
   - Detect duplicate component paths.
   - Validate provider requirements and boundary path existence.
3. **DX improvements**
   - Actionable error messages with component/slice names and remediation hints.

## Deliverables
- Schema file versioned with releases.
- Validation command output contract.
- IDE autocompletion setup instructions.
