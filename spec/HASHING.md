# Hashing contract (v3)

This document defines the deterministic hashing and source-snapshot contract
for `boundary-lock/v3`. A v1 or v2 lock must be regenerated from repository
content: neither older format binds all of the inputs required by v3. A v3
lock carrying `boundver-semantic-config/v1` must also be regenerated because
its digest meaning differs from v2 and cannot be relabelled safely.

## Core rules

- Hash algorithm: SHA-256, emitted as lowercase hexadecimal.
- Path basis: repository-relative Git paths with POSIX separators. Content-only
  comparisons use paths relative to each compared tree.
- File identity: every file entry binds its label/path, canonical Git mode,
  Git object type, and content. A transition among `100644`, `100755`, and
  `120000` therefore changes exact, content-only/vendored, raw boundary, and
  raw behavior fingerprints even when the blob bytes are identical.
- Text line endings: CRLF is canonicalized to LF when content contains no NUL.
- Binary content: bytes containing NUL are hashed without line-ending changes.
- Ordering: core hash inputs are sorted by encoded label, mode, type, then
  content. The framing layer never coalesces identical tuples, so any internal
  duplicates remain distinct and count toward `entry_count`. Provider results
  have a stricter contract: labels must be unique and in strictly increasing
  encoded-label order; duplicate labels are rejected before hashing.

Every entry digest uses this binary format. `u64` is an unsigned big-endian
64-bit integer and `||` means byte concatenation:

```text
u64(len(magic)) || magic
u64(len(domain)) || domain
u64(entry_count)
for each entry:
  u64(len(label)) || label
  u64(len(mode)) || mode
  u64(len(object_type)) || object_type
  u64(len(content)) || content
```

The magic is the ASCII value `boundver-hash/v3`. Domains, labels, modes, and
types are UTF-8 (the canonical Git values are ASCII). On POSIX, undecodable Git
filename bytes round-trip through Python's `surrogateescape` mapping. Explicit
lengths and an entry count make the encoding unambiguous.

Bounded textual Git commands, including repository-root, symbolic-ref, and tag
lookupsâ€”decode with Python's filesystem encoding and `surrogateescape`, never
the user's preferred process locale. This makes their byte transport
round-trippable while retaining readable Unicode on normal filesystems. Raw
filename protocols remain byte-oriented and NUL-delimited. Object IDs, modes,
object types, and status fields are still validated against their strict ASCII
grammars after decoding; surrogate preservation cannot make malformed machine
fields valid.

## Domains, labels, modes, and types

| Purpose | Domain | Label | Mode/type |
| --- | --- | --- | --- |
| Exact component tree | `exact-tree` | `file:{repository-relative path}` | selected file's Git mode/type |
| Vendored/content-only tree | `content-only-tree` | `file:{path relative to compared tree}` | selected file's Git mode/type |
| Raw boundary or raw behavior provider | `boundary` | provider's component-relative file label | selected file's Git mode/type |
| Canonical/semantic provider value | `boundary` | provider's deterministic semantic label | `semantic` / `value` |
| Behavior envelope | `behavior-envelope` | `behavior`, `boundary` | `semantic` / `value` |

Domain separation prevents equal entries used for different purposes from
producing the same digest. Raw path providers must propagate the selected
file's mode and type with its bytes. Providers that parse and canonicalize a
file hash the resulting semantic value, not the source representation, and use
the explicit non-file `semantic/value` identity.

Git modes are the six-character canonical values emitted by Git. Blob modes
supported by normal file hashing are `100644`, `100755`, and `120000`.
Unsupported working-tree file types and non-blob Git tree entries fail closed.

## Declared path and glob grammar

Boundary and behavior path declarations are component-relative POSIX paths and
are matched case-sensitively, independent of the host platform. Matching is
path-segment aware:

- `*`, `?`, and bracket classes such as `[a-z]` match only within one segment;
  they never consume `/`.
- A complete `**` segment matches zero or more complete path segments. Thus
  `**/*.yaml` includes root-level and nested YAML files, while
  `api/**/*.yaml` includes direct children of `api` and descendants.
- Dotfiles are not implicitly excluded; a wildcard can match a leading `.`.
- A literal file selects that file. A literal directory selects its tracked
  descendants. Glob and literal results use the selected source snapshot.
- Every declaration must match at least one selected tracked file. An unmatched
  declaration fails the provider rather than silently weakening the contract.
- The final selection is the deduplicated union of all declarations, sorted by
  component-relative path bytes. Overlapping declarations hash a file once.

Backslashes, absolute paths, empty/whitespace-only values, and `.` or `..` path
segments are invalid declarations. In particular, leading `./` is rejected
rather than assigned a second spelling.

## One source snapshot per operation

A complete generate or verify operation creates one source accessor and reuses
it for the configuration, lockfile (during verification), every component,
version source, provider, vendored copy, and slice. Generation writes its new
lockfile to the working tree only after reading all inputs from the selected
source:

- `head`: resolve `HEAD^{commit}` once, enumerate its immutable tree, and read
  blobs by object ID. A concurrent branch/ref update cannot change the source.
- `index`: run `git write-tree` once, enumerate that immutable tree, and read
  blobs by object ID. A concurrent index update cannot hybridize components.
- `working-tree`: capture one index tree for the tracked path set, then read
  current bytes and mode/type from disk. Tracked paths absent on disk are
  excluded. Disk state is inherently mutable; disappearance during a read is
  an error. An unborn repository with no captured tracked state retains the
  bounded filesystem fallback used for initial setup.

Git-tag version extraction is evaluated against the HEAD commit captured by
the operation. Tags not reachable from that commit are never selected.

For `head` and `index`, the config and verification lock are blobs from the
captured source. Unstaged working-tree versions of those files cannot be mixed
with staged or committed component artifacts. Generation emits its new lock to
the working tree after computation; callers must stage it before index
verification or commit it before head verification. A complete index refresh
therefore stages source/derived output/config, generates, stages the lock, and
then verifies.

## Failure contract

Hashing fails closed. Missing, malformed, truncated, non-blob, or oversized Git
objects are errors, as are unresolved index stages, unsupported filesystem
types, and files that disappear between enumeration and reading. None of these
states contributes empty bytes. A real zero-byte regular file remains valid.

Strict generation also fails when a declared vendored source/copy has no files
in the selected source or when its v3 content-only digest differs from the
source. Such a state is never blessable as a strict lockfile.

`generate --allow-partial` changes only slice assembly: an intentional null
boundary, behavior, or compatibility fingerprint may be stored as that slice
member's input. It does not suppress provider, version, exact, behavior, or
vendored computation errors. Verification treats a facet explicitly selected
by CLI or configured policy as unavailable when either locked or current value
is null and returns usage exit `2`; the policy-free fallback gates all available
facets instead.

The guardrails are 50,000 entries per digest, 50 MiB per entry, and 256 MiB of
logical entry content per digest. Repeated paths that resolve to the same Git
blob count once toward Git transport memory but once per path toward the
logical-content limit. Encoded labels are limited to 16 KiB each and 16 MiB in
aggregate. These limits are enforced while Git blobs and tree entries are
streamed, before a complete component tree can accumulate in memory.

Git source snapshots and filename listings are NUL-parsed as a stream rather
than captured as one subprocess result. A captured repository tree is limited
to 50,000 entries, 16 KiB per encoded path, 16 MiB of encoded paths in
aggregate, and 32 MiB of listing transport. This repository-level snapshot
ceiling is applied before component selection because one operation must use a
single immutable tree. Working-tree regular files are read in fixed-size
chunks with a one-byte oversize sentinel; identity, size, and modification-time
changes during the read fail closed. Symlink target bytes retain the separate
handling described below. Directory symlinks and Windows directory reparse
points (including NTFS junctions) are never traversed as repository content.

Built-in providers apply the same per-entry and aggregate ceilings while they
collect results and, when the host supplies a limit-aware source accessor,
request no more than the remaining aggregate byte budget. Custom providers are
explicitly trusted in-process Python extensions: boundver validates and bounds
their returned result before hashing, but cannot constrain allocations made by
arbitrary provider code while its `resolve()` method is executing.
Provider path declarations are capped at 50,000, built-in validation retains at
most 100 bounded errors, and a configuration may declare at most 100 custom
provider classes; the custom-provider count is rejected before any import.
Custom-provider metadata is limited to 64 nesting levels, 100,000 JSON values,
and 1 MiB of canonical JSON; canonical emission stops at that byte ceiling.

JSON-compatible value trees from configuration, lockfiles, and canonical
JSON/OpenAPI providers are traversed iteratively under a shared limit of 128
levels and 100,000 values. Validation retains at most 100 issues, and a
rendered diagnostic path is capped at 4 KiB. Path nodes share their ancestors
until an error is rendered, so nested long keys cannot create quadratic live
memory before a depth or node guardrail is applied. Untrusted scalar previews
in validation diagnostics are rendered independently of Python's process-wide
integer conversion setting and capped at 500 characters.

## Derived digests

- `exact`: `exact-tree` over every selected file below the component path.
- `boundary`: `boundary` over entries resolved by the configured provider.
- Raw `behavior`: first compute a `boundary` provider digest over declared
  behavior files. Then hash a `behavior-envelope` containing that digest and
  the component's boundary digest. If a provider intentionally has no boundary
  digest, the envelope includes `none:{boundary_status}`. Consequently a
  boundary change always changes a configured behavior fingerprint.
- `compat`: SHA-256 of UTF-8
  `{component_name}@compat:{compat_identity}`. It is a single-value derived
  digest and does not use entry framing.
- `slice`: SHA-256 over canonical JSON of
  `{component_name: selected_digest}`, with the digest selected by slice mode.

Symlinks use mode `120000` and their link-target text as content; they are not
dereferenced. Git LFS pointers and other filtered content use Git blobs for
`head`/`index` and disk bytes for `working-tree`, so those sources can differ by
design. Line-ending canonicalization is the only built-in content conversion.

## Semantic configuration digest

Every v3 lock stores:

```json
{
  "config_contract": "boundver-semantic-config/v2",
  "config_digest": "<sha256>"
}
```

`config_digest` is SHA-256 of the UTF-8 string
`boundver-semantic-config/v2\n` followed by canonical JSON of the semantic
configuration. Canonical JSON sorts object keys, uses compact `,`/`:`
separators, preserves Unicode, and has no insignificant whitespace.

The semantic value covers the project, custom-provider declarations, normalized
defaults (including compatibility mode and default verify facets), every
component path, boundary provider/globs/options, behavior paths, version source,
vendored paths, compatibility inputs, validated internal `consumers`, opaque
`external_consumers`, per-component `verify_facets`, and every explicit or
`closure_of` slice declaration. Set-like lists are sorted and documented
default values are materialized. Presentation-only `$schema` values, component
`ecosystem`, component `note`, boundary `note`, and object insertion order do
not affect it.
Verification compares this digest before
component fingerprints, so a contract-affecting config mutation cannot remain
invisible merely because it happens to select the same current bytes.

## Derived-artifact boundary

The hashing contract binds a declared output artifact, not a relationship
between that output and generator inputs. v3 defines no `derived_from` command,
does not execute repository-configured generators, and does not include ambient
toolchain identity. A deterministic generator freshness check must run before
boundver when a selected artifact is derived. First-class declarative derivation
would require a future contract revision.
