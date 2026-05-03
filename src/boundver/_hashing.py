"""Content hashing primitives for boundver."""

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from ._git import (
    _git_batch_cat,
    _git_cat_blob,
    _list_files_for_source,
    _to_posix,
)
from ._utils import (
    GuardrailError,
    _is_within,
    _short,
    boundary_provider_name,
)

MAX_HASH_FILES = 50000
MAX_HASH_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB per file guardrail


def canonical_json(obj) -> str:
    """Deterministic JSON for hashing. Sorted keys, compact separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


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
    rel = _to_posix(str(full_path.relative_to(repo_root)))
    if source == "index":
        return _git_cat_blob(repo_root, f":{rel}")
    if source == "head":
        return _git_cat_blob(repo_root, f"HEAD:{rel}")
    if full_path.is_symlink():
        # Hash symlink link-target text (matches what Git stores for symlink blobs).
        try:
            target = os.readlink(full_path)
        except OSError as exc:
            raise ValueError(
                f"Cannot read symlink at {full_path.relative_to(repo_root)}: {exc}"
            ) from exc
        return target.encode("utf-8") if isinstance(target, str) else target
    try:
        sz = full_path.stat().st_size
        if sz > MAX_HASH_FILE_BYTES:
            raise ValueError(
                f"Hash guardrail exceeded: file too large ({sz} bytes) at "
                f"{full_path.relative_to(repo_root)}"
            )
        raw = full_path.read_bytes()
    except FileNotFoundError:
        # File was listed (e.g. by ls-files) but removed before read — treat as empty.
        return b""
    # Normalize CRLF → LF so working-tree hashes match what git stores (git strips CR on commit).
    # This keeps fingerprints stable across platforms and git autocrlf configurations.
    if b"\r\n" in raw and b"\x00" not in raw:
        raw = raw.replace(b"\r\n", b"\n")
    return raw


def source_tree_digest(repo_root: Path, path: str, source: str = "head") -> Optional[str]:
    """Canonical SHA-256 digest for a path from HEAD, index, or working tree."""
    files = sorted(_list_files_for_source(repo_root, path, source))
    if not files:
        return None
    for i, rel in enumerate(files):
        _enforce_hash_guardrails(repo_root / rel, i + 1)
    content_parts: List[bytes] = []
    if source in ("head", "index"):
        obj_prefix = "HEAD:" if source == "head" else ":"
        refs = [f"{obj_prefix}{rel}" for rel in files]
        blobs = _git_batch_cat(repo_root, refs)
        for rel, ref in zip(files, refs):
            content = blobs.get(ref, b"")
            _enforce_content_size(content, rel)
            content_parts.append(f"file:{rel}\n".encode("utf-8"))
            content_parts.append(content)
    else:
        for rel in files:
            content = _read_path_content(repo_root, repo_root / rel, source)
            _enforce_content_size(content, rel)
            content_parts.append(f"file:{rel}\n".encode("utf-8"))
            content_parts.append(content)
    return hashlib.sha256(b"".join(content_parts)).hexdigest() if content_parts else None


def _content_only_digest(repo_root: Path, path: str, source: str = "head") -> Optional[str]:
    """SHA-256 of sorted file contents rooted at `path`, without path prefixes.

    Used for vendored copy comparison so the same files at different repo paths
    hash identically when their content is identical.
    """
    files = _list_files_for_source(repo_root, path, source)
    if not files:
        return None
    base = path.rstrip("/")
    sorted_files = sorted(files)
    for i, repo_rel in enumerate(sorted_files):
        _enforce_hash_guardrails(repo_root / repo_rel, i + 1)
    content_parts: List[bytes] = []
    if source in ("head", "index"):
        obj_prefix = "HEAD:" if source == "head" else ":"
        refs = [f"{obj_prefix}{repo_rel}" for repo_rel in sorted_files]
        blobs = _git_batch_cat(repo_root, refs)
        for repo_rel, ref in zip(sorted_files, refs):
            try:
                local_rel = _to_posix(str(Path(repo_rel).relative_to(base)))
            except ValueError:
                local_rel = _to_posix(repo_rel)
            content = blobs.get(ref, b"")
            _enforce_content_size(content, repo_rel)
            content_parts.append(f"file:{local_rel}\n".encode("utf-8"))
            content_parts.append(content)
    else:
        for repo_rel in sorted_files:
            try:
                local_rel = _to_posix(str(Path(repo_rel).relative_to(base)))
            except ValueError:
                local_rel = _to_posix(repo_rel)
            content = _read_path_content(repo_root, repo_root / repo_rel, source)
            _enforce_content_size(content, repo_rel)
            content_parts.append(f"file:{local_rel}\n".encode("utf-8"))
            content_parts.append(content)
    return hashlib.sha256(b"".join(content_parts)).hexdigest() if content_parts else None
