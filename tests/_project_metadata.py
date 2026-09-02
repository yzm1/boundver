"""Project metadata shared by tests that model the current release."""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


REPO_ROOT = Path(__file__).resolve().parents[1]
with (REPO_ROOT / "pyproject.toml").open("rb") as pyproject_file:
    CURRENT_VERSION = str(tomllib.load(pyproject_file)["project"]["version"])

CURRENT_TAG = f"v{CURRENT_VERSION}"
CURRENT_MINOR_TAG = f"v{CURRENT_VERSION.rsplit('.', 1)[0]}"
