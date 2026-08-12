# Behavior tier example

This example separates a service's observable behavior from its narrower API boundary.

## Files

- `boundary.config.json` declares the component and two slices.
- `service/openapi.yaml` belongs to both the behavior and boundary facets.
- `service/config.json` belongs only to the behavior facet.
- `service/version.json` supplies the compatibility version.
- `expected.boundary.lock.json` records the expected fingerprints.

## Expected classifications

| Change | Facets that drift |
|---|---|
| Internal tracked file | `exact` |
| `service/config.json` | `exact`, `behavior` |
| `service/openapi.yaml` | `exact`, `behavior`, `boundary` |
| Major version in `service/version.json` | `exact`, `compat` |

The `payment-boundary` slice follows API shape; `payment-behavior` follows the broader observable contract.

## Generate and verify

Install `boundver`, then run from the **boundver repository root**:

```bash
boundver generate \
  --config examples/behavior/boundary.config.json \
  --out examples/behavior/expected.boundary.lock.json \
  --source working-tree
boundver verify \
  --config examples/behavior/boundary.config.json \
  --lock examples/behavior/expected.boundary.lock.json \
  --source working-tree
```

These fingerprints detect changes in the declared files; they do not prove runtime compatibility.
