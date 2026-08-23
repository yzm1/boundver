# boundver examples

These small repositories-in-a-repository demonstrate each built-in boundary style. Run every command from the **boundver repository root** so component paths resolve consistently.

Install the CLI first:

```bash
python -m pip install boundver
```

| Example | What it demonstrates |
|---|---|
| [Consumer impact](consumer-impact/) | Boundary drift routed through a transitive consumer graph |
| [Behavior tier](behavior/) | Separate runtime-behavior and API-boundary fingerprints |
| [OpenAPI](openapi/) | A raw OpenAPI artifact as a service boundary |
| [JSON file](json-file/) | A generic JSON contract |
| [Python package](python-package/) | Public Python exports |
| [TypeScript package](typescript-package/) | A TypeScript export barrel |
| [Implicit and leaf](implicit-and-leaf/) | Gradual adoption and components with no consumers |

Each example contains a config, sample component files, and `expected.boundary.lock.json`. Its README uses the same source mode for generation and verification:

```bash
boundver generate --config examples/EXAMPLE/boundary.config.json \
  --out examples/EXAMPLE/expected.boundary.lock.json \
  --source working-tree
boundver verify --config examples/EXAMPLE/boundary.config.json \
  --lock examples/EXAMPLE/expected.boundary.lock.json \
  --source working-tree
```

The examples show declared-artifact drift, not proof of semantic compatibility. Edit a tracked example file, rerun `verify`, and inspect which facet changes; regenerate afterward to restore the expected lockfile.
