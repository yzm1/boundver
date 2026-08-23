# Why boundver?

Use boundver when a repository contains components with downstream consumers,
but no single compiler or build graph can explain whether a change was internal,
behavioral, boundary-facing, or a compatibility-family change.

## The problem it solves

Polyglot repositories often carry contracts as files rather than as one
type-checked program:

- a service publishes OpenAPI generated from infrastructure code;
- an application consumes JSON Schema owned by another team;
- a package exposes declarations while runtime defaults live elsewhere;
- a shared platform component has consumers in several build systems.

Git can say that bytes changed. A schema-specific checker may say whether one
particular format is backward compatible. A build system may determine which
tasks are affected. None of those signals alone records the repository's
declared contract identity and consumer relationships in a portable lockfile.

boundver fills that gap. It fingerprints declared tracked inputs, stores those
identities in `boundary.lock.json`, and classifies later drift into four facets:

| Facet | Signal |
|---|---|
| `exact` | Tracked component bytes or file identities changed. |
| `behavior` | Declared observable behavior inputs changed. |
| `boundary` | The configured public artifact changed. |
| `compat` | The configured compatibility family changed. |

Boundary and compatibility drift can also report direct or transitive consumers
from the declared graph. That output is suitable for review gates, targeted
verification, and integration-cache keys.

## What boundver does not claim

boundver detects **drift in declared artifacts**. It does not prove that a
change is backward compatible, execute consumer tests, discover every dependency,
or infer runtime behavior from source code.

A clean result means that the selected Git snapshot agrees with the recorded
identities. It does not mean that every possible consumer is safe.

Use a semantic checker when one is available for the contract format, and use
the build system to schedule work it can discover. boundver supplies the
language-neutral classification and routing layer between those systems.

## Comparison

| Tool family | What it answers well | How boundver differs |
|---|---|---|
| Nx `affected`, Pants changed targets, `bazel-diff` | Which projects or build targets are affected by a revision? | boundver is build-system-independent and classifies declared contract facets rather than tasks. |
| oasdiff, Buf breaking, GraphQL Inspector | Is this OpenAPI, Protobuf, or GraphQL change semantically breaking? | boundver supports heterogeneous artifact families but intentionally does not replace format-specific compatibility analysis. |
| Changesets, semantic-release | Which package version or release should be produced? | boundver detects and routes drift before release planning or promotion. |
| Fiberplane Drift | Did a documentation-to-code binding fingerprint change? | Both use recorded fingerprints; boundver focuses on component contracts, facets, Git snapshots, and consumer impact. |

These tools are complements rather than mutually exclusive choices. A practical
pipeline can use boundver to identify a changed OpenAPI boundary and affected
consumers, then run oasdiff and only the relevant consumer suites.

See the [comparison guide](comparison.md) for integration patterns and source
links.

## When to use it

boundver is a good fit when:

- contracts cross languages, build systems, or team boundaries;
- generated or hand-written artifacts need a reviewed baseline;
- internal refactors should not be treated as public-contract changes;
- CI needs deterministic, source-controlled change classification;
- downstream relationships are known and can be declared explicitly.

It is probably unnecessary when one compiler and one dependency graph already
answer all of those questions, or when the repository has no meaningful
consumer-facing artifacts.

## Current scope

- The graph is declared and validated; dependency discovery remains the build
  system's responsibility.
- Generated artifacts need a separate deterministic freshness check before
  boundver verification.
- Canonical providers remove documented formatting or presentation noise; they
  do not establish backward compatibility.
- Trusted custom providers can fingerprint deterministic derived output, but a
  checked-out configuration cannot authorize executable imports by itself.
- Files omitted from component, behavior, or boundary selectors are outside the
  corresponding identity.

## Adoption pattern

1. Start with one component and gate `exact`.
2. Declare its real boundary artifact and choose a provider.
3. Add behavior inputs and a version source where they carry useful signals.
4. Declare direct consumers and inspect transitive impact.
5. Add schema-specific compatibility checks and targeted consumer suites where
   needed.
6. Expand one component at a time.

Continue with [getting started](getting-started.md), the
[runnable demo](demo.md), or the [gradual adoption guide](gradual-adoption.md).
