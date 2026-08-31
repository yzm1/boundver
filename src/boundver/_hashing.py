"""Content hashing primitives for boundver."""

import hashlib
import os
import stat
import struct
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

from ._git import (
    GitSourceSnapshot,
    GitTreeEntry,
    _capture_git_source_snapshot,
    _git_cat_blob,
    _iter_git_blobs,
    _is_git_repository,
    _list_files_for_source,
    _snapshot_files,
    _working_tree_mode,
)
from ._utils import (
    GuardrailError,
    SOURCE_MODE_SET,
    _bounded_json_dumps,
    _is_windows_reparse_point,
    _read_bounded_path_bytes as _read_path_bytes_with_limit,
)

MAX_HASH_FILES = 50000
MAX_HASH_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB per file guardrail
MAX_HASH_TOTAL_BYTES = 256 * 1024 * 1024
MAX_HASH_LABEL_BYTES = 16 * 1024
MAX_HASH_TOTAL_LABEL_BYTES = 16 * 1024 * 1024

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
    return _bounded_json_dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


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
    prepared: List[Tuple[bytes, bytes, bytes, bytes]] = []
    total_content_bytes = 0
    total_label_bytes = 0
    for entry in entries:
        if len(prepared) >= MAX_HASH_FILES:
            raise GuardrailError(
                f"Hash guardrail exceeded: >{MAX_HASH_FILES} entries"
            )
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
        label_bytes = label.encode("utf-8", errors="surrogateescape")
        if len(label_bytes) > MAX_HASH_LABEL_BYTES:
            raise GuardrailError(
                "Hash guardrail exceeded: entry label exceeds the "
                f"{MAX_HASH_LABEL_BYTES}-byte limit"
            )
        total_label_bytes += len(label_bytes)
        if total_label_bytes > MAX_HASH_TOTAL_LABEL_BYTES:
            raise GuardrailError(
                "Hash guardrail exceeded: entry labels exceed the "
                f"{MAX_HASH_TOTAL_LABEL_BYTES}-byte aggregate limit"
            )
        _enforce_content_size(content, label)
        total_content_bytes = _enforce_total_content_size(
            total_content_bytes, len(content)
        )
        prepared.append(
            (
                label_bytes,
                mode.encode("ascii"),
                object_type.encode("ascii"),
                content,
            )
        )
    prepared.sort(key=lambda item: (item[0], item[1], item[2], item[3]))

    return _hash_prepared_entries(prepared, domain=domain)


def _new_framed_digest(domain: str, entry_count: int):
    """Initialize a v3 framed digest for a known number of entries."""
    domain_bytes = domain.encode("utf-8")
    digest = hashlib.sha256()
    magic = HASH_FRAME_VERSION.encode("ascii")
    digest.update(_u64(len(magic)))
    digest.update(magic)
    digest.update(_u64(len(domain_bytes)))
    digest.update(domain_bytes)
    digest.update(_u64(entry_count))
    return digest


def _update_framed_digest(
    digest,
    label_bytes: bytes,
    mode_bytes: bytes,
    type_bytes: bytes,
    content: bytes,
) -> None:
    """Append one already ordered entry to a v3 framed digest."""
    digest.update(_u64(len(label_bytes)))
    digest.update(label_bytes)
    digest.update(_u64(len(mode_bytes)))
    digest.update(mode_bytes)
    digest.update(_u64(len(type_bytes)))
    digest.update(type_bytes)
    digest.update(_u64(len(content)))
    digest.update(content)


def _hash_prepared_entries(
    prepared: Iterable[Tuple[bytes, bytes, bytes, bytes]],
    *,
    domain: str,
) -> str:
    """Hash ordered, prepared entries without copying their content."""
    if not isinstance(prepared, list):
        prepared = list(prepared)
    digest = _new_framed_digest(domain, len(prepared))
    for label_bytes, mode_bytes, type_bytes, content in prepared:
        _update_framed_digest(
            digest, label_bytes, mode_bytes, type_bytes, content
        )
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


def _enforce_total_content_size(total: int, next_size: int) -> int:
    """Return the new logical byte total or raise at the aggregate limit."""
    updated = total + next_size
    if updated > MAX_HASH_TOTAL_BYTES:
        raise GuardrailError(
            "Hash guardrail exceeded: hashed content exceeds the "
            f"{MAX_HASH_TOTAL_BYTES}-byte aggregate limit"
        )
    return updated


def _read_bounded_path_bytes(
    full_path: Path,
    path_label: str,
    *,
    max_bytes: Optional[int] = None,
) -> bytes:
    """Read one regular file with a hard ceiling and race detection.

    The size reported before an ordinary ``Path.read_bytes()`` is only a hint:
    a file may grow after ``stat`` and make the subsequent allocation
    unbounded.  Read through the already-open handle in fixed chunks, inspect
    one byte beyond the ceiling, and fail closed when size/mtime/identity
    changes while the snapshot is being consumed.

    This helper is intentionally shared with ``_SourceAccessor`` so exact,
    boundary, behavior, and version reads use precisely the same guarantee.
    """
    effective_limit = (
        MAX_HASH_FILE_BYTES
        if max_bytes is None
        else min(max_bytes, MAX_HASH_FILE_BYTES)
    )
    return _read_path_bytes_with_limit(
        full_path,
        path_label,
        max_bytes=effective_limit,
    )


def _capture_working_tree_ancestors(
    repo_root: Path,
    full_path: Path,
    path_label: str,
) -> List[Tuple[Path, os.stat_result]]:
    """Capture plain directory ancestors for one repository path."""
    try:
        parts = full_path.relative_to(repo_root).parts[:-1]
    except ValueError as exc:
        raise ValueError(f"Path escapes repository: {path_label}") from exc
    ancestors: List[Tuple[Path, os.stat_result]] = []
    current = repo_root
    for part in parts:
        if part in {"", ".", ".."}:
            raise ValueError(f"Path escapes repository: {path_label}")
        current = current / part
        try:
            identity = current.lstat()
        except FileNotFoundError as exc:
            raise ValueError(
                f"File disappeared while hashing: {path_label}"
            ) from exc
        if not stat.S_ISDIR(identity.st_mode) or _is_windows_reparse_point(identity):
            raise ValueError(
                f"Symlink, reparse point, or non-directory ancestor while hashing: "
                f"{path_label}"
            )
        ancestors.append((current, identity))
    return ancestors


def _verify_working_tree_ancestors(
    ancestors: List[Tuple[Path, os.stat_result]],
    path_label: str,
) -> None:
    """Reject an ancestor identity/type change during a working-tree read."""
    for path, before in ancestors:
        try:
            after = path.lstat()
        except FileNotFoundError as exc:
            raise ValueError(
                f"File changed while hashing: {path_label}"
            ) from exc
        if (
            not stat.S_ISDIR(after.st_mode)
            or _is_windows_reparse_point(after)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_mode != after.st_mode
        ):
            raise ValueError(f"File changed while hashing: {path_label}")


def _read_path_content(
    repo_root: Path,
    full_path: Path,
    source: str,
    *,
    max_bytes: int = MAX_HASH_FILE_BYTES,
    tracked_entry: Optional[GitTreeEntry] = None,
    core_filemode: bool = True,
    normalize: bool = True,
) -> bytes:
    """Read one path and bind its bytes and Git mode to one identity window."""
    if max_bytes < 0:
        raise ValueError("File byte limit must be non-negative")
    effective_limit = min(max_bytes, MAX_HASH_FILE_BYTES)
    rel = full_path.relative_to(repo_root).as_posix()
    if source == "index":
        content = _git_cat_blob(repo_root, f":{rel}", max_bytes=effective_limit)
        return _normalize_hash_content(content) if normalize else content
    if source == "head":
        content = _git_cat_blob(
            repo_root, f"HEAD:{rel}", max_bytes=effective_limit
        )
        return _normalize_hash_content(content) if normalize else content

    ancestors = _capture_working_tree_ancestors(repo_root, full_path, rel)
    try:
        before = full_path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"File disappeared while hashing: {rel}") from exc
    mode, object_type = _working_tree_mode(
        repo_root,
        rel,
        tracked_entry,
        core_filemode=core_filemode,
        path_stat=before,
    )

    if stat.S_ISLNK(before.st_mode):
        # Hash symlink link-target text (matches what Git stores for symlink blobs).
        try:
            if before.st_size > effective_limit:
                raise GuardrailError(
                    "Hash guardrail exceeded: file too large "
                    f"({before.st_size} bytes) at {rel}"
                )
            target = os.readlink(full_path)
            after = full_path.lstat()
        except OSError as exc:
            raise ValueError(
                f"Cannot read symlink at {full_path.relative_to(repo_root)}: {exc}"
            ) from exc
        # ``os.readlink`` decodes arbitrary POSIX bytes with surrogateescape;
        # ``os.fsencode`` reverses that mapping exactly.  UTF-8 encoding would
        # reject such legitimate Git symlink targets.
        encoded = os.fsencode(target)
        if len(encoded) > effective_limit:
            raise GuardrailError(
                "Hash guardrail exceeded: file too large "
                f"({len(encoded)} bytes) at {rel}"
            )
        raw = encoded
    else:
        raw = _read_bounded_path_bytes(
            full_path,
            rel,
            max_bytes=effective_limit,
        )
        try:
            after = full_path.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f"File disappeared while hashing: {rel}") from exc

    if (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_mode != after.st_mode
    ):
        raise ValueError(f"File changed while hashing: {rel}")
    _verify_working_tree_ancestors(ancestors, rel)
    _enforce_content_size(raw, rel)
    content = _normalize_hash_content(raw) if normalize else raw
    return _ModeAwareBytes(content, mode, object_type)


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


def _tree_entry_descriptors(
    files: List[str],
    *,
    base: Optional[str] = None,
) -> List[Tuple[bytes, str]]:
    """Return bounded tree labels and paths in hash wire-format order."""
    descriptors: List[Tuple[bytes, str]] = []
    total_label_bytes = 0
    for index, repo_rel in enumerate(files, start=1):
        _enforce_hash_guardrails(Path(repo_rel), index)
        if base is None:
            local_rel = repo_rel
        else:
            try:
                local_rel = Path(repo_rel).relative_to(base).as_posix()
            except ValueError:
                local_rel = repo_rel
        label_bytes = f"file:{local_rel}".encode(
            "utf-8", errors="surrogateescape"
        )
        if len(label_bytes) > MAX_HASH_LABEL_BYTES:
            raise GuardrailError(
                "Hash guardrail exceeded: entry label exceeds the "
                f"{MAX_HASH_LABEL_BYTES}-byte limit"
            )
        total_label_bytes += len(label_bytes)
        if total_label_bytes > MAX_HASH_TOTAL_LABEL_BYTES:
            raise GuardrailError(
                "Hash guardrail exceeded: entry labels exceed the "
                f"{MAX_HASH_TOTAL_LABEL_BYTES}-byte aggregate limit"
            )
        descriptors.append((label_bytes, repo_rel))
    descriptors.sort(key=lambda item: item[0])
    return descriptors


def _stream_tree_digest(
    repo_root: Path,
    descriptors: List[Tuple[bytes, str]],
    source: str,
    captured: Optional[GitSourceSnapshot],
    *,
    domain: str,
    read_blob_fn: Optional[Callable[[str, int], bytes]] = None,
) -> str:
    """Hash a sorted tree while retaining at most duplicated blob content."""
    digest = _new_framed_digest(domain, len(descriptors))
    total_content_bytes = 0

    if source in {"head", "index"}:
        assert captured is not None
        git_entries = [captured.entries[repo_rel] for _, repo_rel in descriptors]
        for (_, repo_rel), entry in zip(descriptors, git_entries):
            if entry.object_type != "blob":
                raise ValueError(
                    f"Cannot hash non-blob Git entry at {repo_rel}: "
                    f"{entry.object_type} mode {entry.mode}"
                )

        oid_counts = Counter(entry.oid for entry in git_entries)
        if read_blob_fn is not None:
            duplicate_cache: dict[str, bytes] = {}
            for (label_bytes, repo_rel), entry in zip(
                descriptors, git_entries
            ):
                if entry.oid in duplicate_cache:
                    content = duplicate_cache[entry.oid]
                else:
                    raw_content = read_blob_fn(
                        entry.oid,
                        MAX_HASH_TOTAL_BYTES - total_content_bytes,
                    )
                    content = _normalize_hash_content(raw_content)
                    if oid_counts[entry.oid] > 1:
                        duplicate_cache[entry.oid] = content
                _enforce_content_size(content, repo_rel)
                total_content_bytes = _enforce_total_content_size(
                    total_content_bytes, len(content)
                )
                _update_framed_digest(
                    digest,
                    label_bytes,
                    entry.mode.encode("ascii"),
                    entry.object_type.encode("ascii"),
                    content,
                )
            return digest.hexdigest()

        blob_iter = _iter_git_blobs(
            repo_root,
            [entry.oid for entry in git_entries],
            max_total_bytes=MAX_HASH_TOTAL_BYTES,
            remaining_bytes=(
                lambda: MAX_HASH_TOTAL_BYTES - total_content_bytes
            ),
        )
        duplicate_cache: dict[str, bytes] = {}
        seen_oids = set()
        try:
            for (label_bytes, repo_rel), entry in zip(
                descriptors, git_entries
            ):
                if entry.oid in seen_oids:
                    content = duplicate_cache[entry.oid]
                else:
                    try:
                        streamed_oid, raw_content = next(blob_iter)
                    except StopIteration as exc:
                        raise ValueError(
                            f"Missing streamed Git blob for {repo_rel}"
                        ) from exc
                    if streamed_oid != entry.oid:
                        raise ValueError(
                            "Git blob stream order disagreed with captured tree"
                        )
                    seen_oids.add(entry.oid)
                    content = _normalize_hash_content(raw_content)
                    if oid_counts[entry.oid] > 1:
                        duplicate_cache[entry.oid] = content
                _enforce_content_size(content, repo_rel)
                total_content_bytes = _enforce_total_content_size(
                    total_content_bytes, len(content)
                )
                _update_framed_digest(
                    digest,
                    label_bytes,
                    entry.mode.encode("ascii"),
                    entry.object_type.encode("ascii"),
                    content,
                )
            try:
                next(blob_iter)
            except StopIteration:
                pass
            else:
                raise ValueError("Unexpected extra blob in Git batch stream")
        finally:
            close = getattr(blob_iter, "close", None)
            if close is not None:
                close()
        return digest.hexdigest()

    for label_bytes, repo_rel in descriptors:
        tracked_entry = (
            captured.entries.get(repo_rel) if captured is not None else None
        )
        content = _read_path_content(
            repo_root,
            repo_root / repo_rel,
            source,
            max_bytes=MAX_HASH_TOTAL_BYTES - total_content_bytes,
            tracked_entry=tracked_entry,
            core_filemode=(captured.filemode if captured is not None else True),
        )
        _enforce_content_size(content, repo_rel)
        total_content_bytes = _enforce_total_content_size(
            total_content_bytes, len(content)
        )
        mode = content.git_mode
        object_type = content.git_object_type
        _update_framed_digest(
            digest,
            label_bytes,
            mode.encode("ascii"),
            object_type.encode("ascii"),
            content,
        )
    return digest.hexdigest()


def source_tree_digest(
    repo_root: Path,
    path: str,
    source: str = "head",
    snapshot: Optional[GitSourceSnapshot] = None,
    read_blob_fn: Optional[Callable[[str, int], bytes]] = None,
) -> Optional[str]:
    """Canonical SHA-256 digest for a path from HEAD, index, or working tree."""
    if source not in SOURCE_MODE_SET:
        raise ValueError(f"Unknown source mode: {source!r}")
    files, captured = _files_from_source(repo_root, path, source, snapshot)
    if not files:
        return None
    descriptors = _tree_entry_descriptors(files)
    return _stream_tree_digest(
        repo_root,
        descriptors,
        source,
        captured,
        domain=HASH_DOMAIN_EXACT,
        read_blob_fn=read_blob_fn,
    )


def _content_only_digest(
    repo_root: Path,
    path: str,
    source: str = "head",
    snapshot: Optional[GitSourceSnapshot] = None,
    read_blob_fn: Optional[Callable[[str, int], bytes]] = None,
) -> Optional[str]:
    """SHA-256 of sorted file contents rooted at `path`, without path prefixes.

    Used for vendored copy comparison so the same files at different repo paths
    hash identically when their content is identical.
    """
    if source not in SOURCE_MODE_SET:
        raise ValueError(f"Unknown source mode: {source!r}")
    files, captured = _files_from_source(repo_root, path, source, snapshot)
    if not files:
        return None
    base = path.rstrip("/")
    descriptors = _tree_entry_descriptors(files, base=base)
    return _stream_tree_digest(
        repo_root,
        descriptors,
        source,
        captured,
        domain=HASH_DOMAIN_CONTENT_ONLY,
        read_blob_fn=read_blob_fn,
    )
