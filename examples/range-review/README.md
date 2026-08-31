# Historical range-review fixture

This is a synthetic, non-Boundver monorepo fixture modeled on an anonymized
field configuration. It contains 17 components, six slices, several artifact
families, and a multi-hop consumer graph. The fixture is copied into a
disposable Git repository by:

```bash
python scripts/demo_range_review.py
```

The script changes one application implementation, one behavioral default,
and one canonical OpenAPI boundary. It asserts current drift before lock
reconciliation and historical range evidence after reconciliation. Nothing is
written back to this directory.
