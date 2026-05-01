# 01 — Correctness & Portability Design

## Goal
Make boundary fingerprinting behavior explicit, deterministic, and portable for public users.

## Scope
- Normalize boundary extraction outcomes (`ok`, `partial`, `error`) and publish behavior contract.
- Eliminate assumptions tied to internal/proprietary artifacts.
- Provide actionable failures when configured boundaries are unavailable.

## Design
1. **Boundary status contract**
   - `ok`: declared boundary paths exist and API digest is produced.
   - `partial`: boundary intentionally implicit (`kind=implicit`) and no boundary paths are declared.
   - `error`: boundary is explicit but no paths or no digest can be produced.
2. **Provider portability layer**
   - Separate public providers (`openapi`, `python-exports`, `typescript-exports`, `json-schema`) from optional org-specific providers.
   - Unknown/custom providers can be registered through config-driven adapters (see doc 05).
3. **Strict diagnostics**
   - Include `boundary_errors` in lock output and `status` command.
   - Preserve strict mode behavior in slices; `--allow-partial` remains explicit escape hatch.

## Deliverables
- Documented status model in README and config reference.
- Provider capability matrix with public-first defaults.
- Clear CLI warnings and failure messages.
