# TypeScript package boundary example

```bash
PYTHONPATH=src python -m boundver.core generate \
  --config examples/typescript-package/boundary.config.json \
  --out examples/typescript-package/expected.boundary.lock.json \
  --source working-tree \
  --deterministic
```
