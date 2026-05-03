# boundver Spec Overview (v1)

## Lockfile schema
- Lockfile schema identifier: `boundary-lock/v1`.
- Canonical schema file: `spec/boundary.lock.schema.json`.

## Component facets
Each component includes four fingerprints:
- `exact`: all tracked files in component path.
- `behavior`: declared behavioral contract files (superset of boundary — includes config, migrations, contract tests). `null` if not configured.
- `boundary`: declared boundary subset via configured provider + paths.
- `compat`: digest derived from compatibility identity (SemVer family mode).

The containment hierarchy is: exact ⊇ behavior ⊇ boundary. Every change that affects boundary also affects behavior and exact.

## Slice model
- A slice is a named set of components plus a mode (`exact`, `behavior`, `boundary`, `compat`).
- Slice fingerprint is stable for unchanged selected component digests.
- Adding unrelated components does not change existing slice digests unless slice membership changes.

## Determinism
- Hashing/ordering/path/canonicalization rules are defined in `spec/HASHING.md`.
- Any implementation that follows this contract should produce matching digests for equivalent repo/source state.

## Source modes
- `head`: committed state at `HEAD`.
- `index`: staged/index state.
- `working-tree`: current checked-out tracked files.

## Forward approach
- No legacy alias expansion in spec language; `boundary` terminology is canonical.
- Future schema changes should use new schema identifiers instead of overloading v1 behavior.
