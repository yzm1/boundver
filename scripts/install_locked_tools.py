#!/usr/bin/env python3
"""Install one checked-in automation profile with pip secure-install flags."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
LOCKS = {
    "action": ROOT / "scripts" / "requirements" / "action.lock",
    "ci": ROOT / "scripts" / "requirements" / "ci.lock",
    "docs": ROOT / "scripts" / "requirements" / "docs.lock",
    "release": ROOT / "scripts" / "requirements" / "release.lock",
}
MINIMUM_PYTHON = {
    "release": (3, 12),
}
MAX_INSTALL_SECONDS = 1_800


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=tuple(LOCKS))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    minimum = MINIMUM_PYTHON.get(args.profile)
    if minimum is not None and sys.version_info[:2] < minimum:
        required = ".".join(str(part) for part in minimum)
        current = ".".join(str(part) for part in sys.version_info[:2])
        print(
            f"ERROR: locked {args.profile} tools require Python {required} "
            f"or newer; current interpreter is Python {current}",
            file=sys.stderr,
        )
        return 2
    command = (
        sys.executable,
        "-I",
        "-m",
        "pip",
        "--isolated",
        "install",
        "--disable-pip-version-check",
        "--quiet",
        "--upgrade",
        "--no-cache-dir",
        "--require-hashes",
        "--only-binary=:all:",
        "--index-url",
        "https://pypi.org/simple",
        "--requirement",
        str(LOCKS[args.profile]),
    )
    try:
        return subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=MAX_INSTALL_SECONDS,
        ).returncode
    except subprocess.TimeoutExpired:
        print(
            f"ERROR: locked {args.profile} tool installation exceeds the "
            f"{MAX_INSTALL_SECONDS}-second wall-clock limit",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
