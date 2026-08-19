#!/usr/bin/env python3
"""Fail-closed package-index and provenance checks for the release workflow.

The script intentionally uses only the Python standard library.  It compares
the local wheel and sdist with one package-index release by filename, byte length,
and SHA-256, then downloads every advertised file and hashes the bytes again.
It also retrieves bounded TestPyPI Integrity API statements for independent
attestation verification by the workflow's hash-locked tooling.
"""

from __future__ import annotations

import argparse
import email.parser
import gzip
import hashlib
import json
import math
import re
import struct
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Mapping, Sequence


DEFAULT_API_BASE = "https://test.pypi.org/pypi"
DEFAULT_DOWNLOAD_ORIGIN = "https://test-files.pythonhosted.org"
DEFAULT_INTEGRITY_ORIGIN = "https://test.pypi.org"
USER_AGENT = "boundver-release-verifier/1"
READ_CHUNK_BYTES = 64 * 1024
MAX_INDEX_METADATA_BYTES = 8 * 1024 * 1024
MAX_PROVENANCE_BYTES = 8 * 1024 * 1024
MAX_DISTRIBUTION_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 768 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_PATH_BYTES = 4 * 1024
MAX_TAR_EXTENSION_BYTES = 64 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_INDEX_FILES = 256
MAX_DISTRIBUTION_FILENAME_BYTES = 1024
MAX_JSON_INTEGER_DIGITS = 4300
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PROJECT_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


class ReleaseVerificationError(ValueError):
    """A permanent integrity or release-shape failure."""


class ReleaseIncompleteError(RuntimeError):
    """A release is missing or has not exposed every candidate file yet."""


class ReleaseNetworkError(RuntimeError):
    """A remote request failed and may succeed on a later attempt."""


def _bounded_json_int(value: str) -> int:
    """Parse a JSON integer independently of Python's mutable digit limit."""
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None:
        raise ValueError("invalid JSON integer")
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(
            "JSON integer exceeds the "
            f"{MAX_JSON_INTEGER_DIGITS}-decimal-digit limit"
        )
    result = 0
    for offset in range(0, len(digits), 9):
        chunk = digits[offset : offset + 9]
        result = result * (10 ** len(chunk)) + int(chunk)
    return -result if negative else result


def _bounded_json_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number is not supported")
    return result


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON constant is not supported")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key is not supported")
        result[key] = value
    return result


def _strict_json_loads(document: str) -> Any:
    return json.loads(
        document,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
        parse_float=_bounded_json_float,
        parse_int=_bounded_json_int,
    )


@dataclass(frozen=True)
class DistributionFile:
    filename: str
    sha256: str
    size: int
    url: str | None = None


def _normalized_project(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _safe_file_size(path: Path, max_bytes: int, label: str) -> int:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ReleaseVerificationError(f"cannot inspect {label} {path}: {error}") from error
    if size > max_bytes:
        raise ReleaseVerificationError(
            f"{label} exceeds the {max_bytes}-byte limit: {path}"
        )
    return size


def _sha256_file(path: Path) -> tuple[str, int]:
    advertised_size = _safe_file_size(
        path, MAX_DISTRIBUTION_BYTES, "distribution"
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while total < MAX_DISTRIBUTION_BYTES:
                requested = min(
                    READ_CHUNK_BYTES, MAX_DISTRIBUTION_BYTES - total
                )
                chunk = stream.read(requested)
                if len(chunk) > requested:
                    raise ReleaseVerificationError(
                        f"distribution exceeded the bounded read request: {path}"
                    )
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
            if stream.read(1):
                raise ReleaseVerificationError(
                    "distribution exceeds the "
                    f"{MAX_DISTRIBUTION_BYTES}-byte limit: {path}"
                )
    except ReleaseVerificationError:
        raise
    except OSError as error:
        raise ReleaseVerificationError(f"cannot read distribution {path}: {error}") from error
    if total != advertised_size:
        raise ReleaseVerificationError(f"distribution changed while being read: {path}")
    return digest.hexdigest(), total


def _validate_archive_path(name: str, archive_name: str) -> None:
    try:
        encoded_length = len(name.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ReleaseVerificationError(
            f"{archive_name} contains a non-UTF-8 archive path"
        ) from error
    stripped = name[:-1] if name.endswith("/") else name
    parts = stripped.split("/")
    if (
        not stripped
        or encoded_length > MAX_ARCHIVE_PATH_BYTES
        or PurePosixPath(name).is_absolute()
        or "\\" in name
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ReleaseVerificationError(
            f"{archive_name} contains an unsafe or overlong archive path: {name!r}"
        )


def _filename_within_limit(name: str) -> bool:
    try:
        return len(name.encode("utf-8")) <= MAX_DISTRIBUTION_FILENAME_BYTES
    except UnicodeEncodeError:
        return False


def _exact_read(stream: BinaryIO, size: int, context: str) -> bytes:
    if size > MAX_METADATA_BYTES:
        raise ReleaseVerificationError(
            f"{context} exceeds the {MAX_METADATA_BYTES}-byte metadata limit"
        )
    chunks: list[bytes] = []
    total = 0
    while total < size:
        requested = min(READ_CHUNK_BYTES, size - total)
        chunk = stream.read(requested)
        if len(chunk) > requested:
            raise ReleaseVerificationError(f"{context} exceeded a bounded read request")
        if not chunk:
            raise ReleaseVerificationError(f"{context} ended before its advertised size")
        chunks.append(chunk)
        total += len(chunk)
    if stream.read(1):
        raise ReleaseVerificationError(f"{context} exceeds its advertised size")
    return b"".join(chunks)


def _read_exact_file_bytes(stream: BinaryIO, size: int, context: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < size:
        requested = min(READ_CHUNK_BYTES, size - total)
        chunk = stream.read(requested)
        if len(chunk) > requested:
            raise ReleaseVerificationError(f"{context} exceeded a bounded read request")
        if not chunk:
            raise ReleaseVerificationError(f"{context} is truncated")
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _preflight_zip(path: Path) -> None:
    source_size = _safe_file_size(path, MAX_DISTRIBUTION_BYTES, "distribution")
    tail_size = min(source_size, 65_557)
    try:
        with path.open("rb") as stream:
            stream.seek(source_size - tail_size)
            tail = _read_exact_file_bytes(stream, tail_size, f"ZIP footer in {path.name}")
            eocd_index = tail.rfind(b"PK\x05\x06")
            if eocd_index < 0 or len(tail) - eocd_index < 22:
                raise ReleaseVerificationError(f"{path.name} has no valid ZIP footer")
            eocd = struct.unpack_from("<4s4H2LH", tail, eocd_index)
            disk_number, central_disk = eocd[1], eocd[2]
            disk_entries, total_entries = eocd[3], eocd[4]
            central_size, central_offset = eocd[5], eocd[6]
            comment_size = eocd[7]
            if eocd_index + 22 + comment_size != len(tail):
                raise ReleaseVerificationError(f"{path.name} has a malformed ZIP footer")
            if (
                disk_number != 0
                or central_disk != 0
                or disk_entries != total_entries
                or total_entries == 0xFFFF
                or central_size == 0xFFFFFFFF
                or central_offset == 0xFFFFFFFF
            ):
                raise ReleaseVerificationError(
                    f"{path.name} uses unsupported multi-disk or ZIP64 metadata"
                )
            if total_entries > MAX_ARCHIVE_MEMBERS:
                raise ReleaseVerificationError(
                    f"{path.name} exceeds the {MAX_ARCHIVE_MEMBERS}-member archive limit"
                )
            eocd_offset = source_size - tail_size + eocd_index
            if central_offset + central_size > eocd_offset:
                raise ReleaseVerificationError(
                    f"{path.name} has a malformed ZIP central directory"
                )
            central_end = central_offset + central_size
            stream.seek(central_offset)
            total_uncompressed = 0
            for _ in range(total_entries):
                header = _read_exact_file_bytes(
                    stream, 46, f"ZIP central directory in {path.name}"
                )
                fields = struct.unpack("<4s6H3I5H2I", header)
                if fields[0] != b"PK\x01\x02":
                    raise ReleaseVerificationError(
                        f"{path.name} has a malformed ZIP central directory"
                    )
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
                    raise ReleaseVerificationError(
                        f"{path.name} uses unsupported ZIP64 member metadata"
                    )
                if filename_size > MAX_ARCHIVE_PATH_BYTES:
                    raise ReleaseVerificationError(
                        f"{path.name} contains an overlong archive path"
                    )
                filename_bytes = _read_exact_file_bytes(
                    stream, filename_size, f"ZIP path in {path.name}"
                )
                encoding = "utf-8" if flags & 0x800 else "cp437"
                try:
                    filename = filename_bytes.decode(encoding)
                except UnicodeDecodeError as error:
                    raise ReleaseVerificationError(
                        f"{path.name} contains an invalid archive path"
                    ) from error
                _validate_archive_path(filename, path.name)
                if uncompressed_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ReleaseVerificationError(
                        f"{path.name} contains an oversized archive member: {filename}"
                    )
                total_uncompressed += uncompressed_size
                if total_uncompressed > MAX_ARCHIVE_TOTAL_BYTES:
                    raise ReleaseVerificationError(
                        f"{path.name} exceeds the archive uncompressed-byte limit"
                    )
                stream.seek(extra_size + item_comment_size, 1)
                if stream.tell() > central_end:
                    raise ReleaseVerificationError(
                        f"{path.name} has a malformed ZIP central directory"
                    )
            if stream.tell() != central_end:
                raise ReleaseVerificationError(
                    f"{path.name} has a malformed ZIP central directory"
                )
    except ReleaseVerificationError:
        raise
    except (OSError, struct.error) as error:
        raise ReleaseVerificationError(
            f"cannot inspect ZIP structure in {path.name}: {error}"
        ) from error


def _tar_size(field: bytes, archive_name: str) -> int:
    if field and field[0] & 0x80:
        value = int.from_bytes(bytes([field[0] & 0x7F]) + field[1:], "big")
    else:
        value_bytes = field.rstrip(b"\0 ").lstrip(b" ")
        if not value_bytes:
            return 0
        if any(byte not in b"01234567" for byte in value_bytes):
            raise ReleaseVerificationError(
                f"{archive_name} contains an invalid TAR size"
            )
        value = int(value_bytes, 8)
    if value < 0:
        raise ReleaseVerificationError(f"{archive_name} contains a negative TAR size")
    return value


def _preflight_tar(path: Path, archive_name: str) -> None:
    source_size = _safe_file_size(
        path, MAX_ARCHIVE_UNCOMPRESSED_BYTES, "uncompressed archive"
    )
    try:
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
                    raise ReleaseVerificationError(
                        f"{archive_name} exceeds the "
                        f"{MAX_ARCHIVE_MEMBERS}-member archive limit"
                    )
                size = _tar_size(header[124:136], archive_name)
                member_type = header[156:157]
                if member_type in (b"x", b"g", b"L", b"K"):
                    if size > MAX_TAR_EXTENSION_BYTES:
                        raise ReleaseVerificationError(
                            f"{archive_name} contains oversized TAR extension metadata"
                        )
                else:
                    if size > MAX_ARCHIVE_MEMBER_BYTES:
                        raise ReleaseVerificationError(
                            f"{archive_name} contains an oversized archive member"
                        )
                    aggregate += size
                    if aggregate > MAX_ARCHIVE_TOTAL_BYTES:
                        raise ReleaseVerificationError(
                            f"{archive_name} exceeds the archive uncompressed-byte limit"
                        )
                padded_size = ((size + 511) // 512) * 512
                if stream.tell() + padded_size > source_size:
                    raise ReleaseVerificationError(f"{archive_name} is truncated")
                stream.seek(padded_size, 1)
    except ReleaseVerificationError:
        raise
    except OSError as error:
        raise ReleaseVerificationError(
            f"cannot inspect TAR structure in {archive_name}: {error}"
        ) from error


class _BoundedReader:
    def __init__(self, stream: BinaryIO, max_bytes: int, context: str) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._context = context
        self._total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._max_bytes - self._total
        requested = remaining + 1 if size < 0 else min(size, remaining + 1)
        chunk = self._stream.read(requested)
        if len(chunk) > requested:
            raise ReleaseVerificationError(
                f"{self._context} exceeded a bounded read request"
            )
        self._total += len(chunk)
        if self._total > self._max_bytes:
            raise ReleaseVerificationError(
                f"{self._context} exceeds the {self._max_bytes}-byte limit"
            )
        return chunk


def _decompress_sdist(path: Path, destination: Path) -> None:
    advertised_size = _safe_file_size(
        path, MAX_DISTRIBUTION_BYTES, "distribution"
    )
    try:
        with (
            path.open("rb") as raw_source,
            destination.open("wb") as target,
        ):
            bounded_source = _BoundedReader(
                raw_source, MAX_DISTRIBUTION_BYTES, f"compressed {path.name}"
            )
            with gzip.GzipFile(fileobj=bounded_source, mode="rb") as compressed:
                total = 0
                while total < MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    requested = min(
                        READ_CHUNK_BYTES, MAX_ARCHIVE_UNCOMPRESSED_BYTES - total
                    )
                    chunk = compressed.read(requested)
                    if len(chunk) > requested:
                        raise ReleaseVerificationError(
                            f"{path.name} exceeded a bounded decompression read"
                        )
                    if not chunk:
                        break
                    target.write(chunk)
                    total += len(chunk)
                if compressed.read(1):
                    raise ReleaseVerificationError(
                        f"{path.name} exceeds the "
                        f"{MAX_ARCHIVE_UNCOMPRESSED_BYTES}-byte uncompressed limit"
                    )
            if bounded_source._total != advertised_size:
                raise ReleaseVerificationError(
                    f"distribution changed while being read: {path}"
                )
    except ReleaseVerificationError:
        raise
    except (OSError, EOFError) as error:
        raise ReleaseVerificationError(
            f"cannot decompress distribution {path.name}: {error}"
        ) from error


def _metadata_identity(path: Path) -> tuple[str, str]:
    try:
        if path.name.endswith(".whl"):
            _preflight_zip(path)
            with zipfile.ZipFile(path) as archive:
                match: zipfile.ZipInfo | None = None
                infos = archive.infolist()
                if len(infos) > MAX_ARCHIVE_MEMBERS:
                    raise ReleaseVerificationError(
                        f"{path.name} exceeds the "
                        f"{MAX_ARCHIVE_MEMBERS}-member archive limit"
                    )
                aggregate = 0
                names: set[str] = set()
                for info in infos:
                    _validate_archive_path(info.filename, path.name)
                    if info.filename in names:
                        raise ReleaseVerificationError(
                            f"{path.name} contains duplicate archive member: "
                            f"{info.filename}"
                        )
                    names.add(info.filename)
                    if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                        raise ReleaseVerificationError(
                            f"{path.name} contains an oversized archive member: "
                            f"{info.filename}"
                        )
                    aggregate += info.file_size
                    if aggregate > MAX_ARCHIVE_TOTAL_BYTES:
                        raise ReleaseVerificationError(
                            f"{path.name} exceeds the archive uncompressed-byte limit"
                        )
                    if info.filename.endswith(".dist-info/METADATA"):
                        if match is not None:
                            raise ReleaseVerificationError(
                                f"{path.name} must contain exactly one wheel "
                                "METADATA file"
                            )
                        match = info
                if match is None:
                    raise ReleaseVerificationError(
                        f"{path.name} must contain exactly one wheel METADATA file"
                    )
                if match.file_size > MAX_METADATA_BYTES:
                    raise ReleaseVerificationError(
                        f"{match.filename} in {path.name} exceeds the "
                        f"{MAX_METADATA_BYTES}-byte metadata limit"
                    )
                with archive.open(match) as stream:
                    payload = _exact_read(
                        stream, match.file_size, f"{match.filename} in {path.name}"
                    )
        elif path.name.endswith(".tar.gz"):
            with tempfile.TemporaryDirectory(prefix="boundver-sdist-inspect-") as temp:
                raw_tar = Path(temp) / "archive.tar"
                _decompress_sdist(path, raw_tar)
                _preflight_tar(raw_tar, path.name)
                with tarfile.open(raw_tar, mode="r:") as archive:
                    payload: bytes | None = None
                    count = 0
                    aggregate = 0
                    names: set[str] = set()
                    for member in archive:
                        count += 1
                        if count > MAX_ARCHIVE_MEMBERS:
                            raise ReleaseVerificationError(
                                f"{path.name} exceeds the "
                                f"{MAX_ARCHIVE_MEMBERS}-member archive limit"
                            )
                        _validate_archive_path(member.name, path.name)
                        if member.name in names:
                            raise ReleaseVerificationError(
                                f"{path.name} contains duplicate archive member: "
                                f"{member.name}"
                            )
                        names.add(member.name)
                        if member.islnk() or member.issym():
                            _validate_archive_path(member.linkname, path.name)
                        if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                            raise ReleaseVerificationError(
                                f"{path.name} contains an oversized archive member: "
                                f"{member.name}"
                            )
                        if member.isfile():
                            aggregate += member.size
                            if aggregate > MAX_ARCHIVE_TOTAL_BYTES:
                                raise ReleaseVerificationError(
                                    f"{path.name} exceeds the archive "
                                    "uncompressed-byte limit"
                                )
                        is_metadata = (
                            member.isfile()
                            and member.name.count("/") == 1
                            and member.name.endswith("/PKG-INFO")
                        )
                        if not is_metadata:
                            continue
                        if payload is not None:
                            raise ReleaseVerificationError(
                                f"{path.name} must contain exactly one top-level "
                                "PKG-INFO file"
                            )
                        if member.size > MAX_METADATA_BYTES:
                            raise ReleaseVerificationError(
                                f"{member.name} in {path.name} exceeds the "
                                f"{MAX_METADATA_BYTES}-byte metadata limit"
                            )
                        stream = archive.extractfile(member)
                        if stream is None:  # pragma: no cover - guarded by isfile()
                            raise ReleaseVerificationError(
                                f"cannot read {member.name} from {path.name}"
                            )
                        with stream:
                            payload = _exact_read(
                                stream,
                                member.size,
                                f"{member.name} in {path.name}",
                            )
                    if payload is None:
                        raise ReleaseVerificationError(
                            f"{path.name} must contain exactly one top-level "
                            "PKG-INFO file"
                        )
        else:  # pragma: no cover - guarded by _load_candidate()
            raise ReleaseVerificationError(f"unsupported distribution: {path.name}")
    except (OSError, tarfile.TarError, zipfile.BadZipFile, KeyError) as error:
        raise ReleaseVerificationError(
            f"cannot inspect distribution metadata in {path.name}: {error}"
        ) from error

    metadata = email.parser.BytesParser().parsebytes(payload)
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise ReleaseVerificationError(
            f"{path.name} metadata must contain Name and Version"
        )
    return name, version


def _load_candidate(
    dist_dir: Path, project: str, version: str
) -> dict[str, DistributionFile]:
    if not dist_dir.is_dir():
        raise ReleaseVerificationError(f"distribution directory is missing: {dist_dir}")

    entries: list[Path] = []
    try:
        for item in dist_dir.iterdir():
            if len(entries) >= 2:
                raise ReleaseVerificationError(
                    f"{dist_dir} must contain exactly one wheel and one .tar.gz sdist"
                )
            entries.append(item)
    except ReleaseVerificationError:
        raise
    except OSError as error:
        raise ReleaseVerificationError(
            f"cannot inspect distribution directory {dist_dir}: {error}"
        ) from error
    entries.sort(key=lambda item: item.name)
    if any(not item.is_file() or item.is_symlink() for item in entries):
        raise ReleaseVerificationError(
            f"{dist_dir} must contain only regular distribution files"
        )
    wheels = [item for item in entries if item.name.endswith(".whl")]
    sdists = [item for item in entries if item.name.endswith(".tar.gz")]
    if len(entries) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseVerificationError(
            f"{dist_dir} must contain exactly one wheel and one .tar.gz sdist"
        )

    expected_project = _normalized_project(project)
    candidate: dict[str, DistributionFile] = {}
    for path in entries:
        metadata_project, metadata_version = _metadata_identity(path)
        if _normalized_project(metadata_project) != expected_project:
            raise ReleaseVerificationError(
                f"{path.name} project {metadata_project!r} does not match {project!r}"
            )
        if metadata_version != version:
            raise ReleaseVerificationError(
                f"{path.name} version {metadata_version!r} does not match {version!r}"
            )
        digest, size = _sha256_file(path)
        candidate[path.name] = DistributionFile(
            filename=path.name,
            sha256=digest,
            size=size,
        )
    return candidate


def _declared_content_length(response: object, context: str) -> int | None:
    response_attributes = getattr(response, "__dict__", {})
    if (
        "headers" not in response_attributes
        and getattr(type(response), "headers", None) is None
    ):
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all("Content-Length")
        if values is None:
            return None
        if len(values) != 1:
            raise ReleaseNetworkError(f"{context} returned ambiguous Content-Length")
        raw_length: object = values[0]
    else:
        raw_length = headers.get("Content-Length")
        if raw_length is None:
            return None
    if isinstance(raw_length, int) and not isinstance(raw_length, bool):
        length = raw_length
    elif (
        isinstance(raw_length, str)
        and len(raw_length) <= 20
        and raw_length.isascii()
        and raw_length.isdigit()
    ):
        length = int(raw_length)
    else:
        raise ReleaseNetworkError(f"{context} returned malformed Content-Length")
    if length < 0:
        raise ReleaseNetworkError(f"{context} returned malformed Content-Length")
    return length


def _read_http_body(response: object, max_bytes: int, context: str) -> bytes:
    declared = _declared_content_length(response, context)
    if declared is not None and declared > max_bytes:
        raise ReleaseNetworkError(
            f"{context} exceeds the {max_bytes}-byte response limit"
        )
    read_limit = declared if declared is not None else max_bytes
    chunks: list[bytes] = []
    total = 0
    while total < read_limit:
        requested = min(READ_CHUNK_BYTES, read_limit - total)
        chunk = response.read(requested)
        if not isinstance(chunk, bytes):
            raise ReleaseNetworkError(f"{context} returned non-byte response data")
        if len(chunk) > requested:
            raise ReleaseNetworkError(f"{context} exceeded a bounded read request")
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    sentinel = response.read(1)
    if not isinstance(sentinel, bytes):
        raise ReleaseNetworkError(f"{context} returned non-byte response data")
    if sentinel:
        limit = declared if declared is not None else max_bytes
        raise ReleaseNetworkError(f"{context} exceeds its {limit}-byte response limit")
    if declared is not None and total != declared:
        raise ReleaseNetworkError(f"{context} body disagrees with Content-Length")
    return b"".join(chunks)


def _provenance_url(project: str, version: str, filename: str) -> str:
    if (
        not filename
        or not _filename_within_limit(filename)
        or Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or not (filename.endswith(".whl") or filename.endswith(".tar.gz"))
    ):
        raise ReleaseVerificationError(
            "provenance filename must name one wheel or .tar.gz sdist"
        )
    return (
        f"{DEFAULT_INTEGRITY_ORIGIN}/integrity/"
        f"{urllib.parse.quote(project, safe='')}/"
        f"{urllib.parse.quote(version, safe='')}/"
        f"{urllib.parse.quote(filename, safe='')}/provenance"
    )


def _write_new_file(path: Path, payload: bytes) -> None:
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ReleaseVerificationError(
            f"provenance output parent must be a regular directory: {parent}"
        )
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            written = stream.write(payload)
            if written != len(payload):
                raise OSError("short write")
    except OSError as error:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise ReleaseVerificationError(
            f"cannot write provenance output {path}: {error}"
        ) from error


def _fetch_provenance(
    project: str, version: str, filename: str, output: Path
) -> None:
    url = _provenance_url(project, version, filename)
    if output.exists() or output.is_symlink():
        raise ReleaseVerificationError(
            f"provenance output already exists: {output}"
        )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.pypi.integrity.v1+json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.geturl() != url:
                raise ReleaseVerificationError(
                    "TestPyPI provenance request redirected"
                )
            payload = _read_http_body(
                response, MAX_PROVENANCE_BYTES, "TestPyPI provenance"
            )
    except (OSError, urllib.error.URLError) as error:
        raise ReleaseNetworkError(
            f"cannot read TestPyPI provenance for {filename}: {error}"
        ) from error
    _write_new_file(output, payload)


def _request_json(url: str) -> Mapping[str, Any] | None:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            requested = urllib.parse.urlsplit(url)
            final = urllib.parse.urlsplit(response.geturl())
            if final.scheme != requested.scheme or final.netloc != requested.netloc:
                raise ReleaseVerificationError(
                    f"TestPyPI metadata redirected outside {requested.scheme}://"
                    f"{requested.netloc}: {response.geturl()!r}"
                )
            body = _read_http_body(
                response, MAX_INDEX_METADATA_BYTES, "package-index metadata"
            )
            try:
                payload = _strict_json_loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise ReleaseNetworkError(
                    f"cannot read package-index release metadata: {error}"
                ) from error
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise ReleaseNetworkError(f"package index returned HTTP {error.code}: {url}") from error
    except (
        OSError,
        urllib.error.URLError,
    ) as error:
        raise ReleaseNetworkError(f"cannot read package-index release metadata: {error}") from error
    if not isinstance(payload, dict):
        raise ReleaseVerificationError("package-index release response must be a JSON object")
    return payload


def _release_url(api_base: str, project: str, version: str) -> str:
    return (
        f"{api_base.rstrip('/')}/"
        f"{urllib.parse.quote(project, safe='')}/"
        f"{urllib.parse.quote(version, safe='')}/json"
    )


def _validate_download_url(url: str, download_origin: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    expected = urllib.parse.urlsplit(download_origin)
    if (
        parsed.scheme != expected.scheme
        or parsed.netloc != expected.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\n" in url
        or "\r" in url
    ):
        raise ReleaseVerificationError(
            f"package index supplied a distribution URL outside {download_origin}: {url!r}"
        )


def _parse_remote_release(
    payload: Mapping[str, Any],
    project: str,
    version: str,
    download_origin: str,
) -> dict[str, DistributionFile]:
    info = payload.get("info")
    urls = payload.get("urls")
    if not isinstance(info, dict) or not isinstance(urls, list):
        raise ReleaseVerificationError("package-index response is missing info or urls")
    if len(urls) > MAX_INDEX_FILES:
        raise ReleaseVerificationError(
            f"package-index response exceeds the {MAX_INDEX_FILES}-file limit"
        )
    remote_project = info.get("name")
    remote_version = info.get("version")
    if not isinstance(remote_project, str) or (
        _normalized_project(remote_project) != _normalized_project(project)
    ):
        raise ReleaseVerificationError(
            "package-index project identity does not match the expected project"
        )
    if remote_version != version:
        raise ReleaseVerificationError(
            "package-index version identity does not match the expected version"
        )

    remote: dict[str, DistributionFile] = {}
    for item in urls:
        if not isinstance(item, dict):
            raise ReleaseVerificationError("package-index urls entries must be objects")
        filename = item.get("filename")
        digest_map = item.get("digests")
        size = item.get("size")
        url = item.get("url")
        if (
            not isinstance(filename, str)
            or not filename
            or not _filename_within_limit(filename)
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
            or not isinstance(digest_map, dict)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_DISTRIBUTION_BYTES
            or not isinstance(url, str)
        ):
            raise ReleaseVerificationError("package index supplied malformed file metadata")
        sha256 = digest_map.get("sha256")
        if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
            raise ReleaseVerificationError(
                f"package index supplied an invalid SHA-256 for {filename!r}"
            )
        if item.get("yanked") is not False:
            raise ReleaseVerificationError(f"package-index file is yanked: {filename}")
        _validate_download_url(url, download_origin)
        if urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).name) != filename:
            raise ReleaseVerificationError(
                f"package-index filename and URL disagree for {filename!r}"
            )
        if filename in remote:
            raise ReleaseVerificationError(
                f"package index supplied duplicate file metadata for {filename!r}"
            )
        remote[filename] = DistributionFile(filename, sha256, size, url)
    return remote


def _compare_release(
    candidate: Mapping[str, DistributionFile],
    remote: Mapping[str, DistributionFile],
) -> bool:
    unexpected = sorted(set(remote) - set(candidate))
    if unexpected:
        raise ReleaseVerificationError(
            f"package-index release has unexpected files: {', '.join(unexpected)}"
        )
    for filename, remote_file in remote.items():
        local_file = candidate[filename]
        if (
            remote_file.sha256 != local_file.sha256
            or remote_file.size != local_file.size
        ):
            raise ReleaseVerificationError(
                f"package-index file does not match the candidate: {filename}"
            )
    return set(remote) == set(candidate)


def _download_and_verify(
    files: Mapping[str, DistributionFile], download_origin: str
) -> None:
    if len(files) > MAX_INDEX_FILES:
        raise ReleaseVerificationError(
            f"package-index download set exceeds the {MAX_INDEX_FILES}-file limit"
        )
    for filename in sorted(files):
        expected = files[filename]
        if (
            not isinstance(expected.size, int)
            or isinstance(expected.size, bool)
            or expected.size < 0
            or expected.size > MAX_DISTRIBUTION_BYTES
        ):
            raise ReleaseVerificationError(
                f"package-index file has an invalid or oversized length: {filename}"
            )
        if expected.url is None:  # pragma: no cover - only remote files are passed
            raise ReleaseVerificationError(f"missing download URL for {filename}")
        _validate_download_url(expected.url, download_origin)
        request = urllib.request.Request(
            expected.url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": USER_AGENT,
            },
        )
        digest = hashlib.sha256()
        size = 0
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                _validate_download_url(response.geturl(), download_origin)
                declared = _declared_content_length(
                    response, f"package-index file {filename}"
                )
                if declared is not None and declared != expected.size:
                    raise ReleaseVerificationError(
                        "package-index download Content-Length disagrees with "
                        f"advertised file: {filename}"
                    )
                while size < expected.size:
                    requested = min(READ_CHUNK_BYTES, expected.size - size)
                    chunk = response.read(requested)
                    if not isinstance(chunk, bytes):
                        raise ReleaseNetworkError(
                            f"package-index file {filename} returned non-byte data"
                        )
                    if len(chunk) > requested:
                        raise ReleaseNetworkError(
                            f"package-index file {filename} exceeded a bounded read"
                        )
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                sentinel = response.read(1)
                if not isinstance(sentinel, bytes):
                    raise ReleaseNetworkError(
                        f"package-index file {filename} returned non-byte data"
                    )
                if sentinel:
                    raise ReleaseVerificationError(
                        "downloaded package-index file exceeds advertised size: "
                        f"{filename}"
                    )
        except (OSError, urllib.error.URLError) as error:
            raise ReleaseNetworkError(
                f"cannot download package-index file {filename}: {error}"
            ) from error
        if digest.hexdigest() != expected.sha256 or size != expected.size:
            raise ReleaseVerificationError(
                f"downloaded package-index bytes do not match advertised file: {filename}"
            )


def _query_release(
    *,
    api_base: str,
    download_origin: str,
    project: str,
    version: str,
) -> dict[str, DistributionFile] | None:
    payload = _request_json(_release_url(api_base, project, version))
    if payload is None:
        return None
    return _parse_remote_release(payload, project, version, download_origin)


def _write_output(path: Path | None, key: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ReleaseVerificationError(f"unsafe workflow output value for {key}")
    if path is None:
        print(f"{key}={value}")
        return
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{key}={value}\n")


def _preflight(args: argparse.Namespace) -> None:
    candidate = _load_candidate(args.dist, args.project, args.version)
    remote = _query_release(
        api_base=args.api_base,
        download_origin=args.download_origin,
        project=args.project,
        version=args.version,
    )
    if remote is None:
        print(f"Package-index release {args.project} {args.version} does not exist yet")
        _write_output(args.github_output, "upload-required", "true")
        return

    complete = _compare_release(candidate, remote)
    _download_and_verify(remote, args.download_origin)
    if complete:
        print("Package index already contains the exact candidate; upload is unnecessary")
        _write_output(args.github_output, "upload-required", "false")
    else:
        missing = sorted(set(candidate) - set(remote))
        print(
            "Package index contains an exact partial candidate; an idempotent upload "
            f"attempt is required for: {', '.join(missing)}"
        )
        _write_output(args.github_output, "upload-required", "true")


def _verify(args: argparse.Namespace) -> None:
    candidate = _load_candidate(args.dist, args.project, args.version)
    last_incomplete = "release is not visible"
    for attempt in range(1, args.attempts + 1):
        try:
            remote = _query_release(
                api_base=args.api_base,
                download_origin=args.download_origin,
                project=args.project,
                version=args.version,
            )
            if remote is None:
                raise ReleaseIncompleteError("release is not visible")
            if not _compare_release(candidate, remote):
                missing = sorted(set(candidate) - set(remote))
                raise ReleaseIncompleteError(
                    f"release is missing: {', '.join(missing)}"
                )
            _download_and_verify(remote, args.download_origin)
        except (ReleaseIncompleteError, ReleaseNetworkError) as error:
            last_incomplete = str(error)
            if attempt == args.attempts:
                break
            print(
                f"Package index is not ready ({attempt}/{args.attempts}): {error}",
                file=sys.stderr,
            )
            time.sleep(args.delay_seconds)
            continue

        wheel = next(item for item in remote.values() if item.filename.endswith(".whl"))
        sdist = next(
            item for item in remote.values() if item.filename.endswith(".tar.gz")
        )
        assert wheel.url is not None  # narrowed by a complete remote release
        assert sdist.url is not None
        _write_output(
            args.github_output, "wheel-url", f"{wheel.url}#sha256={wheel.sha256}"
        )
        _write_output(
            args.github_output, "sdist-url", f"{sdist.url}#sha256={sdist.sha256}"
        )
        print(
            f"Package index contains the exact {args.project} {args.version} candidate"
        )
        return
    raise ReleaseIncompleteError(
        f"Package index did not expose the complete candidate after "
        f"{args.attempts} attempt(s): {last_incomplete}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare exact local distributions with one TestPyPI release."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--dist", type=Path, required=True)
        subparser.add_argument("--project", required=True)
        subparser.add_argument("--version", required=True)
        subparser.add_argument("--api-base", default=DEFAULT_API_BASE)
        subparser.add_argument(
            "--download-origin", default=DEFAULT_DOWNLOAD_ORIGIN
        )
        subparser.add_argument("--github-output", type=Path)
        if command == "verify":
            subparser.add_argument("--attempts", type=int, default=12)
            subparser.add_argument("--delay-seconds", type=float, default=10.0)
    provenance = subparsers.add_parser(
        "provenance",
        help="download one bounded TestPyPI Integrity API statement",
    )
    provenance.add_argument("--project", required=True)
    provenance.add_argument("--version", required=True)
    provenance.add_argument("--filename", required=True)
    provenance.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if PROJECT_RE.fullmatch(args.project) is None:
        parser.error("--project must be a valid Python distribution name")
    if VERSION_RE.fullmatch(args.version) is None:
        parser.error("--version must be an exact X.Y.Z release")
    if args.command == "verify" and (
        args.attempts < 1 or args.delay_seconds < 0
    ):
        parser.error("--attempts must be positive and --delay-seconds non-negative")

    try:
        if args.command == "preflight":
            _preflight(args)
        elif args.command == "verify":
            _verify(args)
        else:
            _fetch_provenance(
                args.project, args.version, args.filename, args.output
            )
    except (ReleaseVerificationError, ReleaseIncompleteError, ReleaseNetworkError) as error:
        print(f"Package-index verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
