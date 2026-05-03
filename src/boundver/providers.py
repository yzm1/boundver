"""
Provider protocol for boundver boundary extraction.

Phase 1: protocol types, built-in wrappers that replicate current raw-byte
behavior exactly (zero digest drift), registry, and compute_boundary().

Phase 2: load_custom_providers() — loads and registers custom providers from
the `providers` config key. Requires --allow-custom-providers at runtime.

Phase 3: semantic built-ins — openapi-canonical, json-canonical.
  These produce digests that are stable across formatting/comment changes but
  change when the logical API contract changes. They are opt-in (activated only
  by explicit provider name) and have zero impact on existing lockfiles.

The design is specified in docs/design/07-provider-architecture.md.
"""
from __future__ import annotations

import fnmatch
import hashlib
import importlib
import json as _json_mod
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable


def _is_glob(pattern: str) -> bool:
    """Return True if the pattern contains glob metacharacters."""
    return any(c in pattern for c in ("*", "?", "["))


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
    Core hashes them as::

        sha256(b"entry:<label>\\n" + content + ...)

    Labels MUST be deterministic and sorted by the provider.
    For path-based providers the label is ``file:<component-relative-path>``,
    preserving backwards-compatibility with the pre-Phase-1 digest format.
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


# ---------------------------------------------------------------------------
# Core hashing function — the ONLY place SHA-256 is called for boundary digests
# ---------------------------------------------------------------------------

def compute_boundary(
    provider: BoundaryProvider,
    ctx: ProviderContext,
) -> tuple:  # tuple[Optional[str], str, List[str]]
    """Ask the provider to resolve content, then hash it.

    Returns ``(digest_or_None, status, errors)``.

    The label format ``"entry:<label>\\n"`` matches the per-entry prefix used in
    the pre-Phase-1 ``boundary_paths_digest`` function (which used
    ``"file:<rel>\\n"``), so labels like ``"file:openapi.yaml"`` produce
    identical digests.
    """
    resolved = provider.resolve(ctx)
    if resolved.status == "error" or not resolved.entries:
        return None, resolved.status or "error", resolved.errors
    # Hash format mirrors pre-Phase-1 boundary_paths_digest:
    #   sha256( ("file:child_rel\n" + content) * N )
    # Labels produced by path-based providers are "file:<child_rel>",
    # so we append "\n" and then the content — identical wire format.
    parts: List[bytes] = []
    for label, content in resolved.entries:
        parts.append(label.encode("utf-8"))
        parts.append(b"\n")
        parts.append(content)
    return hashlib.sha256(b"".join(parts)).hexdigest(), resolved.status, resolved.errors


# ---------------------------------------------------------------------------
# Built-in provider base: PathHashProvider
# ---------------------------------------------------------------------------

class PathHashProvider:
    """Hash declared boundary paths as raw normalised bytes.

    Replicates the pre-Phase-1 ``boundary_paths_digest`` behavior exactly so
    existing lockfile digests are preserved.

    Subclasses set ``name`` to the provider identifier string.
    """

    name: str = ""  # overridden by subclasses / instances

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        paths = ctx.boundary_cfg.get("paths", [])
        if not paths:
            return ResolvedBoundary(
                status="error",
                errors=["No boundary paths declared for explicit boundary provider"],
            )
        seen: set = set()
        ordered: List[tuple] = []  # List[tuple[str, bytes]]

        # Lazily enumerate all component files once when any glob pattern is present.
        _all_component_files: Optional[List[str]] = None

        def _component_files() -> List[str]:
            nonlocal _all_component_files
            if _all_component_files is None:
                _all_component_files = sorted(ctx.list_files(ctx.component_path))
            return _all_component_files

        for rel in sorted(paths):
            rel = rel.strip()
            if _is_glob(rel):
                # Expand glob against all files in the component directory.
                # fnmatch treats * as matching any characters incl. path separators,
                # so 'api/*.yaml' matches files at any depth under api/.
                for repo_rel in _component_files():
                    child_rel = repo_rel[len(ctx.component_path) + 1:]
                    if _fnmatch_case_sensitive(child_rel, rel) and repo_rel not in seen:
                        seen.add(repo_rel)
                        content = ctx.read_file(repo_rel)
                        if b"\r\n" in content and b"\x00" not in content:
                            content = content.replace(b"\r\n", b"\n")
                        ordered.append((f"file:{child_rel}", content))
            else:
                full_base = f"{ctx.component_path}/{rel}".lstrip("/")
                for repo_rel in sorted(ctx.list_files(full_base)):
                    if repo_rel in seen:
                        continue
                    seen.add(repo_rel)
                    content = ctx.read_file(repo_rel)
                    # CRLF normalisation — preserved from _read_path_content
                    if b"\r\n" in content and b"\x00" not in content:
                        content = content.replace(b"\r\n", b"\n")
                    child_rel = repo_rel[len(ctx.component_path) + 1:]
                    # Label uses "file:" prefix to match pre-Phase-1 format.
                    ordered.append((f"file:{child_rel}", content))

        if not ordered:
            return ResolvedBoundary(
                status="error",
                errors=["Declared boundary paths produced no digest"],
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


def _strip_openapi(obj: Any) -> Any:
    """Recursively remove non-contract fields from an OpenAPI object.

    Drops:
    - All ``x-*`` extension keys (vendor-specific, non-standard).
    - ``description``, ``summary``, ``externalDocs``, ``example``, ``examples``
      at any nesting level (documentation-only fields).
    """
    if isinstance(obj, dict):
        return {
            k: _strip_openapi(v)
            for k, v in obj.items()
            if k not in _OPENAPI_STRIP_KEYS and not (isinstance(k, str) and k.startswith("x-"))
        }
    if isinstance(obj, list):
        return [_strip_openapi(item) for item in obj]
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
        raise ValueError(
            f"Cannot parse {path_label}: file is not valid JSON and PyYAML is not installed. "
            "Install PyYAML: pip install PyYAML"
        )
    except Exception as exc:
        raise ValueError(f"YAML/JSON parse failed for {path_label}: {exc}") from exc


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

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        paths = ctx.boundary_cfg.get("paths", [])
        if not paths:
            return ResolvedBoundary(
                status="error",
                errors=["No boundary paths declared for json-canonical provider"],
            )
        entries: List[tuple] = []
        for rel in sorted(paths):
            rel = rel.strip()
            full_base = f"{ctx.component_path}/{rel}".lstrip("/")
            for repo_rel in sorted(ctx.list_files(full_base)):
                raw = ctx.read_file(repo_rel)
                child_rel = repo_rel[len(ctx.component_path) + 1:]
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

    def resolve(self, ctx: ProviderContext) -> ResolvedBoundary:
        paths = ctx.boundary_cfg.get("paths", [])
        if not paths:
            return ResolvedBoundary(
                status="error",
                errors=["No boundary paths declared for openapi-canonical provider"],
            )
        entries: List[tuple] = []
        for rel in sorted(paths):
            rel = rel.strip()
            full_base = f"{ctx.component_path}/{rel}".lstrip("/")
            for repo_rel in sorted(ctx.list_files(full_base)):
                raw = ctx.read_file(repo_rel)
                child_rel = repo_rel[len(ctx.component_path) + 1:]
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

_REGISTRY: Dict[str, BoundaryProvider] = {}


def register_provider(p: BoundaryProvider) -> None:
    """Register a provider instance. Overwrites any existing entry with the same name."""
    _REGISTRY[p.name] = p


def get_provider(name: str) -> Optional[BoundaryProvider]:
    """Return the registered provider for *name*, or None."""
    return _REGISTRY.get(name)


def _register_builtins() -> None:
    for cls in (
        OpenApiProvider,
        JsonFileProvider,
        PythonExportsProvider,
        TypeScriptExportsProvider,
        ImplicitProvider,
        LeafProvider,
        JsonCanonicalProvider,
        OpenApiCanonicalProvider,
    ):
        register_provider(cls())


_register_builtins()


# ---------------------------------------------------------------------------
# Custom provider loading (Phase 2)
# ---------------------------------------------------------------------------

def load_custom_providers(
    providers_list: list,
    allow_custom: bool,
) -> List[str]:
    """Load and register custom providers declared in the config ``providers`` key.

    Returns a list of error strings.  Raises nothing — callers decide whether
    errors are fatal.

    ``providers_list`` is the value of ``config.get("providers", [])``.
    ``allow_custom`` must be True (set by ``--allow-custom-providers``) before
    any custom provider is loaded; otherwise an error is returned immediately.
    """
    if not providers_list:
        return []
    if not allow_custom:
        return [
            "Config declares custom providers but loading is not enabled. "
            "Set \"allow_custom_providers\": true in your config or pass --allow-custom-providers."
        ]
    errors: List[str] = []
    for entry in providers_list:
        module_name = entry.get("module", "")
        class_name = entry.get("class", "")
        if not module_name or not class_name:
            errors.append(
                f"Provider entry missing required fields 'module'/'class': {entry!r}"
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
                "custom provider names must start with 'custom.'"
            )
            continue
        register_provider(instance)
    return errors
