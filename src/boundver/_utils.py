"""Shared utilities, enums, and exception types for boundver."""

from enum import Enum
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Source mode enum
# ---------------------------------------------------------------------------

class SourceMode(str, Enum):
    """Which version of the file tree to fingerprint.

    Inherits from ``str`` so instances compare equal to their value string
    and can be passed directly to functions expecting ``str``.
    """

    HEAD = "head"
    INDEX = "index"
    WORKING_TREE = "working-tree"

    def __str__(self) -> str:  # pragma: no cover
        return self.value


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class BoundverError(Exception):
    """Base for all boundver-specific errors."""


class ConfigError(BoundverError, ValueError):
    """Configuration file is invalid or missing."""


class LockfileError(BoundverError, ValueError):
    """Lockfile is malformed, missing, or cannot be migrated."""


class ProviderError(BoundverError, ValueError):
    """A boundary provider failed to load or execute."""


class GuardrailError(BoundverError, ValueError):
    """A safety guardrail was triggered (file count, size, etc.)."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _is_glob(pattern: str) -> bool:
    """Return True if the pattern contains glob metacharacters."""
    return any(c in pattern for c in ("*", "?", "["))


def boundary_provider_name(boundary: dict) -> str:
    """Return boundary provider name from a component's boundary config."""
    return boundary.get("provider") or "unknown"


def _short(h: Optional[str]) -> str:
    """Truncate a hex digest for display."""
    if h is None:
        return "none"
    return h[:12] + "..."


def _is_within(base: Path, candidate: Path) -> bool:
    """Return True if candidate path is within base path."""
    try:
        candidate.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False
