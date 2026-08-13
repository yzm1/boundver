"""Shared utilities, enums, and exception types for boundver."""

import fnmatch
import re
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Optional, Set


# Python 3.11+ limits decimal-to-int conversion to 4,300 digits by default,
# while earlier supported interpreters do not.  Enforce one explicit contract
# so parsing the same JSON cannot depend on the runner's Python version.
MAX_JSON_INTEGER_DIGITS = 4300
_MAX_JSON_INTEGER_ABS = 10 ** MAX_JSON_INTEGER_DIGITS


def _bounded_json_int(value: str) -> int:
    """Parse one JSON integer under the cross-version decimal-size limit."""
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(
            "JSON integer exceeds the "
            f"{MAX_JSON_INTEGER_DIGITS}-decimal-digit limit"
        )
    return int(value)


def _json_integer_is_bounded(value: int) -> bool:
    """Return whether *value* fits the JSON decimal-size contract."""
    return abs(value) < _MAX_JSON_INTEGER_ABS


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


def _normalize_declared_path(path: str) -> str:
    """Return a canonical component/repository-relative declared path.

    The same validation is used before hashing and by config diagnostics so a
    path cannot mean one thing in one source mode and another elsewhere.
    A trailing slash is accepted for directory literals; all other redundant
    or unsafe segments are rejected.
    """
    if not isinstance(path, str):
        raise ValueError("must be a string")
    if not path or not path.strip():
        raise ValueError("must not be empty or whitespace")
    if path != path.strip():
        raise ValueError("must not have leading or trailing whitespace")
    if "\\" in path:
        raise ValueError("must use '/' separators")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ValueError("must be relative")
    normalized = path.rstrip("/")
    parts = normalized.split("/")
    if any(part == "" for part in parts):
        raise ValueError("must not contain empty path segments")
    if "." in parts:
        raise ValueError("must not contain '.' path segments")
    if ".." in parts:
        raise ValueError(
            "must not contain '..' path segments; the path escapes its declared root"
        )
    return normalized


def _match_path_glob(path: str, pattern: str) -> bool:
    """Match a POSIX path with deterministic, segment-aware glob semantics.

    Ordinary wildcard segments use :func:`fnmatch.fnmatchcase`, so ``*``,
    ``?``, and character classes never cross ``/``.  A segment that is exactly
    ``**`` consumes zero or more complete path segments.  Matching is always
    case-sensitive and, like ``fnmatch``, includes leading-dot names.
    """
    if not isinstance(path, str) or not isinstance(pattern, str):
        return False
    if path.startswith("/") or pattern.startswith("/"):
        return False
    path_parts = tuple(path.split("/")) if path else tuple()
    pattern_parts = tuple(pattern.split("/")) if pattern else tuple()
    if any(part == "" for part in path_parts + pattern_parts):
        return False

    @lru_cache(maxsize=None)
    def _matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        token = pattern_parts[pattern_index]
        if token == "**":
            return _matches(path_index, pattern_index + 1) or (
                path_index < len(path_parts)
                and _matches(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], token)
            and _matches(path_index + 1, pattern_index + 1)
        )

    return _matches(0, 0)


_FACET_ISSUE_RE = re.compile(
    r"^(?:MISMATCH|SLICE MISMATCH|UNAVAILABLE FACET) "
    r".+\.(exact|behavior|boundary|compat):",
    re.DOTALL,
)


def _issue_facet(message: str) -> Optional[str]:
    """Return the structured facet encoded by a verification issue."""
    match = _FACET_ISSUE_RE.match(message)
    return match.group(1) if match else None


def _available_component_facets(component: Mapping[str, object]) -> Set[str]:
    """Return facets the declaration can intentionally produce.

    This is the policy-free fallback used by verify and its JSON policy view.
    It describes declared capability, not computation success: provider or
    source failures are reported independently as digest errors.
    """
    available = {"exact"}
    boundary = component.get("boundary")
    if isinstance(boundary, dict):
        provider = boundary_provider_name(boundary)
        paths = boundary.get("paths", [])
        if provider not in {"leaf", "implicit"} or (
            provider == "implicit" and isinstance(paths, list) and bool(paths)
        ):
            available.add("boundary")
    behavior = component.get("behavior")
    if isinstance(behavior, dict):
        paths = behavior.get("paths")
        if isinstance(paths, list) and bool(paths):
            available.add("behavior")
    if isinstance(component.get("version_source"), dict):
        available.add("compat")
    return available


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
    except (ValueError, OSError):
        return False
