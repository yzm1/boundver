# JSON file boundary

This example treats `service/boundary.json` as the declared contract for `svc`. The raw `json-file` provider detects content and formatting changes; choose `json-canonical` when formatting-insensitive structural hashing is more appropriate.

## Files

- `boundary.config.json` declares the `svc` component.
- `service/boundary.json` is the boundary artifact.
- `expected.boundary.lock.json` records the expected fingerprints.

## Generate and verify

Install `boundver`, then run from the **boundver repository root**:

```bash
boundver generate \
  --config examples/json-file/boundary.config.json \
  --out examples/json-file/expected.boundary.lock.json \
  --source working-tree
boundver verify \
  --config examples/json-file/boundary.config.json \
  --lock examples/json-file/expected.boundary.lock.json \
  --source working-tree
```

A stable digest means the declared artifact is stable; it is not a semantic compatibility proof.
