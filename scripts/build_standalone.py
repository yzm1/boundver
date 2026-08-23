#!/usr/bin/env python3
"""
Build a standalone boundver.pyz zipapp — no pip install required.

Usage:
    python scripts/build_standalone.py
    python scripts/build_standalone.py --output dist/boundver.pyz

The resulting .pyz file is self-contained and can be run directly:

    python3 boundver.pyz generate
    python3 boundver.pyz verify

On Unix systems you can also make it executable:
    chmod +x dist/boundver.pyz
    ./dist/boundver.pyz verify

Requires:
  - Python 3.9+ for JSON and YAML configs
  - Python 3.11+ for TOML configs in the standalone archive

The archive vendors the pure-Python runtime from the exact PyYAML wheel pinned
in the release toolchain. Native LibYAML extensions are deliberately omitted so
one reproducible archive runs on every supported platform.
"""

from __future__ import annotations

import argparse
import importlib.metadata as importlib_metadata
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional
from zipfile import ZIP_STORED, ZipFile, ZipInfo


_ZIP_MIN_EPOCH = 315532800  # 1980-01-01T00:00:00Z
_ZIP_MAX_EPOCH = 4294967294  # latest even timestamp also representable by gzip
MAX_SOURCE_TREE_ENTRIES = 20_000
MAX_SOURCE_MEMBERS = 10_000
MAX_SOURCE_PATH_BYTES = 4 * 1024
MAX_SOURCE_TOTAL_PATH_BYTES = 8 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_TOTAL_BYTES = 128 * 1024 * 1024
MAX_PROJECT_METADATA_BYTES = 1024 * 1024
MAX_TOOL_LOCK_BYTES = 4 * 1024 * 1024
MAX_LICENSE_BYTES = 4 * 1024 * 1024
MAX_VENDORED_TREE_ENTRIES = 128
MAX_VENDORED_MEMBERS = 64
MAX_VENDORED_TOTAL_BYTES = 8 * 1024 * 1024
_PYYAML_SOURCE_FILES = (
    "__init__.py",
    "composer.py",
    "constructor.py",
    "cyaml.py",
    "dumper.py",
    "emitter.py",
    "error.py",
    "events.py",
    "loader.py",
    "nodes.py",
    "parser.py",
    "reader.py",
    "representer.py",
    "resolver.py",
    "scanner.py",
    "serializer.py",
    "tokens.py",
)
MAX_STAGE_TREE_ENTRIES = MAX_SOURCE_MEMBERS + MAX_VENDORED_MEMBERS + 32
MAX_STAGE_MEMBERS = MAX_SOURCE_MEMBERS + MAX_VENDORED_MEMBERS + 32
MAX_STAGE_TOTAL_BYTES = (
    MAX_SOURCE_TOTAL_BYTES
    + MAX_VENDORED_TOTAL_BYTES
    + (3 * MAX_LICENSE_BYTES)
    + 64 * 1024
)
_READ_CHUNK_BYTES = 64 * 1024


class StandaloneBuildError(ValueError):
    """The standalone source cannot be built safely within fixed limits."""


@dataclass(frozen=True)
class _TreeEntry:
    path: Path
    relative: Path
    identity: os.stat_result
    is_directory: bool


@dataclass(frozen=True)
class _TreeManifest:
    root: Path
    root_identity: os.stat_result
    entries: list[_TreeEntry]
    total_file_bytes: int


@dataclass(frozen=True)
class _VendoredPyYAML:
    manifest: _TreeManifest
    license_bytes: bytes
    version: str


def _is_windows_reparse_point(identity: os.stat_result) -> bool:
    attributes = getattr(identity, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _changed(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or before.st_mode != after.st_mode
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    )


def _path_bytes(path: Path) -> int:
    return len(path.as_posix().encode("utf-8", "surrogatepass"))


def _is_ignored_source_path(relative: Path) -> bool:
    return (
        any(part.lower() == "__pycache__" for part in relative.parts)
        or relative.suffix.lower() in {".pyc", ".pyo"}
    )


def _collect_tree(
    root: Path,
    *,
    source_tree: bool,
    max_entries: int,
    max_members: int,
    max_total_bytes: int,
) -> _TreeManifest:
    """Build a bounded, sorted manifest from a lazy directory traversal."""
    try:
        root_identity = root.lstat()
    except FileNotFoundError as exc:
        raise StandaloneBuildError(f"tree root does not exist: {root}") from exc
    if (
        not stat.S_ISDIR(root_identity.st_mode)
        or _is_windows_reparse_point(root_identity)
    ):
        raise StandaloneBuildError(f"tree root is not a plain directory: {root}")

    pending: list[tuple[Path, Path]] = [(root, Path())]
    selected: list[_TreeEntry] = []
    traversed = 0
    total_path_bytes = 0
    total_file_bytes = 0
    while pending:
        directory, relative_directory = pending.pop()
        try:
            before = directory.lstat()
            if (
                not stat.S_ISDIR(before.st_mode)
                or _is_windows_reparse_point(before)
            ):
                raise StandaloneBuildError(
                    f"tree directory is not safe to traverse: {directory}"
                )
            with os.scandir(directory) as entries:
                for entry in entries:
                    traversed += 1
                    if traversed > max_entries:
                        raise StandaloneBuildError(
                            f"tree exceeds the {max_entries}-entry traversal limit"
                        )
                    relative = relative_directory / entry.name
                    encoded_length = _path_bytes(relative)
                    if encoded_length > MAX_SOURCE_PATH_BYTES:
                        raise StandaloneBuildError(
                            "tree path exceeds the "
                            f"{MAX_SOURCE_PATH_BYTES}-byte limit"
                        )
                    total_path_bytes += encoded_length
                    if total_path_bytes > MAX_SOURCE_TOTAL_PATH_BYTES:
                        raise StandaloneBuildError(
                            "tree paths exceed the "
                            f"{MAX_SOURCE_TOTAL_PATH_BYTES}-byte aggregate limit"
                        )
                    if source_tree and _is_ignored_source_path(relative):
                        continue
                    # ``DirEntry.stat`` may omit stable file identifiers on
                    # Windows; capture the identity through the same lstat
                    # primitive used by all later race checks.
                    entry_path = Path(entry.path)
                    identity = entry_path.lstat()
                    is_directory = stat.S_ISDIR(identity.st_mode)
                    if (
                        _is_windows_reparse_point(identity)
                        or not (is_directory or stat.S_ISREG(identity.st_mode))
                    ):
                        raise StandaloneBuildError(
                            f"tree contains a symlink, reparse point, or "
                            f"non-regular entry: {relative}"
                        )
                    if len(selected) >= max_members:
                        raise StandaloneBuildError(
                            f"tree exceeds the {max_members}-member limit"
                        )
                    selected.append(
                        _TreeEntry(
                            entry_path, relative, identity, is_directory
                        )
                    )
                    if is_directory:
                        pending.append((entry_path, relative))
                        continue
                    if identity.st_size > MAX_SOURCE_FILE_BYTES:
                        raise StandaloneBuildError(
                            "tree file exceeds the "
                            f"{MAX_SOURCE_FILE_BYTES}-byte limit: {relative}"
                        )
                    total_file_bytes += identity.st_size
                    if total_file_bytes > max_total_bytes:
                        raise StandaloneBuildError(
                            "tree files exceed the "
                            f"{max_total_bytes}-byte aggregate limit"
                        )
            after = directory.lstat()
        except FileNotFoundError as exc:
            raise StandaloneBuildError(
                f"tree changed while traversing: {directory}"
            ) from exc
        if (
            not stat.S_ISDIR(after.st_mode)
            or _is_windows_reparse_point(after)
            or _changed(before, after)
        ):
            raise StandaloneBuildError(
                f"tree changed while traversing: {directory}"
            )

    current_root = root.lstat()
    if (
        not stat.S_ISDIR(current_root.st_mode)
        or _is_windows_reparse_point(current_root)
        or _changed(root_identity, current_root)
    ):
        raise StandaloneBuildError(f"tree changed while traversing: {root}")
    selected.sort(key=lambda item: item.relative.as_posix())
    return _TreeManifest(root, root_identity, selected, total_file_bytes)


def _collect_source_tree(root: Path) -> _TreeManifest:
    return _collect_tree(
        root,
        source_tree=True,
        max_entries=MAX_SOURCE_TREE_ENTRIES,
        max_members=MAX_SOURCE_MEMBERS,
        max_total_bytes=MAX_SOURCE_TOTAL_BYTES,
    )


def _collect_stage_tree(root: Path) -> _TreeManifest:
    return _collect_tree(
        root,
        source_tree=False,
        max_entries=MAX_STAGE_TREE_ENTRIES,
        max_members=MAX_STAGE_MEMBERS,
        max_total_bytes=MAX_STAGE_TOTAL_BYTES,
    )


def _read_stable_bytes(path: Path, max_bytes: int, label: str) -> bytes:
    """Read one stable regular file with a one-byte growth sentinel."""
    if max_bytes < 0:
        raise ValueError("file byte limit must be non-negative")
    try:
        initial = path.lstat()
        if not stat.S_ISREG(initial.st_mode) or _is_windows_reparse_point(initial):
            raise StandaloneBuildError(f"{label} is not a regular file: {path}")
        if initial.st_size > max_bytes:
            raise StandaloneBuildError(
                f"{label} exceeds the {max_bytes}-byte limit: {path}"
            )
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _changed(initial, opened):
                raise StandaloneBuildError(f"{label} changed while opening: {path}")
            content = bytearray()
            while True:
                remaining = max_bytes - len(content)
                chunk = stream.read(min(_READ_CHUNK_BYTES, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise StandaloneBuildError(
                        f"{label} exceeds the {max_bytes}-byte limit: {path}"
                    )
                content.extend(chunk)
            finished = os.fstat(stream.fileno())
        current = path.lstat()
    except FileNotFoundError as exc:
        raise StandaloneBuildError(f"{label} disappeared while reading: {path}") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or _is_windows_reparse_point(current)
        or _changed(opened, finished)
        or _changed(finished, current)
        or finished.st_size != len(content)
    ):
        raise StandaloneBuildError(f"{label} changed while reading: {path}")
    return bytes(content)


def _source_date_epoch() -> int | None:
    """Return the reproducible-build timestamp requested by the caller."""
    value = os.environ.get("SOURCE_DATE_EPOCH")
    if value is None:
        return None
    try:
        epoch = int(value)
    except ValueError as exc:
        raise ValueError("SOURCE_DATE_EPOCH must be an integer") from exc
    if not _ZIP_MIN_EPOCH <= epoch <= _ZIP_MAX_EPOCH:
        raise ValueError(
            "SOURCE_DATE_EPOCH must fit the shared ZIP/gzip timestamp range "
            "1980-01-01 through 2106-02-07"
        )
    # ZIP timestamps have a two-second resolution. Normalizing here makes the
    # filesystem value and the value recorded by zipfile unambiguous.
    return epoch - (epoch % 2)


def _normalize_tree_timestamps(
    root: Path, epoch: int, manifest: Optional[_TreeManifest] = None
) -> None:
    bounded = manifest if manifest is not None else _collect_stage_tree(root)
    for entry in bounded.entries:
        os.utime(entry.path, (epoch, epoch))
    os.utime(root, (epoch, epoch))


def _project_version(pyproject_path: Path) -> str:
    """Read the static PEP 621 version without requiring a TOML dependency."""
    text = _read_stable_bytes(
        pyproject_path, MAX_PROJECT_METADATA_BYTES, "project metadata"
    ).decode("utf-8")
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
    version = version_match.group("version")
    if (
        re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9._+!-]{0,126}[A-Za-z0-9])?",
            version,
        )
        is None
        or ".." in version
    ):
        raise ValueError("project.version is unsafe for standalone metadata")
    return version


def _locked_pyyaml_version(lock_path: Path) -> str:
    """Read the exact PyYAML pin used by the public Action and release build."""
    text = _read_stable_bytes(
        lock_path, MAX_TOOL_LOCK_BYTES, "release tool lock"
    ).decode("utf-8")
    matches = re.findall(
        r"(?mi)^PyYAML==(?P<version>[A-Za-z0-9][A-Za-z0-9._+!-]{0,126})"
        r"\s*\\\s*$",
        text,
    )
    if len(matches) != 1:
        raise StandaloneBuildError(
            "release tool lock must contain exactly one exact PyYAML pin"
        )
    return matches[0]


def _is_native_pyyaml_member(relative: Path) -> bool:
    return (
        len(relative.parts) == 1
        and relative.name.startswith("_yaml.")
        and relative.suffix.lower() in {".pyd", ".so"}
    )


def _collect_pyyaml_tree(root: Path) -> _TreeManifest:
    """Select PyYAML's fixed pure-Python surface from a bounded wheel tree."""
    manifest = _collect_tree(
        root,
        source_tree=True,
        max_entries=MAX_VENDORED_TREE_ENTRIES,
        max_members=MAX_VENDORED_MEMBERS,
        max_total_bytes=MAX_VENDORED_TOTAL_BYTES,
    )
    expected = set(_PYYAML_SOURCE_FILES)
    selected: list[_TreeEntry] = []
    seen: set[str] = set()
    native_members = 0
    for entry in manifest.entries:
        name = entry.relative.as_posix()
        if entry.is_directory:
            raise StandaloneBuildError(
                f"installed PyYAML contains an unexpected directory: {name}"
            )
        if name in expected:
            selected.append(entry)
            seen.add(name)
            continue
        if _is_native_pyyaml_member(entry.relative):
            native_members += 1
            if native_members > 1:
                raise StandaloneBuildError(
                    "installed PyYAML contains multiple native extensions"
                )
            continue
        raise StandaloneBuildError(
            f"installed PyYAML contains an unexpected package member: {name}"
        )
    missing = sorted(expected - seen)
    if missing:
        raise StandaloneBuildError(
            "installed PyYAML is missing required pure-Python members: "
            + ", ".join(missing)
        )
    selected.sort(key=lambda item: item.relative.as_posix())
    return _TreeManifest(
        manifest.root,
        manifest.root_identity,
        selected,
        sum(entry.identity.st_size for entry in selected),
    )


def _distribution_member_name(member: object) -> str:
    raw = str(member).replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or ":" in path.parts[0]
        or "\0" in raw
        or path.as_posix() != raw
    ):
        raise StandaloneBuildError(
            f"installed PyYAML metadata contains an unsafe path: {member}"
        )
    return path.as_posix()


def _same_unresolved_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _preflight_pyyaml(lock_path: Path) -> _VendoredPyYAML:
    """Bind vendored PyYAML sources and license to the exact reviewed lock."""
    expected_version = _locked_pyyaml_version(lock_path)
    try:
        distribution = importlib_metadata.distribution("PyYAML")
    except importlib_metadata.PackageNotFoundError as exc:
        raise StandaloneBuildError(
            f"PyYAML {expected_version} must be installed from {lock_path} "
            "before building the standalone archive"
        ) from exc
    if distribution.version != expected_version:
        raise StandaloneBuildError(
            f"installed PyYAML {distribution.version} does not match the "
            f"release tool lock ({expected_version})"
        )
    if distribution.metadata.get("Name", "").casefold() != "pyyaml":
        raise StandaloneBuildError("installed PyYAML distribution name is invalid")

    files = distribution.files
    if files is None:
        raise StandaloneBuildError("installed PyYAML has no file inventory")
    inventory: dict[str, list[object]] = {}
    for member in files:
        inventory.setdefault(_distribution_member_name(member), []).append(member)

    required = {f"yaml/{name}" for name in _PYYAML_SOURCE_FILES}
    for name in sorted(required):
        if len(inventory.get(name, ())) != 1:
            raise StandaloneBuildError(
                f"installed PyYAML metadata must identify exactly one {name}"
            )
    init_member = inventory["yaml/__init__.py"][0]
    package_root = Path(distribution.locate_file(init_member)).parent
    for name in _PYYAML_SOURCE_FILES:
        member = inventory[f"yaml/{name}"][0]
        located = Path(distribution.locate_file(member))
        if not _same_unresolved_path(located, package_root / name):
            raise StandaloneBuildError(
                f"installed PyYAML metadata redirects yaml/{name} outside its package"
            )

    metadata_names = [
        name
        for name in inventory
        if name.casefold().endswith(".dist-info/metadata")
    ]
    if len(metadata_names) != 1:
        raise StandaloneBuildError(
            "installed PyYAML metadata must identify exactly one METADATA file"
        )
    dist_info_prefix = metadata_names[0].rsplit("/", 1)[0] + "/"
    license_members = inventory.get(dist_info_prefix + "licenses/LICENSE", ())
    if len(license_members) != 1:
        raise StandaloneBuildError(
            "installed PyYAML metadata must identify exactly one distribution license"
        )
    license_bytes = _read_stable_bytes(
        Path(distribution.locate_file(license_members[0])),
        MAX_LICENSE_BYTES,
        "PyYAML license",
    )
    if not license_bytes:
        raise StandaloneBuildError("installed PyYAML license is empty")

    manifest = _collect_pyyaml_tree(package_root)
    init_text = _read_stable_bytes(
        package_root / "__init__.py",
        MAX_SOURCE_FILE_BYTES,
        "PyYAML package version source",
    ).decode("utf-8")
    version_matches = re.findall(
        r"(?m)^__version__\s*=\s*['\"]([^'\"]+)['\"]\s*$", init_text
    )
    if version_matches != [expected_version]:
        raise StandaloneBuildError(
            "installed PyYAML package version does not match the release tool lock"
        )
    return _VendoredPyYAML(manifest, license_bytes, expected_version)


def _apply_metadata(path: Path, identity: os.stat_result) -> None:
    os.chmod(path, stat.S_IMODE(identity.st_mode))
    os.utime(path, ns=(identity.st_atime_ns, identity.st_mtime_ns))


def _verify_manifest(manifest: _TreeManifest) -> None:
    try:
        current_root = manifest.root.lstat()
    except FileNotFoundError as exc:
        raise StandaloneBuildError(
            f"tree changed while being consumed: {manifest.root}"
        ) from exc
    if (
        not stat.S_ISDIR(current_root.st_mode)
        or _is_windows_reparse_point(current_root)
        or _changed(manifest.root_identity, current_root)
    ):
        raise StandaloneBuildError(
            f"tree changed while being consumed: {manifest.root}"
        )
    for entry in manifest.entries:
        try:
            current = entry.path.lstat()
        except FileNotFoundError as exc:
            raise StandaloneBuildError(
                f"tree entry disappeared while being consumed: {entry.relative}"
            ) from exc
        expected_type = stat.S_ISDIR if entry.is_directory else stat.S_ISREG
        if (
            not expected_type(current.st_mode)
            or _is_windows_reparse_point(current)
            or _changed(entry.identity, current)
        ):
            raise StandaloneBuildError(
                f"tree entry changed while being consumed: {entry.relative}"
            )


def _copy_manifest(manifest: _TreeManifest, destination: Path) -> None:
    """Copy a preflighted package tree without an unbounded copytree walk."""
    destination.mkdir()
    for entry in manifest.entries:
        if entry.is_directory:
            (destination / entry.relative).mkdir(parents=True, exist_ok=True)

    total = 0
    for entry in manifest.entries:
        if entry.is_directory:
            continue
        remaining_total = MAX_SOURCE_TOTAL_BYTES - total
        if remaining_total <= 0:
            raise StandaloneBuildError(
                "source files exceed the "
                f"{MAX_SOURCE_TOTAL_BYTES}-byte aggregate limit"
            )
        limit = min(MAX_SOURCE_FILE_BYTES, remaining_total)
        try:
            initial = entry.path.lstat()
            if (
                not stat.S_ISREG(initial.st_mode)
                or _is_windows_reparse_point(initial)
                or _changed(entry.identity, initial)
            ):
                raise StandaloneBuildError(
                    f"source file changed before copying: {entry.relative}"
                )
            if initial.st_size > limit:
                message = (
                    f"source file exceeds the {MAX_SOURCE_FILE_BYTES}-byte limit: "
                    f"{entry.relative}"
                    if limit == MAX_SOURCE_FILE_BYTES
                    else "source files exceed the "
                    f"{MAX_SOURCE_TOTAL_BYTES}-byte aggregate limit"
                )
                raise StandaloneBuildError(message)
            target_path = destination / entry.relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with entry.path.open("rb") as source, target_path.open("xb") as target:
                opened = os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode) or _changed(initial, opened):
                    raise StandaloneBuildError(
                        f"source file changed while opening: {entry.relative}"
                    )
                copied = 0
                while True:
                    remaining = limit - copied
                    chunk = source.read(min(_READ_CHUNK_BYTES, remaining + 1))
                    if not chunk:
                        break
                    if len(chunk) > remaining:
                        raise StandaloneBuildError(
                            f"source file grew beyond its byte budget: {entry.relative}"
                        )
                    target.write(chunk)
                    copied += len(chunk)
                finished = os.fstat(source.fileno())
            current = entry.path.lstat()
        except FileNotFoundError as exc:
            raise StandaloneBuildError(
                f"source file disappeared while copying: {entry.relative}"
            ) from exc
        if (
            not stat.S_ISREG(current.st_mode)
            or _is_windows_reparse_point(current)
            or _changed(opened, finished)
            or _changed(finished, current)
            or _changed(entry.identity, current)
            or finished.st_size != copied
        ):
            raise StandaloneBuildError(
                f"source file changed while copying: {entry.relative}"
            )
        _apply_metadata(target_path, entry.identity)
        total += copied

    for entry in reversed(manifest.entries):
        if entry.is_directory:
            _apply_metadata(destination / entry.relative, entry.identity)
    _apply_metadata(destination, manifest.root_identity)
    _verify_manifest(manifest)


def _zip_info(entry: _TreeEntry) -> ZipInfo:
    name = entry.relative.as_posix()
    if entry.is_directory:
        name += "/"
    date_time = time.localtime(entry.identity.st_mtime)[:6]
    info = ZipInfo(name, date_time)
    info.compress_type = ZIP_STORED
    info.external_attr = (entry.identity.st_mode & 0xFFFF) << 16
    if entry.is_directory:
        info.file_size = 0
        info.external_attr |= 0x10
    else:
        info.file_size = entry.identity.st_size
    return info


def _write_archive(
    archive_path: Path, manifest: _TreeManifest
) -> None:
    """Write exactly one bounded manifest as an executable stored ZIP."""
    total = 0
    with archive_path.open("xb") as raw_archive:
        raw_archive.write(b"#!/usr/bin/env python3\n")
        with ZipFile(raw_archive, "w", compression=ZIP_STORED) as archive:
            for entry in manifest.entries:
                info = _zip_info(entry)
                if entry.is_directory:
                    archive.writestr(info, b"")
                    continue
                remaining_total = MAX_STAGE_TOTAL_BYTES - total
                if remaining_total <= 0:
                    raise StandaloneBuildError(
                        "standalone members exceed the "
                        f"{MAX_STAGE_TOTAL_BYTES}-byte aggregate limit"
                    )
                limit = min(MAX_SOURCE_FILE_BYTES, remaining_total)
                try:
                    initial = entry.path.lstat()
                    if (
                        not stat.S_ISREG(initial.st_mode)
                        or _is_windows_reparse_point(initial)
                        or _changed(entry.identity, initial)
                    ):
                        raise StandaloneBuildError(
                            f"standalone member changed before archiving: "
                            f"{entry.relative}"
                        )
                    with entry.path.open("rb") as source, archive.open(
                        info, "w"
                    ) as target:
                        opened = os.fstat(source.fileno())
                        if not stat.S_ISREG(opened.st_mode) or _changed(
                            initial, opened
                        ):
                            raise StandaloneBuildError(
                                f"standalone member changed while opening: "
                                f"{entry.relative}"
                            )
                        copied = 0
                        while True:
                            remaining = limit - copied
                            chunk = source.read(
                                min(_READ_CHUNK_BYTES, remaining + 1)
                            )
                            if not chunk:
                                break
                            if len(chunk) > remaining:
                                raise StandaloneBuildError(
                                    "standalone member grew beyond its byte budget: "
                                    f"{entry.relative}"
                                )
                            target.write(chunk)
                            copied += len(chunk)
                        finished = os.fstat(source.fileno())
                    current = entry.path.lstat()
                except FileNotFoundError as exc:
                    raise StandaloneBuildError(
                        f"standalone member disappeared while archiving: "
                        f"{entry.relative}"
                    ) from exc
                if (
                    not stat.S_ISREG(current.st_mode)
                    or _is_windows_reparse_point(current)
                    or _changed(opened, finished)
                    or _changed(finished, current)
                    or _changed(entry.identity, current)
                    or finished.st_size != copied
                ):
                    raise StandaloneBuildError(
                        f"standalone member changed while archiving: {entry.relative}"
                    )
                total += copied
    _verify_manifest(manifest)
    archive_path.chmod(archive_path.stat().st_mode | stat.S_IEXEC)


def build(output: Path) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    src_pkg = repo_root / "src" / "boundver"
    if not src_pkg.is_dir():
        sys.exit(f"ERROR: boundver package not found at {src_pkg}")
    license_path = repo_root / "LICENSE"
    if not license_path.is_file():
        sys.exit(f"ERROR: license not found at {license_path}")
    try:
        source_manifest = _collect_source_tree(src_pkg)
        vendored_pyyaml = _preflight_pyyaml(
            repo_root / "scripts" / "requirements" / "action.lock"
        )
        version = _project_version(repo_root / "pyproject.toml")
        license_bytes = _read_stable_bytes(
            license_path, MAX_LICENSE_BYTES, "license"
        )
        source_date_epoch = _source_date_epoch()
    except (OSError, UnicodeError, ValueError) as exc:
        sys.exit(f"ERROR: cannot preflight standalone sources: {exc}")

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Stage and build on the destination filesystem so os.replace is atomic.
    with tempfile.TemporaryDirectory(
        dir=output.parent, prefix=".boundver-standalone-"
    ) as temp_dir:
        temp_root = Path(temp_dir)
        stage = temp_root / "stage"
        stage.mkdir()

        # Copy the package tree into the staging dir.
        dest_pkg = stage / "boundver"
        _copy_manifest(source_manifest, dest_pkg)
        _copy_manifest(vendored_pyyaml.manifest, stage / "yaml")

        # Write __main__.py entry point.
        (stage / "__main__.py").write_bytes(
            b"from boundver.cli import main\nmain()\n"
        )

        # importlib.metadata supports distributions stored on a zip sys.path.
        # Including metadata keeps both `--version` and boundver.__version__
        # truthful without adding a second source-of-truth version constant.
        dist_info_name = f"boundver-{version.replace('-', '_')}.dist-info"
        dist_info = stage / dist_info_name
        licenses = dist_info / "licenses"
        licenses.mkdir(parents=True)
        metadata = (
            "Metadata-Version: 2.4\n"
            "Name: boundver\n"
            f"Version: {version}\n"
            "License-Expression: MIT\n"
            "License-File: licenses/LICENSE\n"
            "License-File: licenses/PyYAML-LICENSE\n"
        )
        (dist_info / "METADATA").write_bytes(metadata.encode("utf-8"))
        (dist_info / "VENDORED").write_bytes(
            f"PyYAML=={vendored_pyyaml.version}\n".encode("ascii")
        )
        (licenses / "LICENSE").write_bytes(license_bytes)
        (licenses / "PyYAML-LICENSE").write_bytes(
            vendored_pyyaml.license_bytes
        )
        (stage / "LICENSE").write_bytes(license_bytes)
        stage_manifest = _collect_stage_tree(stage)
        if source_date_epoch is not None:
            _normalize_tree_timestamps(stage, source_date_epoch, stage_manifest)
            stage_manifest = _collect_stage_tree(stage)

        archive = temp_root / output.name
        _write_archive(archive, stage_manifest)
        os.replace(archive, output)

    size_kb = output.stat().st_size // 1024
    print(f"Built {output}  ({size_kb} KB)")
    print(f"Run with:  python3 {output} --help")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--output", "-o",
        default="dist/boundver.pyz",
        help="Output path for the .pyz archive (default: dist/boundver.pyz)",
    )
    args = parser.parse_args()
    build(Path(args.output))


if __name__ == "__main__":
    main()
