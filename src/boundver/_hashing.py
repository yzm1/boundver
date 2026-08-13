"""Content hashing primitives for boundver."""

import hashlib
import json
import os
import struct
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from ._git import (
    GitSourceSnapshot,
    _capture_git_source_snapshot,
    _git_batch_cat,
    _git_cat_blob,
    _is_git_repository,
    _list_files_for_source,
    _snapshot_files,
    _working_tree_mode,
)
from ._utils import GuardrailError

MAX_HASH_FILES = 50000
MAX_HASH_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB per file guardrail

# v3 retains v2's unambiguous length framing and additionally binds each entry
# to its Git mode and object type.  A chmod or regular-file/symlink transition
# is contract-relevant even when the stored bytes are identical.
HASH_FRAME_VERSION = "boundver-hash/v3"
HASH_DOMAIN_EXACT = "exact-tree"
HASH_DOMAIN_CONTENT_ONLY = "content-only-tree"
HASH_DOMAIN_BOUNDARY = "boundary"
HASH_DOMAIN_BEHAVIOR = "behavior-envelope"

_SEMANTIC_MODE = "semantic"
_SEMANTIC_OBJECT_TYPE = "value"


class _ModeAwareBytes(bytes):
    """Bytes carrying source mode/type through raw provider resolution.

    ``ProviderContext.read_file`` promises bytes, so a bytes subclass preserves
    the public callback contract.  Raw providers return these bytes unchanged
    (apart from line-ending normalization); semantic providers intentionally
    transform them into new semantic values and therefore drop file metadata.
    """

    def __new__(
        cls,
        content: bytes,
        git_mode: str,
        git_object_type: str = "blob",
    ):
        value = super().__new__(cls, content)
        value.git_mode = git_mode
        value.git_object_type = git_object_type
        return value

    def replace(self, old: bytes, new: bytes, count: int = -1):
        replaced = bytes(self).replace(old, new, count)
        return type(self)(replaced, self.git_mode, self.git_object_type)


def canonical_json(obj) -> str:
    """Deterministic JSON for hashing. Sorted keys, compact separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _u64(value: int) -> bytes:
    """Encode a non-negative integer for the hashing wire format."""
    if value < 0 or value >= 2 ** 64:
        raise ValueError(f"Hash frame length is out of range: {value}")
    return struct.pack(">Q", value)


def _hash_framed_entries(
    entries: Iterable[tuple],
    *,
    domain: str,
) -> str:
    """Hash labeled byte entries using the mode-aware v3 wire format.

    ``domain`` separates hashes produced for different purposes (for example,
    an exact tree and a provider boundary).  Entries may be ``(label,
    content)``, where mode/type are read from :class:`_ModeAwareBytes`, or the
    explicit ``(label, mode, object_type, content)`` form used by tree hashing.
    Plain semantic bytes receive a non-file ``semantic/value`` discriminator.
    """
    domain_bytes = domain.encode("utf-8")
    prepared: List[Tuple[bytes, bytes, bytes, bytes]] = []
    for entry in entries:
        if not isinstance(entry, tuple):
            raise TypeError("Hash entries must be tuples")
        if len(entry) == 2:
            label, content = entry
            mode = getattr(content, "git_mode", _SEMANTIC_MODE)
            object_type = getattr(
                content, "git_object_type", _SEMANTIC_OBJECT_TYPE
            )
        elif len(entry) == 4:
            label, mode, object_type, content = entry
        else:
            raise TypeError("Hash entries must contain 2 or 4 fields")
        if not isinstance(label, str):
            raise TypeError("Hash entry labels must be strings")
        if not isinstance(mode, str) or not mode:
            raise TypeError("Hash entry modes must be non-empty strings")
        if not isinstance(object_type, str) or not object_type:
            raise TypeError("Hash entry object types must be non-empty strings")
        if not isinstance(content, bytes):
            raise TypeError("Hash entry content must be bytes")
        prepared.append(
            (
                label.encode("utf-8", errors="surrogateescape"),
                mode.encode("ascii"),
                object_type.encode("ascii"),
                bytes(content),
            )
        )
    prepared.sort(key=lambda item: (item[0], item[1], item[2], item[3]))

    digest = hashlib.sha256()
    magic = HASH_FRAME_VERSION.encode("ascii")
    digest.update(_u64(len(magic)))
    digest.update(magic)
    digest.update(_u64(len(domain_bytes)))
    digest.update(domain_bytes)
    digest.update(_u64(len(prepared)))
    for label_bytes, mode_bytes, type_bytes, content in prepared:
        digest.update(_u64(len(label_bytes)))
        digest.update(label_bytes)
        digest.update(_u64(len(mode_bytes)))
        digest.update(mode_bytes)
        digest.update(_u64(len(type_bytes)))
        digest.update(type_bytes)
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


def _files_from_source(
    repo_root: Path,
    path: str,
    source: str,
    snapshot: Optional[GitSourceSnapshot],
) -> tuple:
    """Return ``(files, snapshot)`` with one immutable Git source."""
    if source in {"head", "index"}:
        captured = snapshot or _capture_git_source_snapshot(repo_root, source)
        if captured.source != source:
            raise ValueError(
                f"Captured {captured.source!r} snapshot cannot serve {source!r}"
            )
        return _snapshot_files(captured, path), captured
    if snapshot is not None:
        if snapshot.source != "index":
            raise ValueError(
                "working-tree hashing requires an index tracking snapshot"
            )
        files = [
            rel
            for rel in _snapshot_files(snapshot, path)
            if (repo_root / rel).exists() or (repo_root / rel).is_symlink()
        ]
        return files, snapshot
    try:
        tracking_snapshot = _capture_git_source_snapshot(repo_root, "index")
    except ValueError:
        if _is_git_repository(repo_root):
            raise
        tracking_snapshot = None
    if tracking_snapshot is not None and (
        tracking_snapshot.entries or tracking_snapshot.head_oid is not None
    ):
        files = [
            rel
            for rel in _snapshot_files(tracking_snapshot, path)
            if (repo_root / rel).exists() or (repo_root / rel).is_symlink()
        ]
        return files, tracking_snapshot
    return sorted(_list_files_for_source(repo_root, path, source)), None


def source_tree_digest(
    repo_root: Path,
    path: str,
    source: str = "head",
    snapshot: Optional[GitSourceSnapshot] = None,
) -> Optional[str]:
    """Canonical SHA-256 digest for a path from HEAD, index, or working tree."""
    if source not in {"head", "index", "working-tree"}:
        raise ValueError(f"Unknown source mode: {source!r}")
    files, captured = _files_from_source(repo_root, path, source, snapshot)
    if not files:
        return None
    for i, rel in enumerate(files):
        _enforce_hash_guardrails(repo_root / rel, i + 1)
    entries: List[tuple] = []
    if source in ("head", "index"):
        assert captured is not None
        git_entries = [captured.entries[rel] for rel in files]
        refs = [entry.oid for entry in git_entries]
        blobs = _git_batch_cat(repo_root, refs)
        for rel, git_entry in zip(files, git_entries):
            if git_entry.object_type != "blob":
                raise ValueError(
                    f"Cannot hash non-blob Git entry at {rel}: "
                    f"{git_entry.object_type} mode {git_entry.mode}"
                )
            content = _normalize_hash_content(blobs[git_entry.oid])
            _enforce_content_size(content, rel)
            entries.append(
                (
                    f"file:{rel}",
                    git_entry.mode,
                    git_entry.object_type,
                    content,
                )
            )
    else:
        for rel in files:
            content = _read_path_content(repo_root, repo_root / rel, source)
            _enforce_content_size(content, rel)
            tracked_entry = captured.entries.get(rel) if captured is not None else None
            mode, object_type = _working_tree_mode(
                repo_root,
                rel,
                tracked_entry,
                core_filemode=(captured.filemode if captured is not None else True),
            )
            entries.append((f"file:{rel}", mode, object_type, content))
    return (
        _hash_framed_entries(entries, domain=HASH_DOMAIN_EXACT)
        if entries
        else None
    )


def _content_only_digest(
    repo_root: Path,
    path: str,
    source: str = "head",
    snapshot: Optional[GitSourceSnapshot] = None,
) -> Optional[str]:
    """SHA-256 of sorted file contents rooted at `path`, without path prefixes.

    Used for vendored copy comparison so the same files at different repo paths
    hash identically when their content is identical.
    """
    if source not in {"head", "index", "working-tree"}:
        raise ValueError(f"Unknown source mode: {source!r}")
    files, captured = _files_from_source(repo_root, path, source, snapshot)
    if not files:
        return None
    base = path.rstrip("/")
    sorted_files = sorted(files)
    for i, repo_rel in enumerate(sorted_files):
        _enforce_hash_guardrails(repo_root / repo_rel, i + 1)
    entries: List[tuple] = []
    if source in ("head", "index"):
        assert captured is not None
        git_entries = [captured.entries[repo_rel] for repo_rel in sorted_files]
        refs = [entry.oid for entry in git_entries]
        blobs = _git_batch_cat(repo_root, refs)
        for repo_rel, git_entry in zip(sorted_files, git_entries):
            try:
                local_rel = Path(repo_rel).relative_to(base).as_posix()
            except ValueError:
                local_rel = repo_rel
            if git_entry.object_type != "blob":
                raise ValueError(
                    f"Cannot hash non-blob Git entry at {repo_rel}: "
                    f"{git_entry.object_type} mode {git_entry.mode}"
                )
            content = _normalize_hash_content(blobs[git_entry.oid])
            _enforce_content_size(content, repo_rel)
            entries.append(
                (
                    f"file:{local_rel}",
                    git_entry.mode,
                    git_entry.object_type,
                    content,
                )
            )
    else:
        for repo_rel in sorted_files:
            try:
                local_rel = Path(repo_rel).relative_to(base).as_posix()
            except ValueError:
                local_rel = repo_rel
            content = _read_path_content(repo_root, repo_root / repo_rel, source)
            _enforce_content_size(content, repo_rel)
            tracked_entry = (
                captured.entries.get(repo_rel) if captured is not None else None
            )
            mode, object_type = _working_tree_mode(
                repo_root,
                repo_rel,
                tracked_entry,
                core_filemode=(captured.filemode if captured is not None else True),
            )
            entries.append((f"file:{local_rel}", mode, object_type, content))
    return (
        _hash_framed_entries(entries, domain=HASH_DOMAIN_CONTENT_ONLY)
        if entries
        else None
    )
