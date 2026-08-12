# OpenAPI boundary

This example uses `service/openapi.yaml` as a service's declared API boundary and `service/version.json` as its compatibility version source.

The raw `openapi` provider reports any artifact change. For a structural fingerprint that ignores selected documentation-only fields, use `openapi-canonical` and explicit boundary files.

## Generate and verify

Install `boundver`, then run from the **boundver repository root**:

```bash
boundver generate \
  --config examples/openapi/boundary.config.json \
  --out examples/openapi/expected.boundary.lock.json \
  --source working-tree
boundver verify \
  --config examples/openapi/boundary.config.json \
  --lock examples/openapi/expected.boundary.lock.json \
  --source working-tree
```

Edit `service/openapi.yaml` to see exact and boundary drift. A detected structural change tells consumers to re-verify; boundver does not itself prove that a client remains compatible.
