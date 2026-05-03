# JSON file boundary example

## Files
- `boundary.config.json`
- `service/boundary.json`
- `expected.boundary.lock.json`

## Re-generate expected lockfile

```bash
PYTHONPATH=src python -m boundver.core generate \
  --config examples/json-file/boundary.config.json \
  --out examples/json-file/expected.boundary.lock.json \
  --source working-tree \

```
