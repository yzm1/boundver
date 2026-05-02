# Public vs Custom Providers

This document clarifies how provider names should be interpreted in `boundver`.

## Built-in providers are raw boundary providers

The built-ins (`openapi`, `python-exports`, `typescript-exports`, `json-file`) hash declared boundary files as raw bytes.
They **do not** perform semantic API normalization.

Implications:
- formatting-only or comment-only edits can change boundary digest
- ordering changes in serialized artifacts can change boundary digest
- digest change means "declared boundary artifact changed", not necessarily "compatibility changed"

## When to use `custom.*`

Use `custom.*` when your boundary source needs organization-specific extraction logic or domain schema interpretation.

Recommended naming pattern:
- `custom.<org>.<domain>.<format>.v<major>`

Examples:
- `custom.hsl.service-definition.v1`
- `custom.acme.graphql.schema.v2`

Version the provider suffix when extraction rules change so lockfile meaning remains explicit and auditable.

## Safety guidance for proprietary/internal boundaries

- Keep extraction deterministic and read-only.
- Keep provider output stable for equivalent logical inputs.
- Document what files/fields are considered boundary.
- Avoid overloading public provider names for private semantics.

## Migration note

If your team previously treated built-ins as semantic analyzers, migrate to a namespaced `custom.*` provider and document behavior in-repo.
