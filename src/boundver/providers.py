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

import fnmatch
import importlib
import json as _json_mod
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from ._hashing import HASH_DOMAIN_BOUNDARY, _hash_framed_entries
from ._utils import ProviderError, _is_glob


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


def _fnmatch_case_sensitive(name: str, pattern: str) -> bool:
    """Case-sensitive fnmatch regardless of platform.

    Standard fnmatch is case-insensitive on Windows, which would cause
    different boundary digests for the same repo on different OSes.
    """
    regex = fnmatch.translate(pattern)
    # fnmatch.translate may produce a case-insensitive pattern on Windows;
    # compile explicitly with no IGNORECASE flag.
    return re.match(regex, name) is not None


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
    resolved = provider.resolve(ctx)
    result: tuple
    if resolved.status == "error" or not resolved.entries:
        result = (None, resolved.status or "error", resolved.errors)
    else:
        result = (
            _hash_framed_entries(resolved.entries, domain=HASH_DOMAIN_BOUNDARY),
            resolved.status,
            resolved.errors,
        )
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
    validator = getattr(provider, "validate_config", None)
    if not callable(validator):
        return []
    provider_name = getattr(provider, "name", provider.__class__.__name__)
    try:
        errors = validator(boundary_cfg, component_path, repo_root)
    except Exception as exc:
        return [f"Provider '{provider_name}' config validation failed: {exc}"]
    if errors is None:
        return []
    if not isinstance(errors, list):
        return [
            f"Provider '{provider_name}' validate_config() must return a list of errors"
        ]
    return [str(error) for error in errors]


def explain_provider_diff(
    provider: BoundaryProvider,
    old_metadata: Optional[dict],
    new_metadata: Optional[dict],
    ctx: ProviderContext,
) -> str:
    """Return a provider-specific diff summary with a useful fallback."""
    provider_name = getattr(provider, "name", provider.__class__.__name__)
    fallback = f"{provider_name} boundary changed"
    explainer = getattr(provider, "explain_diff", None)
    if not callable(explainer):
        return fallback
    try:
        explanation = explainer(old_metadata, new_metadata, ctx)
    except Exception as exc:
        return f"{fallback} (provider explanation unavailable: {exc})"
    if isinstance(explanation, str) and explanation.strip():
        return explanation.strip()
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

    name: str = ""  # overridden by subclasses / instances
    version: str = "1"  # Bump when the hashing algorithm changes

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        paths = ctx.boundary_cfg.get("paths", [])
        if not paths:
            return ResolvedBoundary(
                status="error",
                errors=["No boundary paths declared for explicit boundary provider"],
            )
        seen: set = set()
        ordered: List[tuple] = []  # List[tuple[str, bytes]]
        unmatched: List[str] = []

        # Lazily enumerate all component files once when any glob pattern is present.
        _all_component_files: Optional[List[str]] = None

        def _component_files() -> List[str]:
            nonlocal _all_component_files
            if _all_component_files is None:
                _all_component_files = sorted(ctx.list_files(ctx.component_path))
            return _all_component_files

        for rel in sorted(paths):
            rel = rel.strip()
            matched = False
            if _is_glob(rel):
                # Expand glob against all files in the component directory.
                # fnmatch treats * as matching any characters incl. path separators,
                # so 'api/*.yaml' matches files at any depth under api/.
                for repo_rel in _component_files():
                    child_rel = _component_relative_path(ctx.component_path, repo_rel)
                    if _fnmatch_case_sensitive(child_rel, rel):
                        matched = True
                    if _fnmatch_case_sensitive(child_rel, rel) and repo_rel not in seen:
                        seen.add(repo_rel)
                        content = ctx.read_file(repo_rel)
                        if b"\r\n" in content and b"\x00" not in content:
                            content = content.replace(b"\r\n", b"\n")
                        ordered.append((f"file:{child_rel}", content))
            else:
                full_base = _join_repo_path(ctx.component_path, rel)
                for repo_rel in sorted(ctx.list_files(full_base)):
                    matched = True
                    if repo_rel in seen:
                        continue
                    seen.add(repo_rel)
                    content = ctx.read_file(repo_rel)
                    # CRLF normalisation — preserved from _read_path_content
                    if b"\r\n" in content and b"\x00" not in content:
                        content = content.replace(b"\r\n", b"\n")
                    child_rel = _component_relative_path(ctx.component_path, repo_rel)
                    # Keep the stable, provider-independent path label.
                    ordered.append((f"file:{child_rel}", content))

            if not matched:
                unmatched.append(rel)

        if unmatched:
            return ResolvedBoundary(
                status="error",
                errors=[
                    f"Declared boundary path matched no tracked files: {rel}"
                    for rel in unmatched
                ],
            )
        if not ordered:
            return ResolvedBoundary(
                status="error",
                errors=["Declared boundary paths produced no digest"],
            )
        # Sort all entries by label for deterministic digest regardless of
        # pattern declaration order in config.
        ordered.sort(key=lambda entry: entry[0])
        return ResolvedBoundary(entries=ordered)

    def validate_config(
        self,
        boundary_cfg: dict,
        component_path: str,
        repo_root: Path,
    ) -> List[str]:
        errors: List[str] = []
        for rel in boundary_cfg.get("paths", []):
            if _is_glob(rel):
                # Glob patterns can't be existence-checked; validated at runtime.
                # Reject patterns with .. to prevent traversal.
                if ".." in rel:
                    errors.append(
                        f"Boundary glob pattern must not contain '..': {rel}"
                    )
                continue
            full = repo_root / component_path / rel
            if not full.exists():
                errors.append(
                    f"Boundary path not found: {component_path}/{rel}"
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
    version = "1"

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
    - All ``x-*`` extension keys (vendor-specific, non-standard).
    - ``description``, ``summary``, ``externalDocs``, ``example``, ``examples``
      at any nesting level (documentation-only fields).

    Keys inside Schema Object ``properties``/``definitions``/``$defs`` maps
    and ``components.schemas`` are user-defined identifiers.  Those map keys
    are retained even when their spelling looks like an annotation; annotation
    fields within the schema value are still removed.
    """
    if isinstance(obj, dict):
        stripped = {}
        for key, value in obj.items():
            is_annotation = key in _OPENAPI_STRIP_KEYS or (
                isinstance(key, str) and key.startswith("x-")
            )
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


def _parse_yaml_or_json(raw: bytes, path_label: str) -> Any:
    """Parse raw bytes as YAML (preferred) or JSON, with a clear error on failure."""
    # Attempt JSON first (subset of YAML, faster path for .json files)
    text = raw.decode("utf-8")
    try:
        return _json_mod.loads(text)
    except _json_mod.JSONDecodeError:
        pass
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        raise ProviderError(
            f"Cannot parse {path_label}: file is not valid JSON and PyYAML is not installed. "
            "Install PyYAML: pip install PyYAML"
        )
    except Exception as exc:
        raise ProviderError(f"YAML/JSON parse failed for {path_label}: {exc}") from exc


class JsonCanonicalProvider:
    """Parse each boundary file as JSON and re-emit as RFC 8785-style canonical JSON.

    This makes the boundary digest stable across whitespace and key-ordering
    changes while still detecting content changes.  The entry label is
    ``canonical:<filename>`` — distinct from ``file:<filename>`` used by raw
    providers, so there is zero digest overlap with pre-Phase-3 lockfiles.

    Use this provider when your boundary files are JSON configuration or
    schema files where formatting should not affect the digest.
    """

    name = "json-canonical"
    version = "1"

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        paths = ctx.boundary_cfg.get("paths", [])
        if not paths:
            return ResolvedBoundary(
                status="error",
                errors=["No boundary paths declared for json-canonical provider"],
            )
        entries: List[tuple] = []
        unmatched: List[str] = []
        for rel in sorted(paths):
            rel = rel.strip()
            if _is_glob(rel):
                return ResolvedBoundary(
                    status="error",
                    errors=[f"Glob patterns not supported by json-canonical provider: {rel}"],
                )
            full_base = _join_repo_path(ctx.component_path, rel)
            matched_files = sorted(ctx.list_files(full_base))
            if not matched_files:
                unmatched.append(rel)
            for repo_rel in matched_files:
                raw = ctx.read_file(repo_rel)
                child_rel = _component_relative_path(ctx.component_path, repo_rel)
                try:
                    obj = _json_mod.loads(raw.decode("utf-8"))
                except (_json_mod.JSONDecodeError, UnicodeDecodeError) as exc:
                    return ResolvedBoundary(
                        status="error",
                        errors=[f"JSON parse failed for {child_rel}: {exc}"],
                    )
                canonical = _json_mod.dumps(
                    obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                entries.append((f"canonical:{child_rel}", canonical.encode("utf-8")))
        if unmatched:
            return ResolvedBoundary(
                status="error",
                errors=[
                    f"Declared boundary path matched no tracked files: {rel}"
                    for rel in unmatched
                ],
            )
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
        errors: List[str] = []
        for rel in boundary_cfg.get("paths", []):
            full = repo_root / component_path / rel
            if not full.exists():
                errors.append(
                    f"Boundary path not found: {component_path}/{rel}"
                    " — ensure the file exists before running generate"
                )
        return errors

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
    - All ``x-*`` extension keys.

    **What is kept (contract):**
    - ``paths``, ``components``, ``security``, ``webhooks`` and all their
      structural content.

    This means adding a comment to an endpoint description does not change the
    boundary digest, but adding, removing, or altering an endpoint, parameter,
    schema, or response type does.

    Requires PyYAML for ``.yaml`` / ``.yml`` files.  ``.json`` files are handled
    by the standard library.
    """

    name = "openapi-canonical"
    # v2 preserves annotation-looking user-defined schema/property names.
    version = "2"

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        paths = ctx.boundary_cfg.get("paths", [])
        if not paths:
            return ResolvedBoundary(
                status="error",
                errors=["No boundary paths declared for openapi-canonical provider"],
            )
        entries: List[tuple] = []
        unmatched: List[str] = []
        for rel in sorted(paths):
            rel = rel.strip()
            if _is_glob(rel):
                return ResolvedBoundary(
                    status="error",
                    errors=[f"Glob patterns not supported by openapi-canonical provider: {rel}"],
                )
            full_base = _join_repo_path(ctx.component_path, rel)
            matched_files = sorted(ctx.list_files(full_base))
            if not matched_files:
                unmatched.append(rel)
            for repo_rel in matched_files:
                raw = ctx.read_file(repo_rel)
                child_rel = _component_relative_path(ctx.component_path, repo_rel)
                try:
                    obj = _parse_yaml_or_json(raw, child_rel)
                except ValueError as exc:
                    return ResolvedBoundary(status="error", errors=[str(exc)])
                # Drop top-level metadata blocks, then recursively strip docs.
                if isinstance(obj, dict):
                    obj = {k: v for k, v in obj.items() if k not in _OPENAPI_DROP_TOP}
                contract = _strip_openapi(obj)
                canonical = _json_mod.dumps(
                    contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                entries.append((f"canonical:{child_rel}", canonical.encode("utf-8")))
        if unmatched:
            return ResolvedBoundary(
                status="error",
                errors=[
                    f"Declared boundary path matched no tracked files: {rel}"
                    for rel in unmatched
                ],
            )
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
        errors: List[str] = []
        for rel in boundary_cfg.get("paths", []):
            full = repo_root / component_path / rel
            if not full.exists():
                errors.append(
                    f"Boundary path not found: {component_path}/{rel}"
                    " — ensure the file exists before running generate"
                )
        return errors

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
    target = registry if registry is not None else _REGISTRY
    target[p.name] = p


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
        reg[provider.name] = provider
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
        except ImportError as exc:
            errors.append(f"Failed to import provider module '{module_name}': {exc}")
            continue
        cls = getattr(mod, class_name, None)
        if cls is None:
            errors.append(
                f"Module '{module_name}' has no attribute '{class_name}'"
            )
            continue
        try:
            instance = cls()
        except Exception as exc:
            errors.append(f"Failed to instantiate '{module_name}.{class_name}': {exc}")
            continue
        provider_name = getattr(instance, "name", None)
        if not isinstance(provider_name, str) or not provider_name.startswith("custom."):
            errors.append(
                f"Provider '{module_name}.{class_name}' has name={provider_name!r}; "
                "custom provider names must start with 'custom.' to avoid collisions "
                "with built-in providers (e.g. name='custom.my_format')"
            )
            continue
        register_provider(instance, registry=registry)
    return errors
