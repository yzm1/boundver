#!/usr/bin/env python3
"""
Build a standalone boundver.pyz zipapp — no pip install required.

Usage:
    python scripts/build_standalone.py
    python scripts/build_standalone.py --output dist/boundver.pyz

The resulting .pyz file is self-contained (no external deps beyond Python 3.8+
stdlib) and can be run directly:

    python3 boundver.pyz generate
    python3 boundver.pyz verify

On Unix systems you can also make it executable:
    chmod +x dist/boundver.pyz
    ./dist/boundver.pyz verify

Requires:
  - Python 3.8+
  - No external packages (boundver itself has none)
"""

import argparse
import shutil
import sys
import zipapp
from pathlib import Path


def build(output: Path) -> None:
    repo_root = Path(__file__).parent.parent
    src_pkg = repo_root / "src" / "boundver"
    if not src_pkg.is_dir():
        sys.exit(f"ERROR: boundver package not found at {src_pkg}")

    # Build in a staging directory next to the output file.
    stage = output.parent / "_stage_boundver"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    try:
        # Copy the package tree into the staging dir.
        dest_pkg = stage / "boundver"
        shutil.copytree(src_pkg, dest_pkg, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))

        # Write __main__.py entry point.
        (stage / "__main__.py").write_text(
            "from boundver.cli import main\nmain()\n", encoding="utf-8"
        )

        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()

        zipapp.create_archive(
            stage,
            target=str(output),
            interpreter="/usr/bin/env python3",
            compressed=True,
        )
    finally:
        shutil.rmtree(stage, ignore_errors=True)

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
