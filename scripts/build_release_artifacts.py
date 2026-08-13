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
import hashlib
import io
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
_ZIP_MIN_EPOCH = 315532800  # 1980-01-01T00:00:00Z
_ZIP_MAX_EPOCH = 4294967294  # latest even timestamp representable by gzip


def _project_version(pyproject_path: Path) -> str:
    text = pyproject_path.read_text(encoding="utf-8")
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
    with ZipFile(source) as archive:
        entries = [(item.filename, archive.read(item)) for item in archive.infolist()]
    names = [name for name, _ in entries]
    if len(names) != len(set(names)):
        raise ValueError(f"wheel contains duplicate members: {source}")

    with ZipFile(destination, "w", compression=ZIP_STORED) as archive:
        for name, payload in sorted(entries):
            info = ZipInfo(name, timestamp)
            info.compress_type = ZIP_STORED
            info.create_system = 3
            info.external_attr = (0o100644 << 16)
            archive.writestr(info, payload)


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


def _canonicalize_sdist(source: Path, destination: Path, epoch: int) -> None:
    """Rewrite a ``.tar.gz`` sdist with stable tar and gzip metadata."""
    with tarfile.open(source, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ValueError(f"sdist contains duplicate members: {source}")
        payloads = {
            member.name: archive.extractfile(member).read()
            for member in members
            if member.isfile()
        }

    tar_bytes = io.BytesIO()
    with tarfile.open(
        fileobj=tar_bytes, mode="w", format=tarfile.PAX_FORMAT
    ) as canonical:
        for member in sorted(members, key=lambda item: item.name):
            normalized = _canonical_tar_info(member, epoch)
            if normalized.isfile():
                canonical.addfile(normalized, io.BytesIO(payloads[member.name]))
            else:
                canonical.addfile(normalized)
    destination.write_bytes(_stored_gzip(tar_bytes.getvalue(), epoch))


def _stored_gzip(payload: bytes, epoch: int) -> bytes:
    """Encode gzip with uncompressed DEFLATE blocks, without zlib variance."""
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

    wheels = list(raw.glob("*.whl"))
    sdists = list(raw.glob("*.tar.gz"))
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
            str(REPO_ROOT / "scripts" / "build_standalone.py"),
            "--output",
            str(canonical / f"boundver-{version}.pyz"),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )


def _digests(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


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
        for path in (first / "canonical").iterdir():
            shutil.copyfile(path, output / path.name)
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
