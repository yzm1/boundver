# Public and custom providers

A boundary provider answers one narrow question: which deterministic value
represents this component's declared public artifact? A changed provider digest
means that value changed. It is not, by itself, a compatibility judgment.

## Built-in providers

| Provider | Interpretation |
|---|---|
| `openapi` / `openapi-raw` | Hash selected OpenAPI/Swagger artifacts without parsing them |
| `json-file` / `json-file-raw` | Hash selected JSON artifacts without parsing them |
| `python-exports` / `python-exports-raw` | Hash selected Python export files |
| `typescript-exports` / `typescript-exports-raw` | Hash selected TypeScript declaration or export files |
| `json-canonical` | Strictly parse JSON and hash a deterministic compact value |
| `openapi-canonical` | Parse OpenAPI/Swagger and hash a reduced deterministic contract value |
| `implicit` | Record exact drift while leaving the boundary intentionally partial |
| `leaf` | Record that the component intentionally publishes no boundary |

The short raw names are preserved for compatibility; the `-raw` aliases make
their artifact-level behavior explicit.

## Raw versus canonical

Raw path providers do not interpret API meaning. Formatting, comments, ordering,
or documentation changes in a selected artifact can rotate the boundary. They
do normalize text CRLF to LF for cross-platform consistency and v3 binds the
selected file's Git mode/type as well as its label and content.

`json-canonical` rejects invalid UTF-8, duplicate object keys, non-finite
numbers, unsupported values, and oversized output. It sorts object keys and
emits deterministic compact JSON. This is boundver's documented canonical form,
not RFC 8785.

`openapi-canonical` validates that each selected document is an OpenAPI or
Swagger object, normalizes supported YAML/JSON input, removes documentation
noise only in recognized annotation positions, and retains extension fields as
contract data. References must be safe same-document fragments; external file
or URL references are rejected because their targets are not part of the
selected digest.

Canonical providers reduce known representation noise. They do not decide
whether a schema evolution is backward compatible, resolve remote dependencies,
or replace an OpenAPI linter and consumer tests.

## One selector grammar

Raw and canonical providers use the same case-sensitive, component-relative
path matcher:

| Selector | Meaning |
|---|---|
| `contract.json` | One literal file |
| `*.json` | Root files only |
| `api/*.yaml` | Direct children only |
| `**/*.json` | Root files and nested files |
| `api/**/*.yaml` | Direct and nested files below `api` |

Within a segment, `*`, `?`, and `[abc]` follow shell-style matching. A complete
`**` segment matches zero or more directories. Use `/`, not `\`. Empty,
absolute, traversing, and redundant paths are rejected. Every declared literal
or pattern must match at least one file in the selected Git source snapshot.

Provider configuration is therefore interchangeable at the selection layer:

```json
{
  "boundary": {
    "provider": "json-canonical",
    "paths": ["schemas/**/*.json"]
  }
}
```

Changing from raw to canonical changes provider identity and lock meaning even
when it currently selects the same files. Regenerate and review the lock.

## Generated provider inputs

A provider fingerprints the artifact it receives. It does not currently know
that `openapi.yaml` was derived from a SAM template, resolvers, source types, or
another file, and it cannot tell whether that output is stale. Give the
generator a deterministic check mode and run it first:

```bash
python ci/generate_platform_openapi.py --check
boundver verify --source head
```

When accepting an index change, stage the derivation source, generated output,
and config before `generate --source index`; then stage the resulting lock and
run `verify --source index`. This keeps all four inputs on one captured staged
snapshot.

There is intentionally no executable `derived_from.command` field. A checked-
out config is not authorization to execute repository commands, and a sound
design also has to bind tool identity and source materialization. Declarative
derived-artifact support remains roadmap work.

## Provider versions and v3 locks

Each component lock entry records `boundary_provider` and
`boundary_provider_version`. A built-in provider version changes when its
selection, validation, normalization, or output identity changes. Verification
checks that metadata independently of the digest.

The top-level v3 semantic configuration digest also binds provider names,
options, declarations, and custom-provider registration data. A policy change
cannot remain invisible just because current output happens to be equal.

## When a custom provider is appropriate

Use a custom provider when the real boundary requires organization-specific
extraction or a domain format that no built-in understands—for example, a
proprietary service-definition schema or a GraphQL normalization policy.

Use a namespaced, versioned name:

- `custom.example.service-definition.v1`
- `custom.acme.graphql.schema.v2`

Bump the provider's own version whenever extraction or canonicalization rules
change. Keep the name stable only when the meaning remains stable.

## Trust model

A custom provider is Python code executed in the boundver process. A checked-out
repository configuration is data, not authorization, so a `providers` entry
alone never imports code. A trusted caller must opt in:

```bash
boundver verify --allow-custom-providers
```

Trusted automation may set `BOUNDVER_ALLOW_CUSTOM_PROVIDERS=1`. Do not set that
globally for workflows that evaluate untrusted forks. The public GitHub Action
does not expose a custom-provider opt-in input; use an explicit trusted install
and CLI invocation when an organization genuinely needs custom code.

A registration entry names a dotted module and class, with an optional expected
custom name:

```json
{
  "providers": [
    {
      "module": "acme_boundaries.service_definition",
      "class": "ServiceDefinitionProvider",
      "name": "custom.acme.service-definition.v1"
    }
  ]
}
```

The module must already be installed in the trusted environment. Provider
Python code is part of that execution environment, not the selected `head` or
`index` source snapshot. Install a pinned provider distribution; do not rely on
an importable module from the checkout or an ambient `PYTHONPATH`. boundver uses
a fresh provider registry per operation so one config cannot leak registrations
into another operation in the same process.

## Custom-provider checklist

- Make resolution deterministic and read-only.
- Return only the documented provider result type and bounded entry data.
- Validate all provider options and fail closed on missing or malformed inputs.
- Use stable semantic labels; include every input that affects output.
- Preserve or deliberately reject file identity when reading source artifacts.
- Keep network access and ambient environment state out of fingerprinting.
- Document what is ignored as well as what is retained.
- Test the provider against `head`, `index`, and `working-tree` source access.
- Treat a provider-version bump and lock regeneration as a reviewed contract
  migration.

If a built-in almost fits but would require undocumented assumptions, prefer a
namespaced custom provider. Do not overload a public built-in name with private
semantics.
