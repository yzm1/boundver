# Consumer-impact example

This example declares a payments API consumed by an SDK, which is consumed by a
checkout application with one external mobile consumer.

Run from the boundver repository root:

```bash
boundver verify \
  --config examples/consumer-impact/boundary.config.json \
  --lock examples/consumer-impact/expected.boundary.lock.json \
  --source working-tree \
  --transitive
```

For a disposable demonstration that changes the API without modifying this
checkout, run `python scripts/demo_consumer_impact.py`.
