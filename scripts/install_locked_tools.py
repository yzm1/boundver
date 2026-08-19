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
    "release": ROOT / "scripts" / "requirements" / "release.lock",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=tuple(LOCKS))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
        "--require-hashes",
        "--only-binary=:all:",
        "--index-url",
        "https://pypi.org/simple",
        "--requirement",
        str(LOCKS[args.profile]),
    )
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
