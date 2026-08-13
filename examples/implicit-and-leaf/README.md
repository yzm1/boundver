# Implicit and leaf boundaries

This example contrasts two intentionally lightweight providers:

- `worker` uses `implicit`, which records exact content while its boundary remains undeclared and reports partial boundary coverage.
- `leafsvc` uses `leaf`, meaning the component has no downstream contract to fingerprint.

Both are useful during gradual adoption, but neither provides an API compatibility guarantee.

## Generate and verify

Install `boundver`, then run from the **boundver repository root**:

```bash
boundver generate \
  --config examples/implicit-and-leaf/boundary.config.json \
  --out examples/implicit-and-leaf/expected.boundary.lock.json \
  --source working-tree
boundver verify \
  --config examples/implicit-and-leaf/boundary.config.json \
  --lock examples/implicit-and-leaf/expected.boundary.lock.json \
  --source working-tree
```

Use `boundver status --source working-tree --config examples/implicit-and-leaf/boundary.config.json --lock examples/implicit-and-leaf/expected.boundary.lock.json` to see the provider coverage states.
