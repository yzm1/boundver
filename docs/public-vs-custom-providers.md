# Public and custom providers

A boundary provider answers one narrow question: which deterministic value
represents this component's declared public artifact? A changed provider digest
means that value changed. It is not, by itself, a compatibility judgment.

## Built-in providers

| Provider | Parses the format? | Interpretation |
|---|---|---|
| `path-hash` | No | Hash arbitrary selected artifacts without format-specific parsing |
| `openapi` / `openapi-raw` | No | Hash selected OpenAPI/Swagger artifacts without parsing them |
| `json-file` / `json-file-raw` | No | Hash selected JSON artifacts without parsing them |
| `python-exports` / `python-exports-raw` | No | Hash selected Python export files |
| `typescript-exports` / `typescript-exports-raw` | No | Hash selected TypeScript declaration or export files |
| `json-canonical` | Yes | Strictly parse JSON and hash a deterministic compact value |
| `openapi-canonical` | Yes | Parse OpenAPI/Swagger and hash a reduced deterministic contract value |
| `implicit` | n/a | Record exact drift while leaving the boundary intentionally partial |
| `leaf` | n/a | Record that the component intentionally publishes no boundary |

The `Parses` column is the column that decides how noisy your boundary gate
will be. The five raw providers differ only in the name recorded in the lock;
they run identical selection and hashing. In particular `python-exports` and
`typescript-exports` do **not** analyse an export surface — a reformat, a new
comment, or a reordered import in a selected file rotates the boundary digest.
Only `json-canonical` and `openapi-canonical` currently reduce a document to
its contract. For Python and TypeScript, either scope the selection narrowly
and accept formatting noise, or supply a trusted custom provider that performs
real export analysis.

The current custom-provider interface executes explicitly trusted Python in the
Boundver process; it is not a plugin sandbox. The proposed future semantic
provider system deliberately uses a separate capability-confined worker and
keeps legacy native extensions in a distinct trust tier. No implementation is
authorized until the [semantic provider RFC](design/semantic-provider-rfc.md)
passes its authoritative acceptance gate. Its accompanying
[threat model](design/semantic-provider-threat-model.md) records the security
and assurance gates.

Use `path-hash` for a format-neutral raw boundary, including SQL migrations,
protobuf definitions, or another artifact type without a named provider. It
does not validate the selected format. The short format-specific raw names are
preserved for compatibility; the `-raw` aliases make their artifact-level
behavior explicit.

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

## Structural range explanations

Historical `boundver review` can ask a provider to explain a changed boundary
after both immutable endpoint locks have passed ordinary verification. This is
an optional interface, separate from fingerprint generation. In v0.15,
`openapi-canonical` is the first built-in implementation. It emits deterministic
added, removed, and changed JSON-pointer rows from the same canonical trees it
hashes. JSON and YAML representations therefore produce the same explanation.
Rows contain structural paths and JSON types but no source values.

The versioned JSON report binds each explanation to both requested refs,
commits, trees, component paths, provider names/versions, and boundary digests. Raw providers
do not implement this interface because parsing them during review would change
their documented byte semantics. Their report is explicitly unavailable.

The optional Python protocol is exported from `boundver.providers` as
`StructuralDiffProvider.structural_diff`: it receives immutable base/target
`ProviderContext` values and one host-owned `StructuralDiffBudget`, and returns
a typed `StructuralDiffResult` composed of `StructuralDocumentDiff` and
`StructuralChange` values. Implementations declare the exact
`boundver-structural-diff/v1` interface identity; the host rejects another or
missing interface version instead of guessing compatibility.
Implementations must spend the supplied aggregate budget before retaining work
or rows. On exhaustion, they raise `GuardrailError`; the host discards that
provider result and emits no partial document rows. Custom implementations are
still trusted in-process Python and require the existing explicit opt-in.

These rows explain why a fingerprint moved. They do not classify breaking
changes or prove compatibility. Keep ecosystem-specific compatibility tools
and consumer tests in the gate.

`openapi` and `openapi-raw` hash bytes and do not require PyYAML.
`openapi-canonical` requires the `yaml` extra when a selected document needs
YAML parsing. `validate-config` preflights selectors that explicitly end in
`.yaml` or `.yml` and reports `boundver[yaml]` before fingerprinting begins.
Directories, extensionless paths, and broad globs are ambiguous until their
tracked files are resolved, so dependency errors for those selectors are
reported during generation or verification. A selector resolving only to JSON
remains dependency-free.

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
`**` segment matches zero or more directories. Wildcard-bearing segments are
limited to 4,096 UTF-8 bytes and 256 wildcard metacharacters, and matching fails
closed on work-budget exhaustion. Use `/`, not `\`. Empty,
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
design also has to bind tool identity and source materialization. See
[reference](reference.md#generated-artifacts-are-not-bound-to-their-generator)
for the freshness check to run instead.

## Provider versions and v3 locks

Each component lock entry records `boundary_provider` and
`boundary_provider_version`. A built-in provider version changes when its
selection, validation, normalization, or output identity changes. Verification
checks that metadata independently of the digest.

The v0.12 built-ins record these versions:

- Raw `path-hash`, `openapi`, `json-file`, `python-exports`, and
  `typescript-exports`: v3.
- `implicit`: v3; `leaf`: v1.
- `json-canonical`: v3; `openapi-canonical`: v4.

The top-level v3 semantic configuration digest also binds provider names,
options, declarations, and custom-provider registration data. A policy change
cannot remain invisible just because current output happens to be equal.

That metadata binding is separate from the selected facet bytes. During a
v3/semantic-config-v1 to v2 regeneration, unchanged source bytes and effective
selectors are expected to retain component facet and slice digest values even
though the semantic-config contract/digest and v0.12 provider metadata change.
Regeneration is still required; this expectation is a review aid, not
permission to relabel the old lock.

Likewise, `json-file-raw` and `path-hash` use the same format-neutral raw
selection contract in v0.12. Changing from the former to the latter with
identical paths, options, and selected bytes is digest-neutral for component
facets and slices, while provider identity and semantic-config metadata change.
If a facet or slice digest also changes, investigate the effective inputs
instead of attributing it to the provider-name transition.

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

Because that opt-in executes arbitrary in-process Python, it is also the memory
and process-isolation boundary. Boundver validates returned entries, labels,
metadata, and errors against hard limits before hashing them, but it cannot
prevent trusted extension code from allocating memory or performing other
Python operations while `resolve()` is running. Run providers that are not
fully trusted in a separately resource-limited process or container instead of
enabling them in the main verification process. Built-in providers enforce
their entry and aggregate budgets while collecting source content.
Configuration is limited to 100 custom-provider declarations, and that limit is
checked before any module import. Provider validation retains no more than 100
bounded error messages. Returned metadata is limited to 64 nesting levels,
100,000 JSON values, and 1 MiB of canonical JSON. The canonical form is emitted
under that byte budget, so repeated large numeric values are rejected before
their complete serialized representation can accumulate in memory.

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
