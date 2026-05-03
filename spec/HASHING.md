# HASHING Contract (v1)

This document defines the deterministic hashing contract for `boundary-lock/v1`.

## Core rules
- Hash algorithm: SHA-256 (hex lowercase output).
- Content basis: raw bytes.
- Path basis: POSIX-normalized path separators (`\\` -> `/`).
- Per-file contribution bytes:

  `b"file:{posix_path}\\n" + file_bytes`

- Component digest input is the concatenation of all per-file contribution bytes in lexicographic path order.

## File enumeration by source mode
- `head`: files from `git ls-tree -r --name-only HEAD <component_path>`.
- `index`: tracked file set for the path from index (current implementation reads blob bytes from `git show :<path>`).
- `working-tree`: tracked files in the repository for the path (excluding untracked files).

> Note: this document defines the target deterministic contract. Any implementation detail drift should be treated as a bug.

## Derived digests
- `exact`: digest over all tracked files under component path.
- `boundary`: same digest algorithm, but restricted to declared `boundary.paths` expansion.
- `compat`: SHA-256 of UTF-8 text identity string:

  `{component_name}@compat:{compat_identity}`

- `behavior`: same digest algorithm, but restricted to declared `behavior.paths` expansion. `null` when not configured.
- `slice`: SHA-256 over canonical JSON of `{component_name: selected_digest}` where `selected_digest` is chosen by slice mode (`exact`, `behavior`, `boundary`, `compat`).

## Canonical JSON
- UTF-8 encoded.
- Object keys sorted.
- Compact separators: `,` and `:` (no insignificant whitespace).

## Edge-case contract
- Binary files: included (raw bytes).
- Empty directories: excluded (not tracked by Git).
- File permissions/mode bits: excluded from digest input.
- Symlinks: hashed as symlink link-target text (not dereferenced file contents).
- Git LFS: pointer blobs are hashed from the selected source state; cross-mode parity can differ when working tree is smudged.
