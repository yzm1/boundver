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


def read_bounded_file(
    path: Path,
    limit: int,
    *,
    path_label: Optional[str] = None,
    operation: str = "reading",
) -> bytes:
    """Read one stable regular file through a fixed-size bounded loop.

    The descriptor is validated before and after reading, one sentinel byte is
    requested beyond ``limit``, and the pathname is checked again before the
    bytes are accepted. This closes stat/read growth, replacement, and metadata
    races without coupling the primitive to a caller's public error vocabulary.
    """
    if limit < 0:
        raise ValueError("File byte limit must be non-negative")
    label = str(path) if path_label is None else path_label
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"Unsupported working-tree file type at {label}")
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
            return output.getvalue()
    except FileNotFoundError as exc:
        raise ValueError(f"File disappeared while {operation}: {label}") from exc
