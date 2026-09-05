#!/usr/bin/env python3
"""Prepare digest-verified, per-platform OCI layouts for offline scanning.

Buildx emits a multi-platform OCI *archive*. Trivy accepts an OCI layout
directory, but its local ``--platform`` option does not select a child from a
multi-platform index. This helper safely expands the retained archive, verifies
every content-addressed blob, and writes one root-index selector per requested
platform. The caller copies each selector over ``layout/index.json`` before
scanning that layout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tarfile
import tempfile
from typing import BinaryIO, NoReturn, Sequence


MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_JSON_BYTES = 4 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024

OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG_MEDIA_TYPE = "application/vnd.oci.image.config.v1+json"

SHA256_RE = re.compile(r"sha256:([0-9a-f]{64})")
BLOB_MEMBER_RE = re.compile(r"blobs/sha256/([0-9a-f]{64})")
PLATFORM_PART_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")


class OciScanLayoutError(ValueError):
    """Raised when retained OCI input is unsafe, malformed, or ambiguous."""


def _fail(message: str) -> NoReturn:
    raise OciScanLayoutError(message)


def _bounded_int(value: str) -> int:
    if len(value) > 20 or re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None:
        _fail("OCI JSON integer is outside the supported representation")
    return int(value)


def _reject_float(value: str) -> float:
    del value
    _fail("OCI JSON floating-point values are not permitted")


def _reject_constant(value: str) -> None:
    del value
    _fail("OCI JSON non-finite values are not permitted")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("OCI JSON contains a duplicate object key")
        result[key] = value
    return result


def _load_json(path: Path, label: str) -> dict[str, object]:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        _fail(f"cannot inspect {label}: {error}")
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_JSON_BYTES:
        _fail(f"{label} must be a bounded regular JSON file")
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_int=_bounded_int,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        _fail(f"cannot parse {label}: {error}")
    if not isinstance(value, dict):
        _fail(f"{label} must contain a JSON object")
    return value


def _safe_member_name(member: tarfile.TarInfo) -> tuple[str, str | None]:
    name = member.name
    path_parts = name.removesuffix("/").split("/")
    if (
        "\\" in name
        or name.startswith("/")
        or any(part in {"", ".", ".."} for part in path_parts)
    ):
        _fail("OCI archive contains an unsafe member name")
    normalized = name.removesuffix("/")
    if normalized in {"blobs", "blobs/sha256"}:
        if not member.isdir():
            _fail(f"OCI archive directory has the wrong type: {name!r}")
        return normalized, None
    if normalized in {"index.json", "oci-layout"}:
        if not member.isreg():
            _fail(f"OCI archive root file has the wrong type: {name!r}")
        return normalized, None
    match = BLOB_MEMBER_RE.fullmatch(normalized)
    if match is None or not member.isreg():
        _fail("OCI archive contains an unsupported member name or type")
    return normalized, match.group(1)


def _copy_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    destination: Path,
    expected_digest: str | None,
) -> None:
    source = archive.extractfile(member)
    if source is None:
        _fail(f"cannot read OCI archive member {member.name!r}")
    digest = hashlib.sha256()
    copied = 0
    try:
        with source, destination.open("xb") as output:
            while copied < member.size:
                chunk = source.read(min(READ_CHUNK_BYTES, member.size - copied))
                if not chunk:
                    _fail(f"OCI archive member {member.name!r} is truncated")
                output.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            if source.read(1):
                _fail(f"OCI archive member {member.name!r} exceeds its declared size")
    except OSError as error:
        _fail(f"cannot extract OCI archive member {member.name!r}: {error}")
    if copied != member.size:
        _fail(f"OCI archive member {member.name!r} has an inconsistent size")
    if expected_digest is not None and digest.hexdigest() != expected_digest:
        _fail(f"OCI blob digest mismatch for {member.name!r}")
    try:
        destination.chmod(0o600)
    except OSError as error:
        _fail(f"cannot restrict extracted OCI member {member.name!r}: {error}")


def _extract_layout(archive_path: Path, layout: Path) -> None:
    try:
        path_metadata = archive_path.lstat()
    except OSError as error:
        _fail(f"cannot inspect retained OCI archive: {error}")
    if not stat.S_ISREG(path_metadata.st_mode):
        _fail("retained OCI archive must be a regular file, not a link")
    if not 0 < path_metadata.st_size <= MAX_ARCHIVE_BYTES:
        _fail("retained OCI archive is empty or exceeds 2 GiB")

    try:
        raw: BinaryIO
        with archive_path.open("rb") as raw:
            opened_metadata = os.fstat(raw.fileno())
            if (
                opened_metadata.st_dev != path_metadata.st_dev
                or opened_metadata.st_ino != path_metadata.st_ino
                or opened_metadata.st_size != path_metadata.st_size
            ):
                _fail("retained OCI archive changed while it was opened")
            with tarfile.open(fileobj=raw, mode="r:") as archive:
                members: list[tuple[tarfile.TarInfo, str, str | None]] = []
                names: set[str] = set()
                total_size = 0
                for member in archive:
                    if len(members) >= MAX_ARCHIVE_MEMBERS:
                        _fail("OCI archive exceeds the member-count limit")
                    normalized, expected_digest = _safe_member_name(member)
                    if normalized in names:
                        _fail(f"OCI archive contains duplicate member {normalized!r}")
                    names.add(normalized)
                    if member.size < 0 or member.size > MAX_ARCHIVE_BYTES:
                        _fail(f"OCI archive member {member.name!r} has an unsafe size")
                    total_size += member.size
                    if total_size > MAX_ARCHIVE_BYTES:
                        _fail("OCI archive exceeds the extracted-size limit")
                    members.append((member, normalized, expected_digest))

                required = {"index.json", "oci-layout"}
                if not required.issubset(names):
                    _fail("OCI archive is missing its root index or layout declaration")
                if not any(BLOB_MEMBER_RE.fullmatch(name) for name in names):
                    _fail("OCI archive contains no content-addressed blobs")

                layout.mkdir(mode=0o700)
                (layout / "blobs" / "sha256").mkdir(parents=True, mode=0o700)
                for member, normalized, expected_digest in members:
                    if member.isdir():
                        continue
                    destination = layout.joinpath(*normalized.split("/"))
                    _copy_member(archive, member, destination, expected_digest)

            final_metadata = os.fstat(raw.fileno())
            if (
                final_metadata.st_dev != opened_metadata.st_dev
                or final_metadata.st_ino != opened_metadata.st_ino
                or final_metadata.st_size != opened_metadata.st_size
                or final_metadata.st_mtime_ns != opened_metadata.st_mtime_ns
            ):
                _fail("retained OCI archive changed while it was read")
    except (OSError, tarfile.TarError) as error:
        _fail(f"cannot read retained OCI archive: {error}")


def _digest_path(layout: Path, digest: object, size: object, label: str) -> Path:
    if not isinstance(digest, str):
        _fail(f"{label} digest is missing")
    match = SHA256_RE.fullmatch(digest)
    if match is None:
        _fail(f"{label} digest is not a canonical SHA-256 digest")
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
        _fail(f"{label} size is invalid")
    path = layout / "blobs" / "sha256" / match.group(1)
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        _fail(f"cannot inspect {label} blob: {error}")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
        _fail(f"{label} blob size or type disagrees with its descriptor")
    return path


def _manifests(value: dict[str, object], label: str) -> list[object]:
    if value.get("schemaVersion") != 2:
        _fail(f"{label} must use OCI schema version 2")
    if value.get("mediaType") != OCI_INDEX_MEDIA_TYPE:
        _fail(f"{label} must be an OCI image index")
    manifests = value.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        _fail(f"{label} must contain at least one manifest descriptor")
    return manifests


def _parse_platform(value: str) -> tuple[str, str]:
    parts = value.split("/")
    if (
        len(parts) != 2
        or PLATFORM_PART_RE.fullmatch(parts[0]) is None
        or PLATFORM_PART_RE.fullmatch(parts[1]) is None
    ):
        _fail(f"invalid platform {value!r}; expected os/architecture")
    return parts[0], parts[1]


def _prepare_selectors(layout: Path, selectors: Path, platforms: Sequence[str]) -> None:
    declaration = _load_json(layout / "oci-layout", "OCI layout declaration")
    if declaration != {"imageLayoutVersion": "1.0.0"}:
        _fail("OCI layout declaration must select image layout version 1.0.0")

    root_index = _load_json(layout / "index.json", "OCI root index")
    root_manifests = _manifests(root_index, "OCI root index")
    if len(root_manifests) != 1 or not isinstance(root_manifests[0], dict):
        _fail("OCI root index must contain exactly one nested index descriptor")
    root_descriptor = root_manifests[0]
    if root_descriptor.get("mediaType") != OCI_INDEX_MEDIA_TYPE:
        _fail("OCI root descriptor must reference a multi-platform image index")
    nested_path = _digest_path(
        layout,
        root_descriptor.get("digest"),
        root_descriptor.get("size"),
        "nested OCI index",
    )
    nested_index = _load_json(nested_path, "nested OCI index")
    descriptors = _manifests(nested_index, "nested OCI index")

    requested = [_parse_platform(platform) for platform in platforms]
    if len(requested) != len(set(requested)):
        _fail("requested OCI scan platforms must be unique")

    selected: dict[tuple[str, str], dict[str, object]] = {}
    for descriptor_value in descriptors:
        if not isinstance(descriptor_value, dict):
            _fail("nested OCI index contains a non-object descriptor")
        if descriptor_value.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE:
            _fail("nested OCI index contains a non-image manifest descriptor")
        platform_value = descriptor_value.get("platform")
        if not isinstance(platform_value, dict):
            _fail("OCI image descriptor is missing its platform")
        operating_system = platform_value.get("os")
        architecture = platform_value.get("architecture")
        if not isinstance(operating_system, str) or not isinstance(architecture, str):
            _fail("OCI image descriptor has a malformed platform")
        platform = _parse_platform(f"{operating_system}/{architecture}")
        if platform in selected:
            _fail(f"OCI image index contains duplicate platform {operating_system}/{architecture}")
        selected[platform] = descriptor_value

    if set(selected) != set(requested):
        actual = ", ".join(f"{os_name}/{arch}" for os_name, arch in sorted(selected))
        expected = ", ".join(f"{os_name}/{arch}" for os_name, arch in sorted(requested))
        _fail(f"OCI platform set disagrees; expected [{expected}], found [{actual}]")

    selectors.mkdir(mode=0o700)
    for operating_system, architecture in requested:
        descriptor = selected[(operating_system, architecture)]
        manifest_path = _digest_path(
            layout,
            descriptor.get("digest"),
            descriptor.get("size"),
            f"{operating_system}/{architecture} image manifest",
        )
        manifest = _load_json(
            manifest_path, f"{operating_system}/{architecture} image manifest"
        )
        if manifest.get("schemaVersion") != 2:
            _fail(f"{operating_system}/{architecture} image manifest has the wrong schema")
        if manifest.get("mediaType") != OCI_MANIFEST_MEDIA_TYPE:
            _fail(f"{operating_system}/{architecture} image manifest has the wrong media type")
        config = manifest.get("config")
        if not isinstance(config, dict) or config.get("mediaType") != OCI_CONFIG_MEDIA_TYPE:
            _fail(f"{operating_system}/{architecture} image config descriptor is malformed")
        config_path = _digest_path(
            layout,
            config.get("digest"),
            config.get("size"),
            f"{operating_system}/{architecture} image config",
        )
        config_value = _load_json(
            config_path, f"{operating_system}/{architecture} image config"
        )
        if (
            config_value.get("os") != operating_system
            or config_value.get("architecture") != architecture
        ):
            _fail(
                f"{operating_system}/{architecture} descriptor disagrees with its image config"
            )

        selector = {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX_MEDIA_TYPE,
            "manifests": [descriptor],
        }
        selector_path = selectors / f"{operating_system}-{architecture}.json"
        try:
            selector_path.write_text(
                json.dumps(selector, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            selector_path.chmod(0o600)
        except OSError as error:
            _fail(f"cannot write OCI selector for {operating_system}/{architecture}: {error}")


def prepare_scan_layout(
    archive: Path, output: Path, platforms: Sequence[str]
) -> None:
    """Safely prepare a retained OCI archive for exact per-platform scans."""
    if not platforms:
        _fail("at least one OCI scan platform is required")
    try:
        output_parent = output.parent
        output_parent.mkdir(parents=True, exist_ok=True)
        if output.exists() or output.is_symlink():
            _fail("OCI scan output path already exists")
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.", dir=str(output_parent))
        )
    except OSError as error:
        _fail(f"cannot create OCI scan output: {error}")
    try:
        _extract_layout(archive, temporary / "layout")
        _prepare_selectors(temporary / "layout", temporary / "selectors", platforms)
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare a retained multi-platform OCI archive for exact Trivy scans."
    )
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--platform",
        action="append",
        required=True,
        dest="platforms",
        help="Required os/architecture; repeat for every exact platform",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        prepare_scan_layout(args.archive, args.output, args.platforms)
    except OciScanLayoutError as error:
        print(f"OCI scan layout preparation failed: {error}", file=sys.stderr)
        return 1
    print(
        "Prepared digest-verified OCI scan selectors for "
        + ", ".join(args.platforms)
        + "."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
