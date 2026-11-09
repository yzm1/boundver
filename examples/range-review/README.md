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

The fixture's config schema URL stays pinned to `v0.16.0`. It is part of the
documented Git commit identities, not a current-release example to repin.
The demo still uses the installed tool's packaged schema and normal integrity
checks. An intentional fixture change must update and re-verify its captured
identities and case study together.
