#!/usr/bin/env python3
"""Print the release before this one, from the repository's own tags.

The previous-release CI job needs a version to install, and hard-coding one
means it goes stale silently the first time nobody remembers to bump it.
Deriving it from the tags keeps the job honest at the cost of one git call.

Only tags of the form vMAJOR.MINOR.PATCH count. The shortened aliases the
project also publishes, such as v0.14, are not installable versions.

Exit codes follow the boundver convention: 0 on success, 2 when no previous
release can be determined.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def released_versions() -> list:
    result = subprocess.run(
        ["git", "tag"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    versions = []
    for line in result.stdout.splitlines():
        match = RELEASE_TAG.match(line.strip())
        if match:
            versions.append(tuple(int(part) for part in match.groups()))
    return sorted(set(versions))


def current_version() -> tuple:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text, re.MULTILINE)
    if match is None:
        raise ValueError("pyproject.toml has no three-part version")
    return tuple(int(part) for part in match.groups())


def previous_release() -> tuple:
    current = current_version()
    earlier = [version for version in released_versions() if version < current]
    if not earlier:
        raise ValueError(
            f"no released tag precedes {'.'.join(str(p) for p in current)}"
        )
    return earlier[-1]


def main() -> int:
    try:
        print(".".join(str(part) for part in previous_release()))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
