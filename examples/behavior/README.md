# Behavior tier example

Demonstrates the behavior fingerprint — a broader contract hash that covers configuration and runtime-relevant files in addition to the API boundary.

## Files

- `boundary.config.json` — declares both `boundary` (OpenAPI spec) and `behavior` (spec + config)
- `service/openapi.yaml` — API boundary file
- `service/config.json` — behavioral config (retry policy, rate limits, feature flags)
- `service/version.json` — version source
- `expected.boundary.lock.json` — generated lockfile with all four fingerprints

## Key concepts

The `behavior.paths` list is a **superset** of `boundary.paths`. This gives three useful change classifications:

| What changed | Fingerprint drift | Classification |
|---|---|---|
| Internal implementation only | `exact` | Refactor / internal |
| `config.json` (behavior file, not boundary) | `exact` + `behavior` | Behavioral contract changed (API shape stable) |
| `openapi.yaml` (both boundary and behavior) | `exact` + `behavior` + `boundary` | API contract changed |

## Slices

- `payment-boundary` — tracks API shape only (mode `boundary`)
- `payment-behavior` — tracks observable behavior (mode `behavior`)

## Re-generate expected lockfile

```bash
PYTHONPATH=src python -m boundver.core generate \
  --config examples/behavior/boundary.config.json \
  --out examples/behavior/expected.boundary.lock.json \
  --source working-tree
```
