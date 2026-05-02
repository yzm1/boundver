# OpenAPI example

## Files
- `boundary.config.json`
- `service/openapi.yaml`
- `service/version.json`
- `expected.boundary.lock.json`

## Re-generate expected lockfile

```bash
PYTHONPATH=src python -m boundver.core generate \
  --config examples/openapi/boundary.config.json \
  --out examples/openapi/expected.boundary.lock.json \
  --source working-tree \
  --deterministic
```
