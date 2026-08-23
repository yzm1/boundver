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
exit code `4` and reports the expected consumer closure. The complete temporary
path is printed so the isolated execution location is visible in the log.

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
