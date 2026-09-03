---
last-verified: 2026-08-31
---

# Case study: contract review across a 17-component monorepo

This case study turns four weeks of donated field notes into a public,
reproducible scenario. The source repository used 17 components, six slices,
and six built-in provider families across APIs, Python and TypeScript packages,
service definitions, workers, and applications. Names, paths, source code, and
organization details have been removed. The checked-in fixture is synthetic;
it preserves the reported topology and review questions, not proprietary
content.

## What the field report observed

These are observations from one Linux repository using boundver 0.14.0, not
universal performance or compatibility claims:

| Observation | Reported result |
|---|---|
| Facet classification | Two accessibility-only React edits changed `exact` while `behavior`, `boundary`, and `compat` stayed unchanged. The team avoided re-verifying four downstream consumers. |
| Heterogeneous extraction | All 17 components extracted successfully across OpenAPI, Python exports, TypeScript exports, JSON, path hashing, and leaf providers without a custom provider. |
| Historical review gap | Answering “what moved on this branch?” required roughly an hour of lockfile hand-diffing. After lock reconciliation, current `consumer_impact` was empty, so reviewers rebuilt the earlier impact manually. |
| Single warm-cache timings | `verify` was reported at 16.9 seconds wall / 3.2 seconds CPU, `status` at 27.6 / 4.6 seconds, and `why`/`explain` at roughly five seconds. These were single runs, not a controlled benchmark. |

The useful result was not “nothing changed.” It was the narrower statement
that implementation bytes changed without moving the declared behavior or
contract boundary. The costly gap was time: current-state verification was
precise, but the pull request and release are ranges.

## Reproduce the sanitized scenario

From a clean boundver checkout, run one command:

```bash
python scripts/demo_range_review.py
```

The script copies a real non-Boundver fixture into a temporary Git repository,
then:

1. records a reconciled baseline for 17 components and six slices;
2. stages one implementation-only edit, one behavioral-default edit, and one
   canonical OpenAPI boundary edit;
3. proves the current staged drift and transitive consumers before the lock is
   regenerated;
4. regenerates and commits the target lock, then proves the target verifies
   cleanly; and
5. runs direct and transitive historical review in both text and JSON, checking
   that the same three transitions remain available after reconciliation.

The Git identities are deterministic and checked by the script. The stable
terminal summary is:

```text
SANITIZED RANGE-REVIEW DEMO
Fixture: 17 components, 6 slices
Before lock reconciliation:
  admin-portal: exact
  analytics-api: exact, behavior
  gateway-api: exact, behavior, boundary
After lock reconciliation: the same three historical transitions remain
Direct consumers: analytics-contracts, platform-client
Transitive consumers: admin-portal, analytics-contracts, checkout-web, insights-web, platform-client, scheduler
External consumers: mobile-app, partner-audit
Structural change: added /paths/~1orders~1{id}
Provenance: base=72dc308d53b356b190e97d8309ee637565499b27 target=70383483c18a1dc57962402a96d0b14a8728c690
Demo passed: current drift and reconciled historical review agree.
```

Each run also prints its own pre-reconciliation verification and direct and
transitive review timings, explicitly labeled “not a benchmark.” CI executes
the script and asserts the fixture size, identities, facet transitions,
consumer closures, changed slices, structural path, text/JSON parity, and exact
endpoint provenance. Documentation therefore cannot quietly outlive the CLI
contract it demonstrates.

## What boundver establishes

For this fixture, boundver establishes deterministic repository facts:

- the application edit moved only `exact`;
- the defaults edit moved `exact` and `behavior` without moving `boundary`;
- the OpenAPI edit moved `exact`, `behavior`, and `boundary`;
- direct and transitive consumer sets are the conservative union of both
  endpoint graphs;
- the OpenAPI provider explains the added canonical path without copying source
  values; and
- every explanation is bound to requested refs, immutable commits and trees,
  provider versions, and boundary digests.

The range command intentionally requires reconciled config/lock pairs at both
committed endpoints. Before reconciliation, ordinary `verify` is the current
integrity signal. After reconciliation, `review BASE..TARGET` preserves the
historical evidence that current verification correctly no longer reports as
outstanding.

## What it does not establish

- Structural OpenAPI output is not proof that a change is backward compatible.
  Run a schema-specific checker such as oasdiff and relevant consumer tests.
- Consumer edges are declared evidence. Boundver validates and traverses them;
  it does not infer every runtime dependency.
- A generated OpenAPI file still needs an independent deterministic freshness
  check before boundver reads it.
- v0.15 structural explanations begin with `openapi-canonical`; raw and other
  providers remain explicit but structurally unsupported.
- This synthetic fixture validates product behavior, not performance at the
  scale or storage layout of every monorepo. The field timings above and each
  demo timing are observations, not promises.

## How it fits with adjacent tools

Affected-build tools such as Nx, Pants, or Bazel remain authoritative for their
own project graphs. Schema tools such as oasdiff, Buf, and GraphQL Inspector can
make stronger format-specific compatibility judgments. A practical pipeline
uses the outputs together:

```text
immutable Git range
  -> boundver facet + consumer plan
  -> format-specific compatibility check
  -> affected consumer suites
```

See [comparison and integrations](comparison.md) for the decision boundary and
[historical range review](reference.md#historical-range-review) for the full
machine contract.
