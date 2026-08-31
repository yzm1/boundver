# Comparison and integrations

Boundver is a contract-drift classifier and impact router. The nearest tools
solve adjacent pieces of the workflow rather than the same complete problem.

## Affected project and build graphs

[Nx affected](https://nx.dev/docs/features/ci-features/affected),
[Pants changed target selection](https://www.pantsbuild.org/stable/docs/using-pants/advanced-target-selection),
and [bazel-diff](https://github.com/Tinder/bazel-diff) use project or target
graphs to determine work affected by changes between revisions.

Use them when their graph is authoritative. Add boundver when contracts cross
those graph boundaries, when several build systems coexist, or when CI needs to
distinguish internal, behavioral, boundary, and compatibility-family drift.

## Schema-specific compatibility

[oasdiff](https://github.com/oasdiff/oasdiff),
[Buf breaking change detection](https://buf.build/docs/breaking/), and
[GraphQL Inspector](https://the-guild.dev/graphql/inspector/docs/recipes/pull-requests)
understand the semantics of particular schema families. They can make stronger
compatibility claims than a content fingerprint.

A useful composition is:

1. boundver detects boundary drift and emits affected consumers;
2. the format-specific checker classifies the schema change;
3. CI schedules only the relevant consumer verification.

## Fingerprint locks

[Fiberplane Drift](https://github.com/fiberplane/drift) records AST-based
fingerprints for documentation-to-code relationships. Its lock model is the
closest conceptual relative. Boundver applies recorded identities to repository
components, multiple contract facets, explicit Git source modes, and a consumer
graph.

## Release planning

[Changesets](https://github.com/changesets/changesets) and semantic-release turn
reviewed change intent into package versions and releases. Boundver operates
earlier: it supplies deterministic evidence that a declared contract family
changed. A release planner can consume that signal, but boundver does not choose
or publish a package version by itself.

## Decision guide

| If you need to... | Start with... |
|---|---|
| find affected targets inside one build graph | Nx, Pants, Bazel, or the native build tool |
| prove an OpenAPI, Protobuf, or GraphQL change is compatible | oasdiff, Buf, or GraphQL Inspector |
| coordinate package versions and release notes | Changesets or semantic-release |
| classify heterogeneous declared artifacts and route consumer verification | boundver |

Most mature pipelines use more than one row.

## Reproducible field scenario

The [17-component range-review case study](case-study-range-review.md) shows
that composition in a sanitized non-Boundver fixture: boundver classifies three
different facets and emits direct/transitive consumers, while compatibility
judgment remains explicitly assigned to ecosystem-specific tools and consumer
tests.
