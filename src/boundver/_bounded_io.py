"""Bounded regular-file reads shared by all disk-backed inputs."""

import io
import os
import stat
from pathlib import Path
from typing import Optional


class FileSizeLimitError(ValueError):
    """A file exceeded the caller's explicit byte limit."""

    def __init__(
        self,
        path: Path,
        size: int,
        limit: int,
        *,
        grew_during_read: bool = False,
    ) -> None:
        super().__init__(f"{path} is at least {size} bytes; limit is {limit} bytes")
        self.path = path
        self.size = size
        self.limit = limit
        self.grew_during_read = grew_during_read


def _is_windows_reparse_point(identity: os.stat_result) -> bool:
    attributes = getattr(identity, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _capture_plain_ancestors(
    path: Path,
    trusted_root: Path,
    *,
    label: str,
    operation: str,
) -> tuple[Path, list[tuple[Path, os.stat_result]]]:
    """Bind a lexical child path to ordinary directories under a trusted root."""
    root = Path(os.path.abspath(trusted_root))
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"Path escapes trusted root while {operation}: {label}"
        ) from exc

    ancestors: list[tuple[Path, os.stat_result]] = []
    current = root
    for part in relative.parts[:-1]:
        if part in {"", ".", ".."}:
            raise ValueError(
                f"Path escapes trusted root while {operation}: {label}"
            )
        current = current / part
        identity = current.lstat()
        if (
            not stat.S_ISDIR(identity.st_mode)
            or stat.S_ISLNK(identity.st_mode)
            or _is_windows_reparse_point(identity)
        ):
            raise ValueError(
                "Unsupported working-tree path ancestor (symlink, reparse "
                f"point, or non-directory) while {operation}: {label}"
            )
        ancestors.append((current, identity))
    return candidate, ancestors


def _verify_plain_ancestors(
    ancestors: list[tuple[Path, os.stat_result]],
    *,
    label: str,
    operation: str,
) -> None:
    """Reject an ancestor replacement during one bounded file read."""
    for path, before in ancestors:
        try:
            after = path.lstat()
        except FileNotFoundError as exc:
            raise ValueError(f"File changed while {operation}: {label}") from exc
        if (
            not stat.S_ISDIR(after.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or _is_windows_reparse_point(after)
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_mode != after.st_mode
        ):
            raise ValueError(f"File changed while {operation}: {label}")


def read_bounded_file(
    path: Path,
    limit: int,
    *,
    path_label: Optional[str] = None,
    operation: str = "reading",
    trusted_root: Optional[Path] = None,
) -> bytes:
    """Read one stable regular file through a fixed-size bounded loop.

    The descriptor is validated before and after reading, one sentinel byte is
    requested beyond ``limit``, and the pathname is checked again before the
    bytes are accepted. When ``trusted_root`` is supplied, every child
    directory is required to remain a plain directory throughout the read.
    This closes stat/read growth, replacement, and metadata races without
    coupling the primitive to a caller's public error vocabulary.
    """
    if limit < 0:
        raise ValueError("File byte limit must be non-negative")
    label = str(path) if path_label is None else path_label
    try:
        ancestors: list[tuple[Path, os.stat_result]] = []
        if trusted_root is not None:
            path, ancestors = _capture_plain_ancestors(
                path,
                trusted_root,
                label=label,
                operation=operation,
            )
        initial = path.lstat()
        if not stat.S_ISREG(initial.st_mode):
            detail = " (symlink)" if stat.S_ISLNK(initial.st_mode) else ""
            raise ValueError(
                f"Unsupported working-tree file type{detail} at {label}"
            )
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"Unsupported working-tree file type at {label}")
            if (
                (initial.st_dev, initial.st_ino)
                != (opened.st_dev, opened.st_ino)
                or initial.st_size != opened.st_size
                or initial.st_mtime_ns != opened.st_mtime_ns
                or stat.S_IMODE(initial.st_mode) != stat.S_IMODE(opened.st_mode)
            ):
                raise ValueError(f"File changed while {operation}: {label}")
            if opened.st_size > limit:
                raise FileSizeLimitError(path, opened.st_size, limit)

            output = io.BytesIO()
            total = 0
            read_chunk_bytes = 64 * 1024
            while True:
                requested = min(read_chunk_bytes, limit - total + 1)
                chunk = stream.read(requested)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise FileSizeLimitError(
                        path,
                        total,
                        limit,
                        grew_during_read=True,
                    )
                output.write(chunk)

            finished = os.fstat(stream.fileno())
            try:
                current = path.lstat()
            except FileNotFoundError as exc:
                raise ValueError(f"File disappeared while {operation}: {label}") from exc
            identity_changed = (
                not stat.S_ISREG(current.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (current.st_dev, current.st_ino)
            )
            content_changed = (
                opened.st_size != finished.st_size
                or opened.st_mtime_ns != finished.st_mtime_ns
                or stat.S_IMODE(opened.st_mode) != stat.S_IMODE(finished.st_mode)
                or finished.st_size != total
                or current.st_size != finished.st_size
                or current.st_mtime_ns != finished.st_mtime_ns
                or stat.S_IMODE(current.st_mode) != stat.S_IMODE(finished.st_mode)
            )
            if identity_changed or content_changed:
                raise ValueError(f"File changed while {operation}: {label}")
            _verify_plain_ancestors(
                ancestors,
                label=label,
                operation=operation,
            )
            return output.getvalue()
    except FileNotFoundError as exc:
        raise ValueError(f"File disappeared while {operation}: {label}") from exc
