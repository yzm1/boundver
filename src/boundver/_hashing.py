"""Content hashing primitives for boundver."""

import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from ._git import (
    _git_batch_cat,
    _git_cat_blob,
    _list_files_for_source,
)
from ._utils import GuardrailError, _is_within

MAX_HASH_FILES = 50000
MAX_HASH_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB per file guardrail

# ``boundary-lock/v1`` originally concatenated labels and content without
# lengths.  That allowed one file's content to be interpreted as the framing
# for another file.  v2 uses an explicit magic value, a purpose-specific
# domain, an entry count, and uint64 lengths for every variable-size field.
HASH_FRAME_VERSION = "boundver-hash/v2"
HASH_DOMAIN_EXACT = "exact-tree"
HASH_DOMAIN_CONTENT_ONLY = "content-only-tree"
HASH_DOMAIN_BOUNDARY = "boundary"


def canonical_json(obj) -> str:
    """Deterministic JSON for hashing. Sorted keys, compact separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _u64(value: int) -> bytes:
    """Encode a non-negative integer for the v2 hashing wire format."""
    if value < 0 or value >= 2 ** 64:
        raise ValueError(f"Hash frame length is out of range: {value}")
    return struct.pack(">Q", value)


def _hash_framed_entries(
    entries: Iterable[Tuple[str, bytes]],
    *,
    domain: str,
) -> str:
    """Hash labeled byte entries using the unambiguous v2 wire format.

    ``domain`` separates hashes produced for different purposes (for example,
    an exact tree and a provider boundary) even when their entries happen to
    be identical.  Sorting by encoded label and then content makes the helper
    deterministic for every caller, including boundary providers.
    """
    domain_bytes = domain.encode("utf-8")
    prepared: List[Tuple[bytes, bytes]] = []
    for label, content in entries:
        if not isinstance(label, str):
            raise TypeError("Hash entry labels must be strings")
        if not isinstance(content, bytes):
            raise TypeError("Hash entry content must be bytes")
        prepared.append((label.encode("utf-8", errors="surrogateescape"), content))
    prepared.sort(key=lambda entry: (entry[0], entry[1]))

    digest = hashlib.sha256()
    magic = HASH_FRAME_VERSION.encode("ascii")
    digest.update(_u64(len(magic)))
    digest.update(magic)
    digest.update(_u64(len(domain_bytes)))
    digest.update(domain_bytes)
    digest.update(_u64(len(prepared)))
    for label_bytes, content in prepared:
        digest.update(_u64(len(label_bytes)))
        digest.update(label_bytes)
        digest.update(_u64(len(content)))
        digest.update(content)
    return digest.hexdigest()


def _normalize_hash_content(content: bytes) -> bytes:
    """Canonicalize text line endings consistently across source modes."""
    if b"\r\n" in content and b"\x00" not in content:
        return content.replace(b"\r\n", b"\n")
    return content


def _enforce_hash_guardrails(full_path: Path, file_count: int) -> None:
    if file_count > MAX_HASH_FILES:
        raise GuardrailError(f"Hash guardrail exceeded: >{MAX_HASH_FILES} files")


def _enforce_content_size(content: bytes, path_label: str) -> None:
    size = len(content)
    if size > MAX_HASH_FILE_BYTES:
        raise GuardrailError(
            f"Hash guardrail exceeded: file too large ({size} bytes) at {path_label}"
        )


def _read_path_content(repo_root: Path, full_path: Path, source: str) -> bytes:
    rel = full_path.relative_to(repo_root).as_posix()
    if source == "index":
        return _normalize_hash_content(_git_cat_blob(repo_root, f":{rel}"))
    if source == "head":
        return _normalize_hash_content(_git_cat_blob(repo_root, f"HEAD:{rel}"))
    if full_path.is_symlink():
        # Hash symlink link-target text (matches what Git stores for symlink blobs).
        try:
            target = os.readlink(full_path)
        except OSError as exc:
            raise ValueError(
                f"Cannot read symlink at {full_path.relative_to(repo_root)}: {exc}"
            ) from exc
        encoded = target.encode("utf-8") if isinstance(target, str) else target
        return _normalize_hash_content(encoded)
    try:
        sz = full_path.stat().st_size
        if sz > MAX_HASH_FILE_BYTES:
            raise GuardrailError(
                f"Hash guardrail exceeded: file too large ({sz} bytes) at "
                f"{full_path.relative_to(repo_root)}"
            )
        raw = full_path.read_bytes()
    except FileNotFoundError as exc:
        # A listed file disappearing is an incomplete snapshot, not an empty
        # file.  Failing closed prevents a transient race from being blessed.
        raise ValueError(
            f"File disappeared while hashing: {full_path.relative_to(repo_root)}"
        ) from exc
    _enforce_content_size(raw, rel)
    return _normalize_hash_content(raw)


def source_tree_digest(repo_root: Path, path: str, source: str = "head") -> Optional[str]:
    """Canonical SHA-256 digest for a path from HEAD, index, or working tree."""
    if source not in {"head", "index", "working-tree"}:
        raise ValueError(f"Unknown source mode: {source!r}")
    files = sorted(_list_files_for_source(repo_root, path, source))
    if not files:
        return None
    for i, rel in enumerate(files):
        _enforce_hash_guardrails(repo_root / rel, i + 1)
    entries: List[Tuple[str, bytes]] = []
    if source in ("head", "index"):
        obj_prefix = "HEAD:" if source == "head" else ":"
        refs = [f"{obj_prefix}{rel}" for rel in files]
        blobs = _git_batch_cat(repo_root, refs)
        for rel, ref in zip(files, refs):
            content = _normalize_hash_content(blobs[ref])
            _enforce_content_size(content, rel)
            entries.append((f"file:{rel}", content))
    else:
        for rel in files:
            content = _read_path_content(repo_root, repo_root / rel, source)
            _enforce_content_size(content, rel)
            entries.append((f"file:{rel}", content))
    return (
        _hash_framed_entries(entries, domain=HASH_DOMAIN_EXACT)
        if entries
        else None
    )


def _content_only_digest(repo_root: Path, path: str, source: str = "head") -> Optional[str]:
    """SHA-256 of sorted file contents rooted at `path`, without path prefixes.

    Used for vendored copy comparison so the same files at different repo paths
    hash identically when their content is identical.
    """
    if source not in {"head", "index", "working-tree"}:
        raise ValueError(f"Unknown source mode: {source!r}")
    files = _list_files_for_source(repo_root, path, source)
    if not files:
        return None
    base = path.rstrip("/")
    sorted_files = sorted(files)
    for i, repo_rel in enumerate(sorted_files):
        _enforce_hash_guardrails(repo_root / repo_rel, i + 1)
    entries: List[Tuple[str, bytes]] = []
    if source in ("head", "index"):
        obj_prefix = "HEAD:" if source == "head" else ":"
        refs = [f"{obj_prefix}{repo_rel}" for repo_rel in sorted_files]
        blobs = _git_batch_cat(repo_root, refs)
        for repo_rel, ref in zip(sorted_files, refs):
            try:
                local_rel = Path(repo_rel).relative_to(base).as_posix()
            except ValueError:
                local_rel = repo_rel
            content = _normalize_hash_content(blobs[ref])
            _enforce_content_size(content, repo_rel)
            entries.append((f"file:{local_rel}", content))
    else:
        for repo_rel in sorted_files:
            try:
                local_rel = Path(repo_rel).relative_to(base).as_posix()
            except ValueError:
                local_rel = repo_rel
            content = _read_path_content(repo_root, repo_root / repo_rel, source)
            _enforce_content_size(content, repo_rel)
            entries.append((f"file:{local_rel}", content))
    return (
        _hash_framed_entries(entries, domain=HASH_DOMAIN_CONTENT_ONLY)
        if entries
        else None
    )
