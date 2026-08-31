# One-minute consumer-impact demo

This demo creates a disposable Git repository with a payments API, generated
SDK, checkout application, and external mobile consumer. It records a clean
baseline, changes the OpenAPI boundary, and asks boundver for the transitive
impact.

From a boundver checkout:

```bash
python scripts/demo_consumer_impact.py
```

The script uses the checked-out source and writes only to a temporary directory.
The important result is:

```text
MISMATCH payments-api.boundary: lockfile=9cf4bb1be668... current=d734da4251c8...
AFFECTED CONSUMERS (TRANSITIVE) payments-api: checkout-web, mobile-app, payments-sdk
```

The process exits successfully only when boundver itself returns boundary-drift
exit code `4`, reports the expected human-readable closure, and returns this
stable `consumer_impact` routing data from `--format json`:

```json
[
  {
    "component": "payments-api",
    "facets": ["boundary"],
    "components": ["checkout-web", "payments-sdk"],
    "external_consumers": ["mobile-app"],
    "transitive": true
  }
]
```

The complete temporary path is printed so the isolated execution location is
visible in the log.

## What happened

1. `generate` recorded the four available identities for each component.
2. The demo added one route to `services/payments/openapi.yaml`.
3. `verify --transitive` detected exact, behavior, and boundary drift.
4. The declared graph routed review toward the SDK, checkout application, and
   external mobile application.

Boundver did **not** decide whether the new route was semantically compatible.
That is where a tool such as oasdiff belongs. Boundver established that the
declared boundary changed and identified which consumers may need work.

Continue with [getting started](getting-started.md) to model your own repository.

For the branch-review version of this workflow, including a reproducible
17-component/six-slice field scenario before and after lock reconciliation, run
`python scripts/demo_range_review.py` and read the
[historical range-review case study](case-study-range-review.md).
