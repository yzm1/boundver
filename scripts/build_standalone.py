#!/usr/bin/env python3
"""
Build a standalone boundver.pyz zipapp — no pip install required.

Usage:
    python scripts/build_standalone.py
    python scripts/build_standalone.py --output dist/boundver.pyz

The resulting .pyz file is self-contained (no external deps beyond Python 3.9+
stdlib) and can be run directly:

    python3 boundver.pyz generate
    python3 boundver.pyz verify

On Unix systems you can also make it executable:
    chmod +x dist/boundver.pyz
    ./dist/boundver.pyz verify

Requires:
  - Python 3.9+ for JSON configs
  - Python 3.11+ for TOML configs in the standalone archive; YAML support is
    optional
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path


_ZIP_MIN_EPOCH = 315532800  # 1980-01-01T00:00:00Z
_ZIP_MAX_EPOCH = 4294967294  # latest even timestamp also representable by gzip


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


def _normalize_tree_timestamps(root: Path, epoch: int) -> None:
    for path in sorted(root.rglob("*")):
        # ``stage`` is a fresh copytree of regular package files/directories;
        # the Windows Python API does not support follow_symlinks=False here.
        os.utime(path, (epoch, epoch))
    os.utime(root, (epoch, epoch))


def _project_version(pyproject_path: Path) -> str:
    """Read the static PEP 621 version without requiring a TOML dependency."""
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


def build(output: Path) -> None:
    repo_root = Path(__file__).parent.parent
    src_pkg = repo_root / "src" / "boundver"
    if not src_pkg.is_dir():
        sys.exit(f"ERROR: boundver package not found at {src_pkg}")
    license_path = repo_root / "LICENSE"
    if not license_path.is_file():
        sys.exit(f"ERROR: license not found at {license_path}")
    try:
        version = _project_version(repo_root / "pyproject.toml")
        source_date_epoch = _source_date_epoch()
    except (OSError, ValueError) as exc:
        sys.exit(f"ERROR: cannot determine boundver version: {exc}")

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
        shutil.copytree(src_pkg, dest_pkg, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))

        # Write __main__.py entry point.
        (stage / "__main__.py").write_text(
            "from boundver.cli import main\nmain()\n", encoding="utf-8"
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
        )
        (dist_info / "METADATA").write_text(metadata, encoding="utf-8")
        shutil.copy2(license_path, licenses / "LICENSE")
        shutil.copy2(license_path, stage / "LICENSE")
        if source_date_epoch is not None:
            _normalize_tree_timestamps(stage, source_date_epoch)

        archive = temp_root / output.name
        zipapp.create_archive(
            stage,
            target=str(archive),
            interpreter="/usr/bin/env python3",
            # Stored members make the byte stream independent of the runner's
            # zlib version. The archive remains a normal executable zipapp.
            compressed=False,
        )
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
