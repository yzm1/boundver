#!/usr/bin/env python3
"""Build and byte-compare the wheel, sdist, and standalone release archive.

The command builds twice, canonicalizes archive metadata, and only copies the
first build to the output directory when every filename and SHA-256 digest
matches. ``SOURCE_DATE_EPOCH`` should be the release commit timestamp. When it
is absent, the timestamp is derived from ``HEAD`` for local packaging smoke
tests.
"""

from __future__ import annotations

import argparse
import binascii
import copy
import gzip
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from zipfile import ZIP_STORED, ZipFile, ZipInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
_ZIP_MIN_EPOCH = 315532800  # 1980-01-01T00:00:00Z
_ZIP_MAX_EPOCH = 4294967294  # latest even timestamp representable by gzip
READ_CHUNK_BYTES = 64 * 1024
MAX_PYPROJECT_BYTES = 1024 * 1024
MAX_SOURCE_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 768 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_PATH_BYTES = 4 * 1024
MAX_TAR_EXTENSION_BYTES = 64 * 1024
MAX_CANONICAL_TAR_BYTES = 768 * 1024 * 1024
MAX_RELEASE_ARTIFACT_BYTES = MAX_CANONICAL_TAR_BYTES + 64 * 1024
MAX_BUILD_DIRECTORY_ENTRIES = 16


def _safe_file_size(path: Path, max_bytes: int, label: str) -> int:
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte limit: {path}")
    return size


def _read_file_bounded(path: Path, max_bytes: int, label: str) -> bytes:
    advertised_size = _safe_file_size(path, max_bytes, label)
    chunks: list[bytes] = []
    total = 0
    with path.open("rb") as stream:
        while total < max_bytes:
            requested = min(READ_CHUNK_BYTES, max_bytes - total)
            chunk = stream.read(requested)
            if len(chunk) > requested:
                raise ValueError(f"{label} exceeded a bounded read request: {path}")
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if stream.read(1):
            raise ValueError(f"{label} exceeds the {max_bytes}-byte limit: {path}")
    if total != advertised_size:
        raise ValueError(f"{label} changed while being read: {path}")
    return b"".join(chunks)


def _read_exact_file_bytes(stream: BinaryIO, size: int, context: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < size:
        requested = min(READ_CHUNK_BYTES, size - total)
        chunk = stream.read(requested)
        if len(chunk) > requested:
            raise ValueError(f"{context} exceeded a bounded read request")
        if not chunk:
            raise ValueError(f"{context} is truncated")
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _validate_archive_path(name: str, archive_name: str) -> None:
    try:
        encoded_length = len(name.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError(f"{archive_name} contains a non-UTF-8 archive path") from error
    stripped = name[:-1] if name.endswith("/") else name
    parts = stripped.split("/")
    if (
        not stripped
        or encoded_length > MAX_ARCHIVE_PATH_BYTES
        or PurePosixPath(name).is_absolute()
        or "\\" in name
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ValueError(
            f"{archive_name} contains an unsafe or overlong archive path: {name!r}"
        )


def _preflight_zip(path: Path) -> None:
    source_size = _safe_file_size(path, MAX_SOURCE_ARCHIVE_BYTES, "source archive")
    tail_size = min(source_size, 65_557)
    with path.open("rb") as stream:
        stream.seek(source_size - tail_size)
        tail = _read_exact_file_bytes(stream, tail_size, f"ZIP footer in {path.name}")
        eocd_index = tail.rfind(b"PK\x05\x06")
        if eocd_index < 0 or len(tail) - eocd_index < 22:
            raise ValueError(f"{path.name} has no valid ZIP footer")
        try:
            eocd = struct.unpack_from("<4s4H2LH", tail, eocd_index)
        except struct.error as error:
            raise ValueError(f"{path.name} has a malformed ZIP footer") from error
        disk_number, central_disk = eocd[1], eocd[2]
        disk_entries, total_entries = eocd[3], eocd[4]
        central_size, central_offset = eocd[5], eocd[6]
        comment_size = eocd[7]
        if eocd_index + 22 + comment_size != len(tail):
            raise ValueError(f"{path.name} has a malformed ZIP footer")
        if (
            disk_number != 0
            or central_disk != 0
            or disk_entries != total_entries
            or total_entries == 0xFFFF
            or central_size == 0xFFFFFFFF
            or central_offset == 0xFFFFFFFF
        ):
            raise ValueError(
                f"{path.name} uses unsupported multi-disk or ZIP64 metadata"
            )
        if total_entries > MAX_ARCHIVE_MEMBERS:
            raise ValueError(
                f"{path.name} exceeds the {MAX_ARCHIVE_MEMBERS}-member archive limit"
            )
        eocd_offset = source_size - tail_size + eocd_index
        if central_offset + central_size > eocd_offset:
            raise ValueError(f"{path.name} has a malformed ZIP central directory")
        central_end = central_offset + central_size
        stream.seek(central_offset)
        total_uncompressed = 0
        for _ in range(total_entries):
            header = _read_exact_file_bytes(
                stream, 46, f"ZIP central directory in {path.name}"
            )
            try:
                fields = struct.unpack("<4s6H3I5H2I", header)
            except struct.error as error:  # pragma: no cover - exact-size invariant
                raise ValueError(
                    f"{path.name} has a malformed ZIP central directory"
                ) from error
            if fields[0] != b"PK\x01\x02":
                raise ValueError(f"{path.name} has a malformed ZIP central directory")
            flags = fields[3]
            compressed_size = fields[8]
            uncompressed_size = fields[9]
            filename_size, extra_size, item_comment_size = fields[10:13]
            local_offset = fields[16]
            if (
                compressed_size == 0xFFFFFFFF
                or uncompressed_size == 0xFFFFFFFF
                or local_offset == 0xFFFFFFFF
            ):
                raise ValueError(f"{path.name} uses unsupported ZIP64 member metadata")
            if filename_size > MAX_ARCHIVE_PATH_BYTES:
                raise ValueError(f"{path.name} contains an overlong archive path")
            filename_bytes = _read_exact_file_bytes(
                stream, filename_size, f"ZIP path in {path.name}"
            )
            encoding = "utf-8" if flags & 0x800 else "cp437"
            try:
                filename = filename_bytes.decode(encoding)
            except UnicodeDecodeError as error:
                raise ValueError(f"{path.name} contains an invalid archive path") from error
            _validate_archive_path(filename, path.name)
            if uncompressed_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError(
                    f"{path.name} contains an oversized archive member: {filename}"
                )
            total_uncompressed += uncompressed_size
            if total_uncompressed > MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError(
                    f"{path.name} exceeds the archive uncompressed-byte limit"
                )
            stream.seek(extra_size + item_comment_size, 1)
            if stream.tell() > central_end:
                raise ValueError(f"{path.name} has a malformed ZIP central directory")
        if stream.tell() != central_end:
            raise ValueError(f"{path.name} has a malformed ZIP central directory")


def _copy_exact(
    source: BinaryIO, destination: BinaryIO, size: int, context: str
) -> None:
    remaining = size
    while remaining:
        requested = min(READ_CHUNK_BYTES, remaining)
        chunk = source.read(requested)
        if len(chunk) > requested:
            raise ValueError(f"{context} exceeded a bounded read request")
        if not chunk:
            raise ValueError(f"{context} ended before its advertised size")
        destination.write(chunk)
        remaining -= len(chunk)
    if source.read(1):
        raise ValueError(f"{context} exceeds its advertised size")


def _project_version(pyproject_path: Path) -> str:
    try:
        text = _read_file_bounded(
            pyproject_path, MAX_PYPROJECT_BYTES, "pyproject"
        ).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"pyproject is not UTF-8: {pyproject_path}") from error
    project_match = re.search(
        r"(?ms)^\[project\]\s*$\n(?P<body>.*?)(?=^\[|\Z)", text
    )
    if project_match is None:
        raise ValueError(f"[project] table not found in {pyproject_path}")
    version_match = re.search(
        r'(?m)^version\s*=\s*["\'](?P<version>[^"\']+)["\']\s*(?:#.*)?$',
        project_match.group("body"),
    )
    if version_match is None:
        raise ValueError(f"static project.version not found in {pyproject_path}")
    return version_match.group("version")


def _release_epoch(explicit: str | None) -> int:
    value = explicit or os.environ.get("SOURCE_DATE_EPOCH")
    if value is None:
        value = subprocess.check_output(
            ["git", "show", "-s", "--format=%ct", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
        ).strip()
    try:
        epoch = int(value)
    except ValueError as exc:
        raise ValueError("SOURCE_DATE_EPOCH must be an integer") from exc
    if not _ZIP_MIN_EPOCH <= epoch <= _ZIP_MAX_EPOCH:
        raise ValueError(
            "SOURCE_DATE_EPOCH must fit the shared ZIP/gzip timestamp range "
            "1980-01-01 through 2106-02-07"
        )
    return epoch - (epoch % 2)


def _zip_timestamp(epoch: int) -> tuple[int, int, int, int, int, int]:
    import time

    return time.gmtime(epoch)[:6]


def _canonicalize_wheel(source: Path, destination: Path, epoch: int) -> None:
    """Rewrite a wheel with stable ordering, timestamps, modes, and storage."""
    timestamp = _zip_timestamp(epoch)
    _preflight_zip(source)
    with ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_MEMBERS:
            raise ValueError(
                f"wheel exceeds the {MAX_ARCHIVE_MEMBERS}-member archive limit: {source}"
            )
        names: set[str] = set()
        total_uncompressed = 0
        for item in entries:
            _validate_archive_path(item.filename, source.name)
            if item.filename in names:
                raise ValueError(f"wheel contains duplicate members: {source}")
            names.add(item.filename)
            if item.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError(
                    f"wheel contains an oversized member: {item.filename}"
                )
            total_uncompressed += item.file_size
            if total_uncompressed > MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError("wheel exceeds the archive uncompressed-byte limit")

        with ZipFile(destination, "w", compression=ZIP_STORED) as canonical:
            for item in sorted(entries, key=lambda entry: entry.filename):
                info = ZipInfo(item.filename, timestamp)
                info.compress_type = ZIP_STORED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.file_size = item.file_size
                with archive.open(item) as member, canonical.open(info, "w") as target:
                    _copy_exact(
                        member,
                        target,
                        item.file_size,
                        f"{item.filename} in {source.name}",
                    )


def _canonical_tar_info(member: tarfile.TarInfo, epoch: int) -> tarfile.TarInfo:
    normalized = copy.copy(member)
    normalized.uid = 0
    normalized.gid = 0
    normalized.uname = ""
    normalized.gname = ""
    normalized.mtime = epoch
    normalized.pax_headers = {}
    if normalized.isdir():
        normalized.mode = 0o755
    elif normalized.isfile():
        normalized.mode = 0o644
    return normalized


def _tar_size(field: bytes, archive_name: str) -> int:
    if field and field[0] & 0x80:
        value = int.from_bytes(bytes([field[0] & 0x7F]) + field[1:], "big")
    else:
        value_bytes = field.rstrip(b"\0 ").lstrip(b" ")
        if not value_bytes:
            return 0
        if any(byte not in b"01234567" for byte in value_bytes):
            raise ValueError(f"{archive_name} contains an invalid TAR size")
        value = int(value_bytes, 8)
    if value < 0:
        raise ValueError(f"{archive_name} contains a negative TAR size")
    return value


def _preflight_tar(path: Path, archive_name: str) -> None:
    source_size = _safe_file_size(
        path, MAX_ARCHIVE_UNCOMPRESSED_BYTES, "uncompressed archive"
    )
    with path.open("rb") as stream:
        count = 0
        aggregate = 0
        while stream.tell() < source_size:
            header = _read_exact_file_bytes(
                stream, 512, f"TAR header in {archive_name}"
            )
            if header == bytes(512):
                break
            count += 1
            if count > MAX_ARCHIVE_MEMBERS:
                raise ValueError(
                    f"{archive_name} exceeds the "
                    f"{MAX_ARCHIVE_MEMBERS}-member archive limit"
                )
            size = _tar_size(header[124:136], archive_name)
            member_type = header[156:157]
            if member_type in (b"x", b"g", b"L", b"K"):
                if size > MAX_TAR_EXTENSION_BYTES:
                    raise ValueError(
                        f"{archive_name} contains oversized TAR extension metadata"
                    )
            else:
                if size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ValueError(
                        f"{archive_name} contains an oversized archive member"
                    )
                aggregate += size
                if aggregate > MAX_ARCHIVE_TOTAL_BYTES:
                    raise ValueError(
                        f"{archive_name} exceeds the archive uncompressed-byte limit"
                    )
            padded_size = ((size + 511) // 512) * 512
            if stream.tell() + padded_size > source_size:
                raise ValueError(f"{archive_name} is truncated")
            stream.seek(padded_size, 1)


class _BoundedReader:
    def __init__(self, stream: BinaryIO, max_bytes: int, context: str) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._context = context
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._max_bytes - self.total
        requested = remaining + 1 if size < 0 else min(size, remaining + 1)
        chunk = self._stream.read(requested)
        if len(chunk) > requested:
            raise ValueError(f"{self._context} exceeded a bounded read request")
        self.total += len(chunk)
        if self.total > self._max_bytes:
            raise ValueError(
                f"{self._context} exceeds the {self._max_bytes}-byte limit"
            )
        return chunk


class _BoundedWriter:
    def __init__(self, stream: BinaryIO, max_bytes: int, context: str) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._context = context

    def write(self, payload: bytes) -> int:
        if self._stream.tell() + len(payload) > self._max_bytes:
            raise ValueError(
                f"{self._context} exceeds the {self._max_bytes}-byte limit"
            )
        written = self._stream.write(payload)
        if written != len(payload):
            raise OSError(f"short write while creating {self._context}")
        return written

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


class _ExactMemberReader:
    def __init__(self, stream: BinaryIO, size: int, context: str) -> None:
        self._stream = stream
        self._remaining = size
        self._context = context

    def read(self, size: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        requested = self._remaining if size < 0 else min(size, self._remaining)
        chunk = self._stream.read(requested)
        if len(chunk) > requested:
            raise ValueError(f"{self._context} exceeded a bounded read request")
        if not chunk:
            raise ValueError(f"{self._context} ended before its advertised size")
        self._remaining -= len(chunk)
        return chunk

    def finish(self) -> None:
        if self._remaining:
            raise ValueError(f"{self._context} ended before its advertised size")
        if self._stream.read(1):
            raise ValueError(f"{self._context} exceeds its advertised size")


def _decompress_gzip_bounded(source: Path, destination: Path) -> None:
    advertised_size = _safe_file_size(
        source, MAX_SOURCE_ARCHIVE_BYTES, "source archive"
    )
    with source.open("rb") as raw_source, destination.open("wb") as target:
        bounded_source = _BoundedReader(
            raw_source, MAX_SOURCE_ARCHIVE_BYTES, f"compressed {source.name}"
        )
        with gzip.GzipFile(fileobj=bounded_source, mode="rb") as compressed:
            total = 0
            while total < MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                requested = min(
                    READ_CHUNK_BYTES, MAX_ARCHIVE_UNCOMPRESSED_BYTES - total
                )
                chunk = compressed.read(requested)
                if len(chunk) > requested:
                    raise ValueError(
                        f"{source.name} exceeded a bounded decompression read"
                    )
                if not chunk:
                    break
                target.write(chunk)
                total += len(chunk)
            if compressed.read(1):
                raise ValueError(
                    f"{source.name} exceeds the "
                    f"{MAX_ARCHIVE_UNCOMPRESSED_BYTES}-byte uncompressed limit"
                )
        if bounded_source.total != advertised_size:
            raise ValueError(f"source archive changed while being read: {source}")


def _canonicalize_sdist(source: Path, destination: Path, epoch: int) -> None:
    """Rewrite a ``.tar.gz`` sdist with stable tar and gzip metadata."""
    with tempfile.TemporaryDirectory(prefix="boundver-sdist-canonical-") as temp:
        temp_root = Path(temp)
        raw_tar = temp_root / "source.tar"
        canonical_tar = temp_root / "canonical.tar"
        _decompress_gzip_bounded(source, raw_tar)
        _preflight_tar(raw_tar, source.name)

        with tarfile.open(raw_tar, "r:") as archive:
            members: list[tarfile.TarInfo] = []
            names: set[str] = set()
            aggregate = 0
            for member in archive:
                if len(members) >= MAX_ARCHIVE_MEMBERS:
                    raise ValueError(
                        f"sdist exceeds the {MAX_ARCHIVE_MEMBERS}-member archive limit: "
                        f"{source}"
                    )
                _validate_archive_path(member.name, source.name)
                if member.name in names:
                    raise ValueError(f"sdist contains duplicate members: {source}")
                names.add(member.name)
                if member.islnk() or member.issym():
                    _validate_archive_path(member.linkname, source.name)
                if not (
                    member.isfile()
                    or member.isdir()
                    or member.islnk()
                    or member.issym()
                ):
                    raise ValueError(
                        f"sdist contains unsupported member type: {member.name}"
                    )
                if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ValueError(
                        f"sdist contains an oversized member: {member.name}"
                    )
                if member.isfile():
                    aggregate += member.size
                    if aggregate > MAX_ARCHIVE_TOTAL_BYTES:
                        raise ValueError(
                            "sdist exceeds the archive uncompressed-byte limit"
                        )
                members.append(member)

            with canonical_tar.open("wb") as raw_output:
                bounded_output = _BoundedWriter(
                    raw_output, MAX_CANONICAL_TAR_BYTES, "canonical TAR"
                )
                with tarfile.open(
                    fileobj=bounded_output, mode="w", format=tarfile.PAX_FORMAT
                ) as canonical:
                    for member in sorted(members, key=lambda item: item.name):
                        normalized = _canonical_tar_info(member, epoch)
                        if not normalized.isfile():
                            canonical.addfile(normalized)
                            continue
                        member_stream = archive.extractfile(member)
                        if member_stream is None:  # pragma: no cover - isfile invariant
                            raise ValueError(
                                f"cannot read {member.name} from {source.name}"
                            )
                        with member_stream:
                            bounded_member = _ExactMemberReader(
                                member_stream,
                                member.size,
                                f"{member.name} in {source.name}",
                            )
                            canonical.addfile(normalized, bounded_member)
                            bounded_member.finish()
        _write_stored_gzip(canonical_tar, destination, epoch)


def _write_stored_gzip(source: Path, destination: Path, epoch: int) -> None:
    size = _safe_file_size(source, MAX_CANONICAL_TAR_BYTES, "canonical TAR")
    crc = 0
    total = 0
    with source.open("rb") as input_stream, destination.open("wb") as output_stream:
        output_stream.write(struct.pack("<BBBBIBB", 0x1F, 0x8B, 8, 0, epoch, 0, 255))
        if size:
            while total < size:
                block_size = min(65_535, size - total)
                block = _read_exact_file_bytes(
                    input_stream, block_size, f"canonical TAR {source}"
                )
                total += len(block)
                output_stream.write(b"\x01" if total == size else b"\x00")
                output_stream.write(
                    struct.pack("<HH", len(block), 0xFFFF ^ len(block))
                )
                output_stream.write(block)
                crc = binascii.crc32(block, crc)
        else:
            output_stream.write(b"\x01\x00\x00\xff\xff")
        if input_stream.read(1):
            raise ValueError(f"canonical TAR changed while being read: {source}")
        output_stream.write(struct.pack("<II", crc & 0xFFFFFFFF, total))


def _stored_gzip(payload: bytes, epoch: int) -> bytes:
    """Encode gzip with uncompressed DEFLATE blocks, without zlib variance."""
    if len(payload) > MAX_CANONICAL_TAR_BYTES:
        raise ValueError(
            f"canonical TAR exceeds the {MAX_CANONICAL_TAR_BYTES}-byte limit"
        )
    result = bytearray(struct.pack("<BBBBIBB", 0x1F, 0x8B, 8, 0, epoch, 0, 255))
    if payload:
        for offset in range(0, len(payload), 65535):
            block = payload[offset : offset + 65535]
            final = offset + len(block) == len(payload)
            result.append(1 if final else 0)
            result.extend(struct.pack("<HH", len(block), 0xFFFF ^ len(block)))
            result.extend(block)
    else:
        result.extend(b"\x01\x00\x00\xff\xff")
    result.extend(
        struct.pack(
            "<II",
            binascii.crc32(payload) & 0xFFFFFFFF,
            len(payload) & 0xFFFFFFFF,
        )
    )
    return bytes(result)


def _clear_backend_state() -> None:
    for path in (REPO_ROOT / "build", REPO_ROOT / "src" / "boundver.egg-info"):
        if path.exists():
            shutil.rmtree(path)


def _bounded_directory_entries(directory: Path) -> list[Path]:
    entries: list[Path] = []
    for path in directory.iterdir():
        if len(entries) >= MAX_BUILD_DIRECTORY_ENTRIES:
            raise ValueError(
                f"{directory} exceeds the "
                f"{MAX_BUILD_DIRECTORY_ENTRIES}-entry build-directory limit"
            )
        entries.append(path)
    return entries


def _single_build(output: Path, version: str, epoch: int) -> None:
    raw = output / "raw"
    canonical = output / "canonical"
    raw.mkdir(parents=True)
    canonical.mkdir()
    _clear_backend_state()

    env = os.environ.copy()
    env.update(
        {
            "SOURCE_DATE_EPOCH": str(epoch),
            "PYTHONHASHSEED": "0",
            "TZ": "UTC",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(raw),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )

    raw_entries = _bounded_directory_entries(raw)
    wheels = [path for path in raw_entries if path.name.endswith(".whl")]
    sdists = [path for path in raw_entries if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError(
            "build must produce exactly one wheel and one .tar.gz sdist; "
            f"found wheels={wheels!r}, sdists={sdists!r}"
        )
    _canonicalize_wheel(wheels[0], canonical / wheels[0].name, epoch)
    _canonicalize_sdist(sdists[0], canonical / sdists[0].name, epoch)
    subprocess.run(
        [
            sys.executable,
            "-I",
            str(REPO_ROOT / "scripts" / "build_standalone.py"),
            "--output",
            str(canonical / f"boundver-{version}.pyz"),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )


def _digests(directory: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(_bounded_directory_entries(directory)):
        if not path.is_file():
            continue
        advertised_size = _safe_file_size(
            path, MAX_RELEASE_ARTIFACT_BYTES, "release artifact"
        )
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as stream:
            while total < MAX_RELEASE_ARTIFACT_BYTES:
                requested = min(
                    READ_CHUNK_BYTES, MAX_RELEASE_ARTIFACT_BYTES - total
                )
                chunk = stream.read(requested)
                if len(chunk) > requested:
                    raise ValueError(
                        f"release artifact exceeded a bounded read request: {path}"
                    )
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
            if stream.read(1):
                raise ValueError(
                    "release artifact exceeds the "
                    f"{MAX_RELEASE_ARTIFACT_BYTES}-byte limit: {path}"
                )
        if total != advertised_size:
            raise ValueError(f"release artifact changed while being read: {path}")
        digests[path.name] = digest.hexdigest()
    return digests


def _copy_file_bounded(source: Path, destination: Path) -> None:
    advertised_size = _safe_file_size(
        source, MAX_RELEASE_ARTIFACT_BYTES, "release artifact"
    )
    total = 0
    with source.open("rb") as input_stream, destination.open("wb") as output_stream:
        while total < MAX_RELEASE_ARTIFACT_BYTES:
            requested = min(
                READ_CHUNK_BYTES, MAX_RELEASE_ARTIFACT_BYTES - total
            )
            chunk = input_stream.read(requested)
            if len(chunk) > requested:
                raise ValueError(
                    f"release artifact exceeded a bounded read request: {source}"
                )
            if not chunk:
                break
            written = output_stream.write(chunk)
            if written != len(chunk):
                raise OSError(f"short write while copying release artifact: {destination}")
            total += len(chunk)
        if input_stream.read(1):
            raise ValueError(
                "release artifact exceeds the "
                f"{MAX_RELEASE_ARTIFACT_BYTES}-byte limit: {source}"
            )
    if total != advertised_size:
        raise ValueError(f"release artifact changed while being copied: {source}")


def build_release_artifacts(output: Path, epoch: int) -> dict[str, str]:
    version = _project_version(REPO_ROOT / "pyproject.toml")
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="boundver-release-build-") as temp:
        temp_root = Path(temp)
        first = temp_root / "first"
        second = temp_root / "second"
        _single_build(first, version, epoch)
        _single_build(second, version, epoch)
        first_digests = _digests(first / "canonical")
        second_digests = _digests(second / "canonical")
        if first_digests != second_digests:
            names = sorted(set(first_digests) | set(second_digests))
            details = "\n".join(
                f"  {name}: {first_digests.get(name, '<missing>')} != "
                f"{second_digests.get(name, '<missing>')}"
                for name in names
                if first_digests.get(name) != second_digests.get(name)
            )
            raise RuntimeError(f"release builds are not byte-reproducible:\n{details}")
        for name in sorted(first_digests):
            _copy_file_bounded(first / "canonical" / name, output / name)
    _clear_backend_state()
    return first_digests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    parser.add_argument(
        "--source-date-epoch",
        help="Release timestamp (defaults to SOURCE_DATE_EPOCH, then HEAD)",
    )
    args = parser.parse_args()
    try:
        epoch = _release_epoch(args.source_date_epoch)
        digests = build_release_artifacts(args.output_dir.resolve(), epoch)
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Reproduced {len(digests)} release artifacts at epoch {epoch}:")
    for name, digest in sorted(digests.items()):
        print(f"  {digest}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
