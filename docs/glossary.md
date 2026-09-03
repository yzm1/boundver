# Glossary

Boundver uses a small vocabulary to describe what it records and compares.
These definitions apply throughout the documentation and CLI output.

## Baseline

A reviewed record used to tolerate known verification failures while preventing
new ones. A baseline is a temporary adoption aid, not a replacement for
`boundary.lock.json`.

## Boundary

The artifact a component publishes to consumers. Examples include an OpenAPI
document, JSON Schema, declaration file, or any files selected by `path-hash`.
A provider turns the declared boundary into a deterministic digest.

## Closure

A component plus every configured internal consumer reachable from it by
following `consumers` edges. Transitive impact and `closure_of` slices use
this downstream closure.

## Compatibility family

The portion of a component version selected by `compat_mode`. For example,
`major` groups versions by their first number. Boundver reports when that
family changes; it does not infer whether the underlying change is compatible.

## Consumer

A downstream component or external system declared as depending on another
component's contract. Internal `consumers` are validated component names.
`external_consumers` are terminal labels outside the config.

## Drift

A mismatch between an identity recorded in `boundary.lock.json` and the same
identity recomputed from the selected Git snapshot. Drift says that an input
changed, not whether the change is safe.

## Facet

One of four separately recorded component identities:

- `exact`: tracked component content, paths, and file identities under the
  hashing contract; text CRLF/LF are equivalent;
- `behavior`: declared runtime-relevant inputs;
- `boundary`: the declared published artifact; and
- `compat`: the configured compatibility family.

The identities are not fully independent. When behavior tracking is configured,
its digest includes the boundary digest, so boundary drift also changes
`behavior`. Several facets can change in one comparison.

## Implicit boundary

The starter boundary created by `boundver init`. With no paths it provides no
separate boundary digest, so the component is initially tracked through
`exact`. An implicit boundary with paths hashes those files, or you can replace
it with a format-aware provider before gating `boundary`.

## Leaf

A component intentionally declared as publishing no boundary. A leaf can still
provide `exact`, `behavior`, and `compat` identities when their inputs are
configured.

## Lockfile

`boundary.lock.json`, the committed record of component identities, provider
metadata, declared consumer edges, and slice identities for one source
snapshot. It is an integrity and routing record, not a signature.

## Provider

The deterministic extractor that converts declared boundary files into the
entries hashed for a boundary identity. Built-in providers are data-only.
Custom providers are explicitly enabled Python code and must be trusted.

## Ratchet

A migration policy that permits a reviewed set of existing failures but rejects
new failures. Boundver's verification baseline supports this gradual-adoption
workflow.

## Slice

A named digest over one facet from several components. A slice can list members
directly or derive them from a downstream closure. Slices support stable cache
keys and group-level verification.

## Source mode

The Git view used for one operation: committed `head`, staged `index`, or
`working-tree`. Generate and verify against the same mode.

## Vendored copy

A repository-relative directory whose complete tracked tree must have the same
content-only digest as the complete tracked tree under the component root. Text
CRLF and LF line endings are equivalent under that digest. Boundver verifies
the mirror instead of assuming that a generated or copied tree stayed
synchronized.
