# Executive summary

Boundver is a Git-aware lockfile and CI check for contracts shared across
components. It answers two questions:

1. Which declared contracts changed?
2. Which components depend on them?

For example:

```text
payment-api   boundary changed
affected      checkout-web, reporting-worker
```

CI can use that result to run an OpenAPI compatibility check and the two
affected consumer suites instead of every test in the repository.

## How it works

You define components, contract files, and consumer relationships in
`boundary.config.json`. A committed lock file records up to four identities for
each component:

- `exact`: any tracked file or file identity changed;
- `behavior`: a runtime-relevant input changed;
- `boundary`: a declared public artifact changed; and
- `compat`: the configured compatibility family changed.

Facet availability follows the declaration. `behavior` needs declared behavior
paths, `compat` needs a version source, and `boundary` is unavailable for a leaf
or an implicit boundary without paths.

`boundver verify` compares the lock with a chosen Git snapshot and reports the
changed identities and affected consumers. Several identities can change in
one comparison. The result is deterministic and can be reproduced locally.

## What it does not do

boundver does not decide whether an API change is backward compatible. It does
not replace tests, compilers, build graphs, oasdiff, or Buf. It tells CI where
those checks are needed.

## When it helps

Use boundver when a contract can cross a language or build-system boundary—for
example, when a Python service, a TypeScript client, and an OpenAPI document
live in one repository. A single-package project with one compiler and one test
suite may not need it.

The built-in boundver CLI is telemetry-free. It does not send source, usage, or
analytics data.

[Try the one-minute demo](demo.md) or follow [Getting started](getting-started.md).
