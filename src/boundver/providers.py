"""
Provider protocol for boundver boundary extraction.

Phase 1: protocol types, raw-byte built-in wrappers, registry, and
compute_boundary().

Phase 2: load_custom_providers() — loads and registers custom providers from
the `providers` config key. Requires --allow-custom-providers at runtime.

Phase 3: semantic built-ins — openapi-canonical, json-canonical.
  These produce digests that are stable across formatting/comment changes but
  change when the logical API contract changes. They are opt-in (activated only
  by explicit provider name) and have zero impact on existing lockfiles.

The public extension and trust model is documented in
docs/public-vs-custom-providers.md.
"""
from __future__ import annotations

import importlib
import json as _json_mod
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from ._hashing import HASH_DOMAIN_BOUNDARY, _ModeAwareBytes, _hash_framed_entries
from ._utils import (
    ProviderError,
    _is_glob,
    _match_path_glob,
    _normalize_declared_path,
)


def _join_repo_path(component_path: str, relative_path: str) -> str:
    """Join config paths without treating a root component as ``./``."""
    prefix = component_path.strip().replace("\\", "/").strip("/")
    while prefix.startswith("./"):
        prefix = prefix[2:]
    if prefix in {"", "."}:
        return relative_path.lstrip("/")
    return f"{prefix}/{relative_path.lstrip('/')}"


def _component_relative_path(component_path: str, repo_relative_path: str) -> str:
    """Return a stable component-relative label for a repo-relative path."""
    prefix = component_path.strip().replace("\\", "/").strip("/")
    while prefix.startswith("./"):
        prefix = prefix[2:]
    # Git already emits '/' as its separator on every platform.  A backslash
    # returned here is therefore a literal filename byte on POSIX and must not
    # be rewritten into a separator (which would collapse distinct labels).
    path = repo_relative_path.lstrip("/")
    if prefix in {"", "."}:
        return path
    expected = prefix + "/"
    if path.startswith(expected):
        return path[len(expected):]
    if path == prefix:
        return Path(path).name
    raise ProviderError(
        f"Provider returned path outside component '{component_path}': {repo_relative_path}"
    )


def _resolve_declared_files(
    ctx: "ProviderContext",
    paths: Any,
) -> tuple[List[tuple[str, str]], List[str]]:
    """Resolve declared literals/globs to unique tracked repository files.

    Returns ``[(repo_relative, component_relative), ...]`` plus errors.  This
    is the single selection path for raw and canonical built-ins, which keeps
    ``*``/``**`` behavior and unmatched-declaration failures identical.
    """
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        return [], ["Boundary paths must be an array of strings"]

    seen: set[str] = set()
    selected: List[tuple[str, str]] = []
    errors: List[str] = []
    all_component_files: Optional[List[str]] = None

    def component_files() -> List[str]:
        nonlocal all_component_files
        if all_component_files is None:
            all_component_files = sorted(ctx.list_files(ctx.component_path))
        return all_component_files

    for declared in sorted(paths):
        try:
            rel = _normalize_declared_path(declared)
        except ValueError as exc:
            errors.append(f"Invalid declared boundary path {declared!r}: {exc}")
            continue

        if _is_glob(rel):
            matches = []
            for repo_rel in component_files():
                child_rel = _component_relative_path(ctx.component_path, repo_rel)
                if _match_path_glob(child_rel, rel):
                    matches.append((repo_rel, child_rel))
        else:
            matches = [
                (repo_rel, _component_relative_path(ctx.component_path, repo_rel))
                for repo_rel in sorted(ctx.list_files(_join_repo_path(ctx.component_path, rel)))
            ]

        if not matches:
            errors.append(f"Declared boundary path matched no tracked files: {rel}")
            continue
        for repo_rel, child_rel in matches:
            if repo_rel not in seen:
                seen.add(repo_rel)
                selected.append((repo_rel, child_rel))

    selected.sort(key=lambda item: item[1].encode("utf-8", errors="surrogateescape"))
    return selected, errors


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ProviderContext:
    """Passed to every provider method.  Core injects git-aware callbacks so
    providers never call git or hashlib directly."""

    repo_root: Path
    component_path: str      # repo-relative, e.g. "services/billing"
    boundary_cfg: dict       # full boundary config dict for this component
    source: str              # "head" | "index" | "working-tree"

    # Injected by core — providers use these instead of calling git directly.
    read_file: Callable[[str], bytes]        # repo_rel_path → raw bytes
    list_files: Callable[[str], List[str]]   # repo_rel_prefix → [repo_rel_path, ...]


@dataclass
class ResolvedBoundary:
    """What a provider returns from resolve().

    ``entries`` is an ordered list of (label, content) pairs.
    Core hashes them with boundver's length-delimited, domain-separated wire
    format.

    Labels MUST be deterministic and sorted by the provider.
    For path-based providers the label is ``file:<component-relative-path>``.
    """

    entries: List[tuple] = field(default_factory=list)  # List[tuple[str, bytes]]

    status: str = "ok"   # "ok" | "partial" | "error"
    errors: List[str] = field(default_factory=list)

    # Optional structured metadata stored in the lockfile alongside the digest.
    # Passed back to explain_diff() when available.  verify() ignores it.
    metadata: Optional[dict] = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class BoundaryProvider(Protocol):
    """Interface every provider must satisfy."""

    name: str  # Stable identifier — must match boundary.provider in config.
    version: str  # Bump when hashing algorithm changes for this provider.

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        """Return the normalised content to be hashed.

        MUST:
        - Return entries in deterministic (sorted) order.
        - Not call subprocesses beyond ctx.read_file / ctx.list_files.
        - Not mutate any file on disk.
        - Return status="error" rather than raising for expected failures.
        """
        ...  # pragma: no cover

    def validate_config(
        self,
        boundary_cfg: dict,
        component_path: str,
        repo_root: Path,
    ) -> List[str]:
        """Return validation error strings.  Empty list = valid."""
        ...  # pragma: no cover

    def explain_diff(
        self,
        old_metadata: Optional[dict],
        new_metadata: Optional[dict],
        ctx: ProviderContext,
    ) -> str:
        """One-line human summary of what changed between two digests.

        If metadata is None (old lockfiles without metadata), return a generic
        message.
        """
        ...  # pragma: no cover


ProviderRegistry = Dict[str, BoundaryProvider]


# Provider results are untrusted extension data.  Keep their in-memory and
# lockfile footprint bounded independently of whichever source accessor a
# provider chooses to use.
MAX_PROVIDER_ENTRIES = 50_000
MAX_PROVIDER_ENTRY_BYTES = 50 * 1024 * 1024
MAX_PROVIDER_TOTAL_BYTES = 256 * 1024 * 1024
MAX_PROVIDER_LABEL_BYTES = 16 * 1024
MAX_PROVIDER_TOTAL_LABEL_BYTES = 16 * 1024 * 1024
MAX_PROVIDER_ERRORS = 100
MAX_PROVIDER_ERROR_BYTES = 16 * 1024
MAX_PROVIDER_METADATA_BYTES = 1024 * 1024
MAX_PROVIDER_METADATA_DEPTH = 64
MAX_PROVIDER_METADATA_NODES = 100_000

_VALID_BOUNDARY_STATUSES = frozenset({"ok", "partial", "error"})


def _safe_provider_attribute(provider: Any, attribute: str) -> Any:
    """Read an extension attribute without allowing a descriptor to escape."""
    try:
        return getattr(provider, attribute)
    except Exception as exc:
        raise ProviderError(
            f"Provider attribute '{attribute}' could not be read: "
            f"{_bounded_exception(exc)}"
        ) from exc


def _bounded_exception(exc: Exception) -> str:
    """Return a useful, bounded exception description for user-facing errors."""
    try:
        detail = str(exc).strip()
    except Exception:
        detail = ""
    detail = detail or exc.__class__.__name__
    detail = detail.encode("utf-8", errors="backslashreplace").decode("utf-8")
    if len(detail) > 500:
        detail = detail[:497] + "..."
    return detail


def _provider_identity_error(provider: Any) -> Optional[str]:
    """Return a provider protocol error, or ``None`` for a usable provider."""
    for attribute in ("name", "version"):
        try:
            value = _safe_provider_attribute(provider, attribute)
        except ProviderError as exc:
            return str(exc)
        if type(value) is not str or not value.strip():
            return f"Provider {attribute} must be a non-empty string"
        if value != value.strip():
            return f"Provider {attribute} must not have leading or trailing whitespace"
        try:
            encoded_value = value.encode("utf-8")
        except UnicodeEncodeError:
            return f"Provider {attribute} must contain valid Unicode"
        if len(encoded_value) > 256:
            return f"Provider {attribute} exceeds the 256-byte limit"
    try:
        resolver = _safe_provider_attribute(provider, "resolve")
    except ProviderError as exc:
        return str(exc)
    if not callable(resolver):
        return "Provider resolve must be callable"
    return None


def _metadata_error(metadata: Any) -> Optional[str]:
    """Validate that provider metadata is a bounded JSON object."""
    if metadata is None:
        return None
    if type(metadata) is not dict:
        return "metadata must be a JSON object or null"

    nodes = 0
    text_bytes = 0
    active: set[int] = set()

    def visit(value: Any, depth: int) -> Optional[str]:
        nonlocal nodes, text_bytes
        nodes += 1
        if nodes > MAX_PROVIDER_METADATA_NODES:
            return (
                "metadata exceeds the "
                f"{MAX_PROVIDER_METADATA_NODES}-value limit"
            )
        if depth > MAX_PROVIDER_METADATA_DEPTH:
            return (
                "metadata exceeds the "
                f"{MAX_PROVIDER_METADATA_DEPTH}-level nesting limit"
            )
        if type(value) is str:
            try:
                text_bytes += len(value.encode("utf-8"))
            except UnicodeEncodeError:
                return "metadata strings must contain valid Unicode"
            if text_bytes > MAX_PROVIDER_METADATA_BYTES:
                return (
                    "metadata text exceeds the "
                    f"{MAX_PROVIDER_METADATA_BYTES}-byte JSON limit"
                )
            return None
        if value is None or type(value) is bool:
            return None
        if type(value) is int:
            # A decimal integer needs at least roughly one byte per four bits.
            # Reject absurd values before asking json.dumps() to materialize
            # their complete decimal representation.
            if value.bit_length() > MAX_PROVIDER_METADATA_BYTES * 4:
                return (
                    "metadata integer exceeds the "
                    f"{MAX_PROVIDER_METADATA_BYTES}-byte JSON limit"
                )
            return None
        if type(value) is float:
            if not math.isfinite(value):
                return "metadata contains a non-finite number"
            return None
        if type(value) not in {list, dict}:
            return f"metadata contains non-JSON value {type(value).__name__}"

        object_id = id(value)
        if object_id in active:
            return "metadata contains a reference cycle"
        active.add(object_id)
        try:
            if type(value) is list:
                for item in value:
                    error = visit(item, depth + 1)
                    if error:
                        return error
                return None
            for key, item in value.items():
                if type(key) is not str:
                    return "metadata object keys must be strings"
                try:
                    text_bytes += len(key.encode("utf-8"))
                except UnicodeEncodeError:
                    return "metadata object keys must contain valid Unicode"
                if text_bytes > MAX_PROVIDER_METADATA_BYTES:
                    return (
                        "metadata text exceeds the "
                        f"{MAX_PROVIDER_METADATA_BYTES}-byte JSON limit"
                    )
                error = visit(item, depth + 1)
                if error:
                    return error
            return None
        finally:
            active.remove(object_id)

    error = visit(metadata, 0)
    if error:
        return error
    try:
        encoded = _json_mod.dumps(
            metadata,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        return f"metadata is not safely JSON serializable: {_bounded_exception(exc)}"
    if len(encoded) > MAX_PROVIDER_METADATA_BYTES:
        return (
            f"metadata exceeds the {MAX_PROVIDER_METADATA_BYTES}-byte JSON limit"
        )
    return None


def _resolved_boundary_error(resolved: Any) -> Optional[str]:
    """Validate the complete, untrusted result returned by ``resolve()``."""
    if type(resolved) is not ResolvedBoundary:
        return "resolve() must return exactly ResolvedBoundary"
    if type(resolved.status) is not str or resolved.status not in _VALID_BOUNDARY_STATUSES:
        return "status must be one of: ok, partial, error"
    if type(resolved.errors) is not list:
        return "errors must be a list of non-empty strings"
    if len(resolved.errors) > MAX_PROVIDER_ERRORS:
        return f"errors exceeds the {MAX_PROVIDER_ERRORS}-item limit"
    for error in resolved.errors:
        if type(error) is not str or not error.strip():
            return "errors must contain only non-empty strings"
        try:
            encoded_error = error.encode("utf-8")
        except UnicodeEncodeError:
            return "errors must contain valid Unicode"
        if len(encoded_error) > MAX_PROVIDER_ERROR_BYTES:
            return (
                "an error message exceeds the "
                f"{MAX_PROVIDER_ERROR_BYTES}-byte limit"
            )
    if resolved.status == "ok" and resolved.errors:
        return "status 'ok' cannot include errors"
    if resolved.status in {"partial", "error"} and not resolved.errors:
        return f"status '{resolved.status}' requires at least one error"
    if resolved.status == "error" and resolved.entries:
        return "status 'error' cannot include hash entries"

    if type(resolved.entries) is not list:
        return "entries must be a list of (label, bytes) tuples"
    if len(resolved.entries) > MAX_PROVIDER_ENTRIES:
        return f"entries exceeds the {MAX_PROVIDER_ENTRIES}-item limit"
    previous_label: Optional[bytes] = None
    total_bytes = 0
    total_label_bytes = 0
    for entry in resolved.entries:
        if type(entry) is not tuple or len(entry) != 2:
            return "entries must contain exact two-item tuples"
        label, content = entry
        if type(label) is not str or not label:
            return "entry labels must be non-empty strings"
        try:
            # Git filenames on POSIX may contain undecodable bytes represented
            # by Python's DC80-DCFF surrogateescape range.  Preserve those
            # bytes; unrelated/lone surrogates still raise here.
            label_bytes = label.encode("utf-8", errors="surrogateescape")
        except UnicodeEncodeError:
            return "entry labels must contain valid Unicode or surrogateescaped Git bytes"
        if len(label_bytes) > MAX_PROVIDER_LABEL_BYTES:
            return (
                "an entry label exceeds the "
                f"{MAX_PROVIDER_LABEL_BYTES}-byte limit"
            )
        total_label_bytes += len(label_bytes)
        if total_label_bytes > MAX_PROVIDER_TOTAL_LABEL_BYTES:
            return (
                "entry labels exceed the "
                f"{MAX_PROVIDER_TOTAL_LABEL_BYTES}-byte aggregate limit"
            )
        if previous_label is not None and label_bytes <= previous_label:
            return "entries must have unique labels in deterministic sorted order"
        previous_label = label_bytes
        if type(content) not in {bytes, _ModeAwareBytes}:
            return "entry content must be bytes"
        if len(content) > MAX_PROVIDER_ENTRY_BYTES:
            return (
                f"entry '{label}' exceeds the "
                f"{MAX_PROVIDER_ENTRY_BYTES}-byte limit"
            )
        total_bytes += len(content)
        if total_bytes > MAX_PROVIDER_TOTAL_BYTES:
            return (
                "entries exceed the "
                f"{MAX_PROVIDER_TOTAL_BYTES}-byte aggregate limit"
            )

    metadata_error = _metadata_error(resolved.metadata)
    if metadata_error:
        return metadata_error
    return None


def _controlled_boundary_error(
    message: str,
    *,
    include_metadata: bool,
) -> tuple:
    result: tuple = (None, "error", [message])
    return result + (None,) if include_metadata else result


# ---------------------------------------------------------------------------
# Core hashing function — the ONLY place SHA-256 is called for boundary digests
# ---------------------------------------------------------------------------

def compute_boundary(
    provider: BoundaryProvider,
    ctx: ProviderContext,
    *,
    include_metadata: bool = False,
) -> tuple:
    """Ask the provider to resolve content, then hash it.

    By default, returns the historical three-item tuple
    ``(digest_or_None, status, errors)``.  Callers that persist or explain
    provider metadata can pass ``include_metadata=True`` to receive
    ``(digest_or_None, status, errors, metadata)`` instead.

    Hashing uses the shared, length-delimited boundary domain so entry content
    cannot be confused with the framing for a later entry.
    """
    identity_error = _provider_identity_error(provider)
    if identity_error:
        return _controlled_boundary_error(
            f"Invalid boundary provider: {identity_error}",
            include_metadata=include_metadata,
        )
    try:
        provider_name = _safe_provider_attribute(provider, "name")
        resolver = _safe_provider_attribute(provider, "resolve")
    except ProviderError as exc:
        return _controlled_boundary_error(
            f"Invalid boundary provider: {exc}",
            include_metadata=include_metadata,
        )
    if type(provider_name) is not str or not callable(resolver):
        return _controlled_boundary_error(
            "Invalid boundary provider: provider identity changed during validation",
            include_metadata=include_metadata,
        )
    try:
        resolved = resolver(ctx)
    except Exception as exc:
        return _controlled_boundary_error(
            f"Provider '{provider_name}' resolve() failed: {_bounded_exception(exc)}",
            include_metadata=include_metadata,
        )
    result_error = _resolved_boundary_error(resolved)
    if result_error:
        return _controlled_boundary_error(
            f"Provider '{provider_name}' returned an invalid result: {result_error}",
            include_metadata=include_metadata,
        )
    if (
        resolved.status == "ok"
        and not resolved.entries
        and type(provider) is not LeafProvider
    ):
        return _controlled_boundary_error(
            f"Provider '{provider_name}' returned status 'ok' without publishing entries",
            include_metadata=include_metadata,
        )
    if (
        resolved.status == "partial"
        and not resolved.entries
        and type(provider) is not ImplicitProvider
    ):
        return _controlled_boundary_error(
            f"Provider '{provider_name}' returned an empty partial result; "
            "only the built-in implicit provider may omit a boundary",
            include_metadata=include_metadata,
        )
    result: tuple
    if resolved.status == "error" or not resolved.entries:
        result = (None, resolved.status or "error", resolved.errors)
    else:
        try:
            digest = _hash_framed_entries(
                resolved.entries, domain=HASH_DOMAIN_BOUNDARY
            )
        except Exception as exc:
            return _controlled_boundary_error(
                f"Provider '{provider_name}' entries could not be hashed: "
                f"{_bounded_exception(exc)}",
                include_metadata=include_metadata,
            )
        result = (digest, resolved.status, resolved.errors)
    if include_metadata:
        return result + (resolved.metadata,)
    return result


def validate_provider_config(
    provider: BoundaryProvider,
    boundary_cfg: dict,
    component_path: str,
    repo_root: Path,
) -> List[str]:
    """Invoke a provider's optional validation hook with stable error handling.

    Older third-party providers that predate the hook remain usable.  A broken
    hook becomes an actionable validation error rather than crashing the CLI.
    """
    try:
        validator = getattr(provider, "validate_config", None)
    except Exception as exc:
        return [
            "Provider config validation hook could not be read: "
            f"{_bounded_exception(exc)}"
        ]
    if not callable(validator):
        return []
    try:
        provider_name = getattr(provider, "name", provider.__class__.__name__)
    except Exception:
        provider_name = provider.__class__.__name__
    try:
        errors = validator(boundary_cfg, component_path, repo_root)
    except Exception as exc:
        return [
            f"Provider '{provider_name}' config validation failed: "
            f"{_bounded_exception(exc)}"
        ]
    if errors is None:
        return []
    if not isinstance(errors, list):
        return [
            f"Provider '{provider_name}' validate_config() must return a list of errors"
        ]
    if len(errors) > MAX_PROVIDER_ERRORS:
        return [
            f"Provider '{provider_name}' validate_config() returned more than "
            f"{MAX_PROVIDER_ERRORS} errors"
        ]
    for error in errors:
        if type(error) is not str or not error.strip():
            return [
                f"Provider '{provider_name}' validate_config() must return only "
                "non-empty error strings"
            ]
        try:
            encoded_error = error.encode("utf-8")
        except UnicodeEncodeError:
            return [
                f"Provider '{provider_name}' validate_config() returned an error "
                "that is not valid Unicode"
            ]
        if len(encoded_error) > MAX_PROVIDER_ERROR_BYTES:
            return [
                f"Provider '{provider_name}' validate_config() returned an error "
                f"longer than {MAX_PROVIDER_ERROR_BYTES} bytes"
            ]
    return list(errors)


def explain_provider_diff(
    provider: BoundaryProvider,
    old_metadata: Optional[dict],
    new_metadata: Optional[dict],
    ctx: ProviderContext,
) -> str:
    """Return a provider-specific diff summary with a useful fallback."""
    try:
        provider_name = getattr(provider, "name", provider.__class__.__name__)
    except Exception:
        provider_name = provider.__class__.__name__
    fallback = f"{provider_name} boundary changed"
    try:
        explainer = getattr(provider, "explain_diff", None)
    except Exception:
        return fallback
    if not callable(explainer):
        return fallback
    try:
        explanation = explainer(old_metadata, new_metadata, ctx)
    except Exception as exc:
        return (
            f"{fallback} (provider explanation unavailable: "
            f"{_bounded_exception(exc)})"
        )
    if isinstance(explanation, str) and explanation.strip():
        explanation = explanation.strip()
        if len(explanation.encode("utf-8", errors="replace")) <= MAX_PROVIDER_ERROR_BYTES:
            return explanation
    return fallback


# ---------------------------------------------------------------------------
# Built-in provider base: PathHashProvider
# ---------------------------------------------------------------------------

class PathHashProvider:
    """Hash declared boundary paths as raw normalised bytes.

    Replicates the historical provider's content selection; core applies the
    current unambiguous hashing wire format.

    Subclasses set ``name`` to the provider identifier string.
    """

    # The base class is public and useful for behavior slices, so it also has a
    # complete provider identity even though named subclasses override it.
    name: str = "path-hash"
    # v2 gives *, ?, and character classes segment-local semantics and gives
    # ** its conventional zero-or-more-directory meaning.
    version: str = "2"

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        paths = ctx.boundary_cfg.get("paths", [])
        if not paths:
            return ResolvedBoundary(
                status="error",
                errors=["No boundary paths declared for explicit boundary provider"],
            )
        selected, errors = _resolve_declared_files(ctx, paths)
        if errors:
            return ResolvedBoundary(status="error", errors=errors)
        ordered: List[tuple] = []
        for repo_rel, child_rel in selected:
            content = ctx.read_file(repo_rel)
            if b"\r\n" in content and b"\x00" not in content:
                content = content.replace(b"\r\n", b"\n")
            ordered.append((f"file:{child_rel}", content))
        if not ordered:
            return ResolvedBoundary(
                status="error",
                errors=["Declared boundary paths produced no digest"],
            )
        # Sort all entries by label for deterministic digest regardless of
        # pattern declaration order in config.
        ordered.sort(
            key=lambda entry: entry[0].encode(
                "utf-8", errors="surrogateescape"
            )
        )
        return ResolvedBoundary(entries=ordered)

    def validate_config(
        self,
        boundary_cfg: dict,
        component_path: str,
        repo_root: Path,
    ) -> List[str]:
        errors: List[str] = []
        for rel in boundary_cfg.get("paths", []):
            try:
                normalized = _normalize_declared_path(rel)
            except ValueError as exc:
                errors.append(f"Invalid boundary path {rel!r}: {exc}")
                continue
            if _is_glob(normalized):
                # Glob existence is checked against the selected source during
                # provider resolution, not against the live filesystem here.
                continue
            full = repo_root / component_path / normalized
            if not full.exists():
                errors.append(
                    f"Boundary path not found: {component_path}/{normalized}"
                    " — ensure the file exists before running generate"
                )
        return errors

    def explain_diff(
        self,
        old_metadata: Optional[dict],
        new_metadata: Optional[dict],
        ctx: ProviderContext,
    ) -> str:
        return "declared boundary artifact changed"


# ---------------------------------------------------------------------------
# Built-in provider: ImplicitProvider
# ---------------------------------------------------------------------------

class ImplicitProvider:
    """Implicit boundary — no explicit boundary paths declared.

    The exact fingerprint covers the whole component tree, but no separate
    boundary digest is produced (status="partial").
    """

    name = "implicit"
    version = "2"

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        paths = ctx.boundary_cfg.get("paths", [])
        if paths:
            # If paths are declared, delegate to PathHashProvider behavior.
            return PathHashProvider().resolve(ctx)
        return ResolvedBoundary(
            entries=[],
            status="partial",
            errors=["No boundary paths declared for implicit boundary"],
        )

    def validate_config(
        self,
        boundary_cfg: dict,
        component_path: str,
        repo_root: Path,
    ) -> List[str]:
        return PathHashProvider().validate_config(boundary_cfg, component_path, repo_root)

    def explain_diff(
        self,
        old_metadata: Optional[dict],
        new_metadata: Optional[dict],
        ctx: ProviderContext,
    ) -> str:
        return "implementation changed (implicit boundary)"


# ---------------------------------------------------------------------------
# Built-in provider: LeafProvider
# ---------------------------------------------------------------------------

class LeafProvider:
    """Leaf component — consumes but does not publish a boundary.

    Produces no boundary digest; status is always "ok" (intentional).
    """

    name = "leaf"
    version = "1"

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        return ResolvedBoundary(entries=[], status="ok")

    def validate_config(
        self,
        boundary_cfg: dict,
        component_path: str,
        repo_root: Path,
    ) -> List[str]:
        return []

    def explain_diff(
        self,
        old_metadata: Optional[dict],
        new_metadata: Optional[dict],
        ctx: ProviderContext,
    ) -> str:
        return "leaf component changed"


# ---------------------------------------------------------------------------
# Named subclasses for each labelled provider
# (identical behavior to PathHashProvider; name differs for config validation)
# ---------------------------------------------------------------------------

class OpenApiProvider(PathHashProvider):
    name = "openapi"


class JsonFileProvider(PathHashProvider):
    name = "json-file"


class PythonExportsProvider(PathHashProvider):
    name = "python-exports"


class TypeScriptExportsProvider(PathHashProvider):
    name = "typescript-exports"


# ---------------------------------------------------------------------------
# Phase 3 — Semantic built-ins
# ---------------------------------------------------------------------------

# Keys stripped at every level of an OpenAPI document (non-contract fields).
_OPENAPI_STRIP_KEYS = frozenset(
    {"description", "summary", "externalDocs", "example", "examples"}
)
# Top-level OpenAPI keys that are metadata only, not API contract.
_OPENAPI_DROP_TOP = frozenset({"info", "servers", "tags"})

# Maps whose direct keys are user-defined identifiers rather than OpenAPI
# annotation fields.  Preserve those keys, then resume normal annotation
# stripping inside each referenced object.
_OPENAPI_COMPONENT_MAPS = frozenset({
    "schemas", "responses", "parameters", "examples", "requestBodies",
    "headers", "securitySchemes", "links", "callbacks", "pathItems",
})
_OPENAPI_NAMED_MAP_KEYS = frozenset({
    "paths", "webhooks", "scopes", "patternProperties", "dependentSchemas",
    "dependentRequired", "parameters", "headers", "encoding", "mapping",
    "callbacks", "links", "variables",
})


def _is_openapi_named_schema_map(path: tuple, key: Any) -> bool:
    """Return whether ``key`` introduces a map of user-defined schema names.

    Annotation-looking names are legal property/schema identifiers.  For
    example, ``properties.description`` describes a property named
    ``description``; it is not the Schema Object's documentation field.
    """
    if key in {"properties", "definitions", "$defs"} | _OPENAPI_NAMED_MAP_KEYS:
        return True
    return path == ("components",) and key in _OPENAPI_COMPONENT_MAPS


def _strip_openapi(
    obj: Any,
    *,
    path: tuple = (),
    preserve_keys: bool = False,
) -> Any:
    """Recursively remove non-contract fields from an OpenAPI object.

    Drops:
    - ``description``, ``summary``, ``externalDocs``, ``example``, ``examples``
      at any nesting level (documentation-only fields).

    OpenAPI explicitly permits ``x-*`` specification extensions.  They can
    affect routing, code generation, authentication, and deployment behavior,
    so they remain part of the canonical contract by default.

    Keys inside Schema Object ``properties``/``definitions``/``$defs`` maps
    and ``components.schemas`` are user-defined identifiers.  Those map keys
    are retained even when their spelling looks like an annotation; annotation
    fields within the schema value are still removed.
    """
    if isinstance(obj, dict):
        stripped = {}
        for key, value in obj.items():
            is_annotation = key in _OPENAPI_STRIP_KEYS
            if is_annotation and not preserve_keys:
                continue

            # A named-map entry's value is a schema object.  Its arbitrary key
            # must not make that schema object itself behave like a named map.
            child_preserves_keys = (
                not preserve_keys and _is_openapi_named_schema_map(path, key)
            )
            stripped[key] = _strip_openapi(
                value,
                path=path + (key,),
                preserve_keys=child_preserves_keys,
            )
        return stripped
    if isinstance(obj, list):
        return [
            _strip_openapi(
                item,
                path=path,
                # Security Requirement Object keys are arbitrary scheme names.
                preserve_keys=(path and path[-1] == "security"),
            )
            for item in obj
        ]
    return obj


def _reject_json_constant(value: str) -> Any:
    raise ProviderError(f"non-finite JSON number {value!r} is not supported")


def _unique_json_object(pairs: List[tuple]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ProviderError(f"duplicate object key {key!r}")
        result[key] = value
    return result


def _parse_json_strict(text: str, path_label: str) -> Any:
    """Parse standards-compliant JSON without lossy duplicate-key handling."""
    try:
        return _json_mod.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (_json_mod.JSONDecodeError, ProviderError, RecursionError) as exc:
        raise ProviderError(
            f"JSON parse failed for {path_label}: {_bounded_exception(exc)}"
        ) from exc


def _parse_yaml_strict(text: str, path_label: str) -> Any:
    """Parse a conservative YAML 1.2-compatible subset for OpenAPI."""
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ProviderError(
            f"Cannot parse {path_label}: file is not valid JSON and PyYAML is not installed. "
            "Install PyYAML: pip install PyYAML"
        ) from exc

    class StrictOpenApiLoader(yaml.SafeLoader):
        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):
                raise ProviderError(
                    "YAML aliases are not supported in canonical OpenAPI inputs"
                )
            return super().compose_node(parent, index)

    # PyYAML's default resolver follows YAML 1.1 and treats yes/no/on/off as
    # booleans.  OpenAPI uses YAML 1.2 semantics, where only true/false are
    # implicit booleans.  Copy before editing so the process-global SafeLoader
    # remains untouched.  Timestamps are not in the YAML 1.2 core schema and
    # remain strings rather than becoming non-JSON datetime/date objects.
    StrictOpenApiLoader.yaml_implicit_resolvers = {
        key: list(resolvers)
        for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    for key, resolvers in StrictOpenApiLoader.yaml_implicit_resolvers.items():
        StrictOpenApiLoader.yaml_implicit_resolvers[key] = [
            resolver
            for resolver in resolvers
            if resolver[0]
            not in {
                "tag:yaml.org,2002:bool",
                "tag:yaml.org,2002:float",
                "tag:yaml.org,2002:int",
                "tag:yaml.org,2002:timestamp",
            }
        ]
    StrictOpenApiLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|false)$", re.IGNORECASE),
        list("tTfF"),
    )
    # Accept the JSON numeric grammar, which is a conservative unambiguous
    # subset of YAML 1.2 core numbers. In particular, YAML 1.1's ``012`` octal
    # and sexagesimal forms remain strings instead of colliding with decimals.
    StrictOpenApiLoader.add_implicit_resolver(
        "tag:yaml.org,2002:int",
        re.compile(r"^-?(?:0|[1-9][0-9]*)$"),
        list("-0123456789"),
    )
    StrictOpenApiLoader.add_implicit_resolver(
        "tag:yaml.org,2002:float",
        re.compile(
            r"^-?(?:(?:0|[1-9][0-9]*)\.[0-9]+(?:[eE][-+]?[0-9]+)?"
            r"|(?:0|[1-9][0-9]*)[eE][-+]?[0-9]+)$"
        ),
        list("-0123456789"),
    )
    # YAML 1.2 still recognizes non-finite spellings. Resolve them as floats
    # so the JSON-tree guard rejects them, instead of accidentally preserving
    # them as benign strings after removing PyYAML's broader YAML 1.1 rules.
    StrictOpenApiLoader.add_implicit_resolver(
        "tag:yaml.org,2002:float",
        re.compile(r"^(?:[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"),
        list("-+."),
    )

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict:
        if not isinstance(node, yaml.MappingNode):
            raise ProviderError("expected a YAML mapping")
        mapping: dict = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if type(key) is not str:
                raise ProviderError(
                    "YAML/OpenAPI mapping keys must be strings; quote numeric "
                    "response keys (for example, '200')"
                )
            if key in mapping:
                raise ProviderError(f"duplicate mapping key {key!r}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    StrictOpenApiLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping,
    )
    try:
        return yaml.load(text, Loader=StrictOpenApiLoader)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(
            f"YAML parse failed for {path_label}: {_bounded_exception(exc)}"
        ) from exc


def _parse_yaml_or_json(raw: bytes, path_label: str) -> Any:
    """Parse raw OpenAPI bytes without accepting ambiguous structures."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProviderError(
            f"OpenAPI file {path_label} is not valid UTF-8: {_bounded_exception(exc)}"
        ) from exc

    # A .json declaration must actually be JSON; silently reinterpreting a
    # malformed JSON contract as YAML would hide mistakes.  YAML files still
    # take the JSON fast path because JSON is a YAML subset.
    if path_label.lower().endswith(".json"):
        return _parse_json_strict(text, path_label)
    try:
        return _json_mod.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except _json_mod.JSONDecodeError:
        return _parse_yaml_strict(text, path_label)
    except ProviderError as exc:
        raise ProviderError(
            f"JSON parse failed for {path_label}: {_bounded_exception(exc)}"
        ) from exc


def _json_tree_error(value: Any, *, path: str = "$") -> Optional[str]:
    """Reject values that cannot be represented safely and portably as JSON."""
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return f"{path} contains a string that is not valid Unicode/UTF-8"
        return None
    if value is None or type(value) in {bool, int}:
        return None
    if type(value) is float:
        if not math.isfinite(value):
            return f"{path} contains a non-finite number"
        return None
    if type(value) is list:
        for index, item in enumerate(value):
            error = _json_tree_error(item, path=f"{path}[{index}]")
            if error:
                return error
        return None
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                return (
                    f"{path} has a non-string mapping key; quote numeric response "
                    "keys (for example, '200')"
                )
            try:
                key.encode("utf-8")
            except UnicodeEncodeError:
                return f"{path} contains an object key that is not valid Unicode/UTF-8"
            error = _json_tree_error(item, path=f"{path}.{key}")
            if error:
                return error
        return None
    return f"{path} contains unsafe scalar type {type(value).__name__}"


def _openapi_document_error(document: Any) -> Optional[str]:
    """Validate the minimum root contract and reference safety policy."""
    tree_error = _json_tree_error(document)
    if tree_error:
        return tree_error
    if type(document) is not dict:
        return "OpenAPI document root must be an object"

    has_openapi_version = "openapi" in document
    has_swagger_version = "swagger" in document
    openapi_version = document.get("openapi")
    swagger_version = document.get("swagger")
    if has_openapi_version and has_swagger_version:
        return "OpenAPI document must not declare both 'openapi' and 'swagger'"
    if has_openapi_version:
        if type(openapi_version) is not str or not re.fullmatch(
            r"3\.(?:0|1)\.\d+", openapi_version
        ):
            return (
                "unsupported or invalid 'openapi' version; expected an OpenAPI "
                "3.0.x or 3.1.x string"
            )
    elif not has_swagger_version or swagger_version != "2.0":
        return (
            "OpenAPI document must declare 'openapi' 3.0.x/3.1.x or "
            "'swagger' 2.0"
        )

    def validate_refs(value: Any, path: str = "$") -> Optional[str]:
        if type(value) is list:
            for index, item in enumerate(value):
                error = validate_refs(item, f"{path}[{index}]")
                if error:
                    return error
            return None
        if type(value) is not dict:
            return None
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key == "$ref":
                if type(item) is not str or not item.startswith("#"):
                    return (
                        f"{child_path} uses an external or local-file reference; "
                        "openapi-canonical accepts only same-document fragment "
                        "references beginning with '#'"
                    )
            error = validate_refs(item, child_path)
            if error:
                return error
        return None

    return validate_refs(document)


def _canonical_json_bytes(value: Any, path_label: str) -> bytes:
    """Serialize validated data deterministically, converting failures to errors."""
    try:
        encoded = _json_mod.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ProviderError(
            f"Canonical JSON serialization failed for {path_label}: "
            f"{_bounded_exception(exc)}"
        ) from exc
    if len(encoded) > MAX_PROVIDER_ENTRY_BYTES:
        raise ProviderError(
            f"Canonical JSON for {path_label} exceeds the "
            f"{MAX_PROVIDER_ENTRY_BYTES}-byte provider entry limit"
        )
    return encoded


class JsonCanonicalProvider:
    """Parse each boundary file as JSON and re-emit deterministic compact JSON.

    This makes the boundary digest stable across whitespace and key-ordering
    changes while still detecting content changes.  This is boundver's own
    canonical form, not an implementation of RFC 8785.  The entry label is
    ``canonical:<filename>`` — distinct from ``file:<filename>`` used by raw
    providers, so there is zero digest overlap with pre-Phase-3 lockfiles.

    Use this provider when your boundary files are JSON configuration or
    schema files where formatting should not affect the digest.
    """

    name = "json-canonical"
    # v2 rejects duplicate keys and non-finite numbers and uses an explicitly
    # bounded, fail-closed canonical serialization path.
    version = "2"

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        paths = ctx.boundary_cfg.get("paths", [])
        if not paths:
            return ResolvedBoundary(
                status="error",
                errors=["No boundary paths declared for json-canonical provider"],
            )
        selected, errors = _resolve_declared_files(ctx, paths)
        if errors:
            return ResolvedBoundary(status="error", errors=errors)
        entries: List[tuple] = []
        for repo_rel, child_rel in selected:
            try:
                raw = ctx.read_file(repo_rel)
                if type(raw) not in {bytes, _ModeAwareBytes}:
                    raise ProviderError("source accessor returned non-bytes content")
                text = raw.decode("utf-8")
                obj = _parse_json_strict(text, child_rel)
                tree_error = _json_tree_error(obj)
                if tree_error:
                    raise ProviderError(tree_error)
                canonical = _canonical_json_bytes(obj, child_rel)
            except (ProviderError, UnicodeDecodeError, OSError) as exc:
                return ResolvedBoundary(
                    status="error",
                    errors=[
                        f"JSON canonicalization failed for {child_rel}: "
                        f"{_bounded_exception(exc)}"
                    ],
                )
            entries.append((f"canonical:{child_rel}", canonical))
        if not entries:
            return ResolvedBoundary(
                status="error",
                errors=["Declared boundary paths produced no digest"],
            )
        return ResolvedBoundary(entries=entries)

    def validate_config(
        self,
        boundary_cfg: dict,
        component_path: str,
        repo_root: Path,
    ) -> List[str]:
        return PathHashProvider().validate_config(
            boundary_cfg, component_path, repo_root
        )

    def explain_diff(
        self,
        old_metadata: Optional[dict],
        new_metadata: Optional[dict],
        ctx: ProviderContext,
    ) -> str:
        return "JSON contract changed"


class OpenApiCanonicalProvider:
    """Parse boundary files as OpenAPI (YAML or JSON) and hash a stripped
    canonical form that contains only the API contract surface.

    **What is stripped (non-contract):**
    - Top-level ``info``, ``servers``, ``tags`` blocks (metadata / deployment details).
    - ``description``, ``summary``, ``externalDocs``, ``example``, ``examples``
      at any nesting depth.

    **What is kept (contract):**
    - ``paths``, ``components``, ``security``, ``webhooks`` and all their
      structural content.
    - ``x-*`` specification extensions, because tooling may give them
      contract-bearing semantics.

    This means adding a comment to an endpoint description does not change the
    boundary digest, but adding, removing, or altering an endpoint, parameter,
    schema, or response type does.

    Requires PyYAML for ``.yaml`` / ``.yml`` files.  ``.json`` files are handled
    by the standard library.
    """

    name = "openapi-canonical"
    # v3 validates the OpenAPI root, preserves x-* extensions, rejects
    # ambiguous YAML/JSON and unsafe references, and uses strict serialization.
    version = "3"

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        paths = ctx.boundary_cfg.get("paths", [])
        if not paths:
            return ResolvedBoundary(
                status="error",
                errors=["No boundary paths declared for openapi-canonical provider"],
            )
        selected, errors = _resolve_declared_files(ctx, paths)
        if errors:
            return ResolvedBoundary(status="error", errors=errors)
        entries: List[tuple] = []
        for repo_rel, child_rel in selected:
            try:
                raw = ctx.read_file(repo_rel)
                if type(raw) not in {bytes, _ModeAwareBytes}:
                    raise ProviderError("source accessor returned non-bytes content")
                obj = _parse_yaml_or_json(raw, child_rel)
                document_error = _openapi_document_error(obj)
                if document_error:
                    raise ProviderError(document_error)
                # Drop top-level metadata blocks, then recursively strip docs.
                obj = {k: v for k, v in obj.items() if k not in _OPENAPI_DROP_TOP}
                contract = _strip_openapi(obj)
                canonical = _canonical_json_bytes(contract, child_rel)
            except (ProviderError, OSError, RecursionError) as exc:
                return ResolvedBoundary(
                    status="error",
                    errors=[
                        f"OpenAPI canonicalization failed for {child_rel}: "
                        f"{_bounded_exception(exc)}"
                    ],
                )
            entries.append((f"canonical:{child_rel}", canonical))
        if not entries:
            return ResolvedBoundary(
                status="error",
                errors=["Declared boundary paths produced no digest"],
            )
        return ResolvedBoundary(entries=entries)

    def validate_config(
        self,
        boundary_cfg: dict,
        component_path: str,
        repo_root: Path,
    ) -> List[str]:
        return PathHashProvider().validate_config(
            boundary_cfg, component_path, repo_root
        )

    def explain_diff(
        self,
        old_metadata: Optional[dict],
        new_metadata: Optional[dict],
        ctx: ProviderContext,
    ) -> str:
        return "OpenAPI contract changed"


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_BUILTIN_PROVIDER_TYPES = (
    OpenApiProvider,
    JsonFileProvider,
    PythonExportsProvider,
    TypeScriptExportsProvider,
    ImplicitProvider,
    LeafProvider,
    JsonCanonicalProvider,
    OpenApiCanonicalProvider,
)

# Raw-hash aliases make the behavior explicit while preserving short names.
_ALIASES = {
    "openapi-raw": "openapi",
    "json-file-raw": "json-file",
    "python-exports-raw": "python-exports",
    "typescript-exports-raw": "typescript-exports",
}

_REGISTRY: ProviderRegistry = {}


def register_provider(
    p: BoundaryProvider,
    registry: Optional[ProviderRegistry] = None,
) -> None:
    """Register a provider instance. Overwrites any existing entry with the same name.

    If *registry* is provided, registers into that dict instead of the global registry.
    """
    contract_error = _provider_identity_error(p)
    if contract_error:
        raise ProviderError(f"Cannot register boundary provider: {contract_error}")
    provider_name = _safe_provider_attribute(p, "name")
    if type(provider_name) is not str:
        raise ProviderError(
            "Cannot register boundary provider: provider name changed during validation"
        )
    target = registry if registry is not None else _REGISTRY
    target[provider_name] = p


def get_provider(
    name: str,
    registry: Optional[ProviderRegistry] = None,
) -> Optional[BoundaryProvider]:
    """Return the registered provider for *name*, or None.

    If *registry* is provided, looks up from that dict instead of the global registry.
    """
    target = registry if registry is not None else _REGISTRY
    return target.get(name)


def create_registry() -> ProviderRegistry:
    """Create a fresh registry populated with builtins. Useful for isolated testing or
    running multiple configs with different custom providers in the same process."""
    reg: ProviderRegistry = {}
    for cls in _BUILTIN_PROVIDER_TYPES:
        provider = cls()
        register_provider(provider, registry=reg)
    for alias, target in _ALIASES.items():
        if target in reg:
            reg[alias] = reg[target]
    return reg


def _register_builtins() -> None:
    _REGISTRY.update(create_registry())


_register_builtins()


# ---------------------------------------------------------------------------
# Custom provider loading (Phase 2)
# ---------------------------------------------------------------------------

def load_custom_providers(
    providers_list: list,
    allow_custom: bool,
    registry: Optional[ProviderRegistry] = None,
) -> List[str]:
    """Load and register custom providers declared in the config ``providers`` key.

    Returns a list of error strings.  Raises nothing — callers decide whether
    errors are fatal.

    ``providers_list`` is the value of ``config.get("providers", [])``.
    ``allow_custom`` must be True (set by ``--allow-custom-providers``) before
    any custom provider is loaded; otherwise an error is returned immediately.
    If *registry* is provided, registers into that dict instead of the global registry.
    """
    if not providers_list:
        return []
    if not allow_custom:
        return [
            "Config declares custom providers but loading is not enabled. "
            "Pass --allow-custom-providers (or the equivalent trusted API argument)."
        ]
    errors: List[str] = []
    loaded_names: set[str] = set()
    # Validate module names before importing anything to prevent injection via
    # crafted config files. Only dotted Python identifiers are allowed.
    _VALID_MODULE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$")
    _VALID_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    for entry in providers_list:
        if not isinstance(entry, dict):
            errors.append(
                f"Provider entry must be an object, got {type(entry).__name__}"
            )
            continue
        module_name = entry.get("module", "")
        class_name = entry.get("class", "")
        if (
            not isinstance(module_name, str)
            or not module_name
            or not isinstance(class_name, str)
            or not class_name
        ):
            errors.append(
                f"Provider entry missing required fields 'module'/'class': {entry!r}"
            )
            continue
        if not _VALID_MODULE_RE.match(module_name):
            errors.append(
                f"Provider module name '{module_name}' is not a valid Python module path "
                "(must be dotted identifiers like 'my_pkg.providers')"
            )
            continue
        if not _VALID_IDENT_RE.match(class_name):
            errors.append(
                f"Provider class name '{class_name}' is not a valid Python identifier"
            )
            continue
        try:
            mod = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(
                f"Failed to import provider module '{module_name}': "
                f"{_bounded_exception(exc)}"
            )
            continue
        try:
            cls = getattr(mod, class_name, None)
        except Exception as exc:
            errors.append(
                f"Failed to read '{module_name}.{class_name}': "
                f"{_bounded_exception(exc)}"
            )
            continue
        if cls is None:
            errors.append(
                f"Module '{module_name}' has no attribute '{class_name}'"
            )
            continue
        try:
            instance = cls()
        except Exception as exc:
            errors.append(
                f"Failed to instantiate '{module_name}.{class_name}': "
                f"{_bounded_exception(exc)}"
            )
            continue
        try:
            provider_name = _safe_provider_attribute(instance, "name")
        except ProviderError as exc:
            errors.append(
                f"Provider '{module_name}.{class_name}' is invalid: {exc}"
            )
            continue
        if type(provider_name) is not str:
            errors.append(
                f"Provider '{module_name}.{class_name}' changed its name during validation"
            )
            continue
        if not provider_name.startswith("custom.") or provider_name == "custom.":
            errors.append(
                f"Provider '{module_name}.{class_name}' has name={provider_name!r}; "
                "custom provider names must start with 'custom.' to avoid collisions "
                "with built-in providers (e.g. name='custom.my_format')"
            )
            continue
        configured_name = entry.get("name")
        if configured_name is not None:
            if type(configured_name) is not str or configured_name != provider_name:
                errors.append(
                    f"Provider '{module_name}.{class_name}' declares runtime "
                    f"name={provider_name!r}, which does not match configured "
                    f"name={configured_name!r}"
                )
                continue
        target_registry = registry if registry is not None else _REGISTRY
        if provider_name in loaded_names:
            errors.append(
                f"Duplicate custom provider name {provider_name!r} in providers config"
            )
            continue
        if provider_name in target_registry:
            errors.append(
                f"Custom provider name {provider_name!r} is already registered; "
                "refusing to replace it while loading config"
            )
            continue
        contract_error = _provider_identity_error(instance)
        if contract_error:
            errors.append(
                f"Provider '{module_name}.{class_name}' is invalid: {contract_error}"
            )
            continue
        try:
            register_provider(instance, registry=registry)
            loaded_names.add(provider_name)
        except ProviderError as exc:
            errors.append(
                f"Provider '{module_name}.{class_name}' could not be registered: {exc}"
            )
    return errors
