# TypeScript package boundary

This example treats `src/index.ts` as a TypeScript package's public export barrel. Its component version comes from `src/version.json`; discovery still recognizes the package's root `package.json`.

The `typescript-exports` provider fingerprints the declared artifact. It complements rather than replaces TypeScript compilation or consumer tests.

## Generate and verify

Install `boundver`, then run from the **boundver repository root**:

```bash
boundver generate \
  --config examples/typescript-package/boundary.config.json \
  --out examples/typescript-package/expected.boundary.lock.json \
  --source working-tree
boundver verify \
  --config examples/typescript-package/boundary.config.json \
  --lock examples/typescript-package/expected.boundary.lock.json \
  --source working-tree
```

Edit `src/index.ts` to see exact and boundary drift, then regenerate the expected lockfile after reviewing the change.
