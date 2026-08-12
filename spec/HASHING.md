# Hashing contract (v2)

This document defines the deterministic hashing contract used by Boundver for
newly generated lockfiles. Hashes created with the earlier unframed v1 contract
must be regenerated: v2 intentionally changes every exact, content-only, raw
boundary, and behavior digest.

## Core rules

- Hash algorithm: SHA-256 (lowercase hexadecimal output).
- Path basis: repository-relative Git paths, which use POSIX separators.
  Filesystem fallback paths use `Path.as_posix()`; a literal backslash in a
  POSIX filename is content, not a separator, and remains a backslash.
- Text line endings: CRLF is canonicalized to LF in every source mode when the
  content contains no NUL byte.
- Binary content: bytes containing a NUL byte are hashed without line-ending
  conversion.
- Ordering: entries are sorted by their UTF-8 label bytes, then by content.
- Duplicate labels remain separate entries and are included in the entry count.

Every digest over labeled entries uses the following binary wire format. `u64`
means an unsigned, big-endian 64-bit integer, and `||` means byte
concatenation:

```text
u64(len(magic)) || magic
u64(len(domain)) || domain
u64(entry_count)
for each entry:
  u64(len(label)) || label
  u64(len(content)) || content
```

The magic value is the ASCII bytes `boundver-hash/v2`. Domains and labels are
UTF-8 encoded. On POSIX, undecodable Git filename bytes are preserved through
Python's `surrogateescape` mapping and re-emitted byte-for-byte. Explicit
lengths and the entry count make the representation unambiguous even when a
filename or file content resembles framing data.

## Domains and labels

| Purpose | Domain | Label |
| --- | --- | --- |
| Exact component tree | `exact-tree` | `file:{repository-relative path}` |
| Vendored/content-only tree | `content-only-tree` | `file:{path relative to compared tree}` |
| Boundary and behavior providers | `boundary` | Provider-supplied deterministic label |

Domain separation prevents equal entry sets used for different purposes from
producing the same digest. Provider implementations must use the shared framed
entry helper and must return deterministic labels and raw byte content.

## File enumeration by source mode

- `head`: tracked files from `git ls-tree -r -z --name-only HEAD -- <path>`;
  content comes from the corresponding `HEAD` blobs.
- `index`: tracked files from `git ls-files --cached -z -- <path>`; content
  comes from the corresponding index blobs.
- `working-tree`: the same tracked file set as `index`, excluding tracked files
  currently absent from disk; content comes from disk.

Git filename output is NUL-delimited and decoded with the platform filesystem
codec, so whitespace, newlines, and non-ASCII names are not confused with Git's
human-readable C quoting. A successful empty Git result is authoritative.
Filesystem enumeration is used for `working-tree` only when the Git listing
command itself fails, such as in a non-Git directory, or when the repository
has no first commit and therefore no tracked-file snapshot. It emits a warning.
`head` and `index` require a readable Git source.

## Failure contract

Hashing fails closed. A missing, malformed, truncated, non-blob, or oversized
Git object is an error, as is a working-tree file that disappears between
enumeration and reading. These states never contribute empty bytes. A real
zero-byte file remains valid and hashes normally.

The guardrails are 50,000 files per digest and 50 MiB per file.

## Derived digests

- `exact`: the `exact-tree` digest over all tracked files below the component
  path.
- `boundary`: the `boundary` digest over the provider's resolved
  entries.
- `behavior`: the `boundary` digest over declared behavior entries;
  `null` when not configured.
- `compat`: SHA-256 of the UTF-8 identity string
  `{component_name}@compat:{compat_identity}`. This single-value digest does not
  use entry framing.
- `slice`: SHA-256 over canonical JSON of `{component_name: selected_digest}`,
  where the selected digest is chosen by slice mode (`exact`, `behavior`,
  `boundary`, or `compat`).

## Canonical JSON

- UTF-8 encoded.
- Object keys sorted.
- Compact separators: `,` and `:` (no insignificant whitespace).

## Edge cases

- Empty directories are excluded because Git does not track them.
- File permissions and mode bits are excluded from digest input.
- Symlinks are hashed as link-target text, not dereferenced content.
- Git LFS pointer blobs are hashed from `head` and `index`; a smudged
  `working-tree` file can therefore differ by design.
- Other clean/smudge filters can likewise make working-tree content differ
  from Git blobs. Line-ending canonicalization is the one built-in conversion.
