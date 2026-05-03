# Implicit and leaf example

## Files
- `boundary.config.json`
- `worker/main.py` (`implicit` boundary)
- `leafsvc/main.py` (`leaf` boundary)
- `expected.boundary.lock.json`

## Re-generate expected lockfile

```bash
PYTHONPATH=src python -m boundver.core generate \
  --config examples/implicit-and-leaf/boundary.config.json \
  --out examples/implicit-and-leaf/expected.boundary.lock.json \
  --source working-tree \

```
