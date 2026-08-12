"""Lockfile generation and verification for boundver."""

import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Union

from ._git import _git_cat_blob, _list_files_for_source, _to_posix, git_latest_tag
from ._hashing import (
    MAX_HASH_FILE_BYTES,
    _content_only_digest,
    _enforce_content_size,
    canonical_json,
    sha256_hex,
    source_tree_digest,
)
from ._utils import _short, boundary_provider_name, ConfigError, GuardrailError, ProviderError, SourceMode
from .providers import (
    PathHashProvider,
    ProviderContext,
    compute_boundary,
    create_registry,
    get_provider,
    load_custom_providers,
)
from .versions import extract_version, parse_semver

LOCKFILE_SCHEMA = "boundary-lock/v2"
LOCKFILE_SCHEMA_URL = "https://raw.githubusercontent.com/yzm1/boundver/main/spec/boundary.lock.schema.json"

# Persisted fields whose integrity matters independently of the four digests.
# Keep this list shared by verify/diff/why so none of those views can report a
# stale entry as current merely because its fingerprints happen to match.
COMPONENT_METADATA_FIELDS = (
    "version", "path", "boundary_provider", "boundary_provider_version",
    "boundary_status", "semver", "consumers", "boundary_metadata",
    "version_errors", "exact_errors", "behavior_errors", "boundary_errors", "warnings",
    "vendored_copies", "vendored_digests",
)


def _normalize_source(source: Union[str, SourceMode]) -> str:
    value = source.value if isinstance(source, SourceMode) else source
    if value not in {"head", "index", "working-tree"}:
        raise ConfigError(
            f"Unknown source mode {value!r}; expected head, index, or working-tree"
        )
    return value


def generate_lockfile(
    config: dict, repo_root: Path, source: Union[str, SourceMode] = "head", strict: bool = True,
    allow_custom_providers: bool = False,
) -> dict:
    """Generate the full lockfile from config + repo state."""
    source = _normalize_source(source)
    if not isinstance(config, dict):
        raise ConfigError("Config root must be an object")
    components_config = config.get("components")
    if not isinstance(components_config, dict) or not components_config:
        raise ConfigError("Config must define at least one component")
    registry = create_registry()
    provider_errors = load_custom_providers(
        config.get("providers", []), allow_custom=allow_custom_providers,
        registry=registry,
    )
    if provider_errors:
        raise ProviderError("Custom provider loading failed:\n" + "\n".join(provider_errors))
    slices_config = config.get("slices", {})
    defaults = config.get("defaults", {})

    lockfile: dict = {
        "$schema": LOCKFILE_SCHEMA_URL,
        "schema": LOCKFILE_SCHEMA,
        "project": config.get("project", "unknown"),
        "components": {},
        "slices": {},
    }

    accessor = _SourceAccessor(repo_root, source)

    # --- Components ---
    for name, comp in components_config.items():
        lockfile["components"][name] = _compute_component_entry(
            name, comp, repo_root, source, defaults, accessor, registry,
        )

    generation_errors = _generation_errors(lockfile)
    if strict and generation_errors:
        raise ConfigError(
            "Lockfile generation failed:\n" + "\n".join(generation_errors)
        )

    # --- Slices ---
    for slice_name, slice_def in slices_config.items():
        lockfile["slices"][slice_name] = _recompute_slice_entry(
            slice_name, slice_def, lockfile["components"], strict=strict
        )

    return lockfile


# ---------------------------------------------------------------------------
# Source accessor — centralizes git-aware I/O callbacks
# ---------------------------------------------------------------------------

class _SourceAccessor:
    """Provides read_file, list_files, and version_read_file for a given source mode."""

    def __init__(self, repo_root: Path, source: str):
        self.repo_root = repo_root
        self.source = _normalize_source(source)

    def read_file(self, repo_rel: str) -> bytes:
        """Read file content for hashing."""
        src = self.source
        if src == "head":
            data = _git_cat_blob(self.repo_root, f"HEAD:{repo_rel}")
        elif src == "index":
            data = _git_cat_blob(self.repo_root, f":{repo_rel}")
        else:
            full = self.repo_root / repo_rel
            if full.is_symlink():
                # Hash symlink target string (matches git's blob storage for symlinks).
                target = os.readlink(full)
                data = target.encode("utf-8") if isinstance(target, str) else target
            else:
                sz = full.stat().st_size
                if sz > MAX_HASH_FILE_BYTES:
                    raise GuardrailError(
                        f"Hash guardrail exceeded: file too large ({sz} bytes) at {repo_rel}"
                    )
                data = full.read_bytes()
        _enforce_content_size(data, repo_rel)
        return data

    def list_files(self, prefix: str) -> List[str]:
        """List files under a prefix."""
        return _list_files_for_source(self.repo_root, prefix, self.source)

    def version_read_file(self, repo_rel: str) -> bytes:
        """Read file content for version extraction."""
        if self.source == "head":
            return _git_cat_blob(self.repo_root, f"HEAD:{repo_rel}")
        elif self.source == "index":
            return _git_cat_blob(self.repo_root, f":{repo_rel}")
        else:
            fpath = self.repo_root / repo_rel
            if fpath.is_symlink():
                raise ConfigError(
                    f"Version source must not be a symlink: {repo_rel}"
                )
            try:
                fpath.resolve().relative_to(self.repo_root.resolve())
            except (ValueError, OSError) as exc:
                raise ConfigError(
                    f"Version source escapes repository: {repo_rel}"
                ) from exc
            size = fpath.stat().st_size
            if size > MAX_HASH_FILE_BYTES:
                raise GuardrailError(
                    f"Version source file too large ({size} bytes): {repo_rel}"
                )
            return fpath.read_bytes()


# ---------------------------------------------------------------------------
# Per-component fingerprint computation
# ---------------------------------------------------------------------------

def _compute_component_entry(
    name: str,
    comp: dict,
    repo_root: Path,
    source: str,
    defaults: dict,
    accessor: "_SourceAccessor",
    registry: Optional[dict] = None,
) -> dict:
    """Compute the lockfile entry for a single component."""
    raw_path = comp.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ConfigError(f"Component '{name}' has invalid or missing 'path'")
    comp_path = _to_posix(os.path.normpath(raw_path.strip()))
    version = extract_version(
        repo_root, comp_path, comp.get("version_source"), git_latest_tag,
        read_file_fn=accessor.version_read_file,
    )
    compat, api_ver, exact_ver = parse_semver(version)
    version_errors: List[str] = []
    if comp.get("version_source") is not None:
        if version is None:
            version_errors.append("Configured version source did not produce a version")
        elif compat is None:
            version_errors.append(
                f"Configured version is not valid SemVer: {version!r}"
            )
    compat_mode = defaults.get("compat_mode", "major")

    # Exact fingerprint: git tree hash of the whole component directory
    exact_errors: List[str] = []
    try:
        exact_digest = source_tree_digest(repo_root, comp_path, source=source)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        exact_digest = None
        exact_errors.append(f"Exact digest failed: {exc}")

    if exact_digest is None and not exact_errors:
        if source in ("head", "index"):
            exact_errors.append(
                f"No files found for '{comp_path}' at {source.upper()}. "
                f"Have you committed this path? Try --source working-tree"
            )
        else:
            exact_errors.append(f"No files found for '{comp_path}' on disk")

    # API fingerprint: resolve via registered provider
    boundary = comp.get("boundary", {})
    bp_name = boundary_provider_name(boundary)
    provider = get_provider(bp_name, registry=registry)
    provider_metadata = None
    if provider is None:
        api_digest = None
        boundary_status = "error"
        boundary_errors: List[str] = [f"Unknown boundary provider: {bp_name!r}"]
    else:
        ctx = ProviderContext(
            repo_root=repo_root,
            component_path=comp_path,
            boundary_cfg=boundary,
            source=source,
            read_file=accessor.read_file,
            list_files=accessor.list_files,
        )
        try:
            api_digest, boundary_status, boundary_errors, provider_metadata = compute_boundary(
                provider, ctx, include_metadata=True
            )
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            api_digest = None
            boundary_status = "error"
            boundary_errors = [f"Boundary digest failed: {exc}"]
            provider_metadata = None

    # Behavior fingerprint: broader contract files (superset of boundary)
    behavior_cfg = comp.get("behavior")
    behavior_digest = None
    if behavior_cfg and behavior_cfg.get("paths"):
        behavior_provider = PathHashProvider()
        behavior_provider.name = "behavior"
        behavior_ctx = ProviderContext(
            repo_root=repo_root,
            component_path=comp_path,
            boundary_cfg=behavior_cfg,
            source=source,
            read_file=accessor.read_file,
            list_files=accessor.list_files,
        )
        try:
            behavior_digest, _bstatus, _berrs = compute_boundary(behavior_provider, behavior_ctx)
            behavior_errors = list(_berrs) if _bstatus == "error" else []
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            behavior_digest = None
            behavior_errors = [f"Behavior digest failed: {exc}"]
    else:
        behavior_errors = []

    # Compatibility fingerprint: derived from semver major (or major.minor)
    compat_digest = None
    compat_identity = None
    if compat_mode in {"major", "semver_major"}:
        compat_identity = compat
    elif compat_mode == "semver_major_minor":
        compat_identity = api_ver

    if compat_identity is not None:
        compat_digest = sha256_hex(f"{name}@compat:{compat_identity}")

    entry: dict = {
        "version": version,
        "path": comp_path,
        "boundary_provider": bp_name,
        "boundary_provider_version": getattr(provider, "version", "1") if provider else None,
        "boundary_status": boundary_status,
        "consumers": sorted(set(comp.get("consumers", []))),
        "fingerprints": {
            "exact": exact_digest,
            "behavior": behavior_digest,
            "boundary": api_digest,
            "compat": compat_digest,
        },
        "semver": {
            "compat_family": compat,
            "api_surface": api_ver,
            "exact_version": exact_ver,
        },
    }
    if boundary_errors:
        entry["boundary_errors"] = boundary_errors
    if provider_metadata is not None:
        entry["boundary_metadata"] = provider_metadata
    if version_errors:
        entry["version_errors"] = version_errors
    if exact_errors:
        entry["exact_errors"] = exact_errors
    if behavior_errors:
        entry["behavior_errors"] = behavior_errors

    # Flag vendored copies
    if "vendored_copies" in comp:
        entry["vendored_copies"] = comp["vendored_copies"]
        source_content_hash = _content_only_digest(repo_root, comp_path, source=source)
        for vc in comp["vendored_copies"]:
            vc_content_hash = _content_only_digest(repo_root, vc.rstrip("/"), source=source)
            entry.setdefault("vendored_digests", {})[vc] = vc_content_hash
            if vc_content_hash != source_content_hash:
                entry.setdefault("warnings", []).append(
                    f"Vendored copy at {vc} differs from source (source={source_content_hash}, copy={vc_content_hash})"
                )

    return entry


def _generation_errors(lockfile: dict) -> List[str]:
    """Return fingerprint computation failures that make a lock unsafe to bless."""
    errors: List[str] = []
    for name, entry in lockfile.get("components", {}).items():
        for message in entry.get("version_errors", []):
            errors.append(f"{name}: {message}")
        for message in entry.get("exact_errors", []):
            errors.append(f"{name}: {message}")
        for message in entry.get("behavior_errors", []):
            errors.append(f"{name}: {message}")
        if entry.get("boundary_status") == "error":
            messages = entry.get("boundary_errors", []) or ["Boundary computation failed"]
            for message in messages:
                errors.append(f"{name}: {message}")
    return errors


def _recompute_slice_entry(
    slice_name: str,
    slice_def: dict,
    components_map: Dict[str, dict],
    strict: bool = True,
) -> dict:
    mode = slice_def.get("mode", "exact")
    component_names = slice_def.get("components", [])
    digest_parts: Dict[str, Optional[str]] = {}
    for cname in sorted(component_names):
        comp_entry = components_map.get(cname)
        if comp_entry is None:
            digest_parts[cname] = None
            continue
        fp = comp_entry.get("fingerprints", {})
        if mode == "exact":
            digest_parts[cname] = fp.get("exact")
        elif mode == "behavior":
            if fp.get("behavior") is None and strict:
                raise ConfigError(f"Slice '{slice_name}' requires behavior digest for component '{cname}'")
            digest_parts[cname] = fp.get("behavior")
        elif mode == "boundary":
            if fp.get("boundary") is None and strict:
                raise ConfigError(f"Slice '{slice_name}' requires boundary digest for component '{cname}'")
            digest_parts[cname] = fp.get("boundary")
        elif mode == "compat":
            if fp.get("compat") is None and strict:
                raise ConfigError(f"Slice '{slice_name}' requires compat digest for component '{cname}'")
            digest_parts[cname] = fp.get("compat")
        else:
            raise ConfigError(f"Unknown slice mode: {mode}")
    return {
        "description": slice_def.get("description", ""),
        "mode": mode,
        "components": sorted(component_names),
        "fingerprint": sha256_hex(canonical_json(digest_parts)),
        "component_digests": digest_parts,
    }


def generate_lockfile_for_components(
    config: dict,
    repo_root: Path,
    selected_components: List[str],
    out_path: Path,
    source: str = "head",
    strict: bool = True,
    allow_custom_providers: bool = False,
) -> dict:
    """Generate/update lockfile only for selected components and impacted slices."""
    source = _normalize_source(source)
    selected = sorted(set(selected_components))
    components_cfg = config.get("components", {})
    missing = [n for n in selected if n not in components_cfg]
    if missing:
        raise ConfigError(f"Unknown component(s): {', '.join(missing)}")

    if not out_path.exists():
        raise ConfigError(
            "Cannot generate a component subset without an existing v2 lockfile. "
            "Run a full `boundver generate` first."
        )
    try:
        merged = json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Existing lockfile at {out_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(merged, dict):
        raise ConfigError(
            f"Existing lockfile at {out_path} must contain a JSON object"
        )
    if merged.get("schema") != LOCKFILE_SCHEMA:
        raise ConfigError(
            f"Cannot partially update schema {merged.get('schema')!r}; "
            f"run a full `boundver generate` to create {LOCKFILE_SCHEMA}."
        )
    structure_issues = _lockfile_structure_issues(merged)
    if structure_issues:
        raise ConfigError(
            "Cannot partially update a malformed lockfile:\n"
            + "\n".join(structure_issues)
        )
    if merged.get("project") != config.get("project", "unknown"):
        raise ConfigError(
            "Cannot partially update after the project name changed. "
            "Run a full `boundver generate`."
        )

    # Recompute every component before merging. This makes component-scoped
    # generation an output-selection convenience, not a way to preserve stale
    # digests after config/default/provider changes.
    current_lock = generate_lockfile(
        config, repo_root, source=source, strict=strict,
        allow_custom_providers=allow_custom_providers,
    )
    for name in set(components_cfg) - set(selected):
        if merged.get("components", {}).get(name) != current_lock["components"][name]:
            raise ConfigError(
                f"Cannot partially generate because unselected component '{name}' "
                "is stale. Run a full `boundver generate`."
            )
    merged["$schema"] = LOCKFILE_SCHEMA_URL
    merged["schema"] = LOCKFILE_SCHEMA
    merged["project"] = config.get("project", "unknown")
    if not isinstance(merged.get("components"), dict):
        merged["components"] = {}
    if not isinstance(merged.get("slices"), dict):
        merged["slices"] = {}

    for name in selected:
        merged["components"][name] = current_lock["components"][name]

    # A partial refresh must still reconcile config removals and every slice.
    configured_names = set(components_cfg)
    merged["components"] = {
        name: entry
        for name, entry in merged["components"].items()
        if name in configured_names
    }
    missing_entries = sorted(configured_names - set(merged["components"]))
    if missing_entries:
        raise ConfigError(
            "Cannot partially generate because the existing lockfile has no entry for: "
            + ", ".join(missing_entries)
            + ". Run a full `boundver generate`."
        )

    merged["slices"] = {}
    for sname, sdef in config.get("slices", {}).items():
        merged["slices"][sname] = _recompute_slice_entry(
            sname, sdef, merged["components"], strict=strict
        )

    errors = _generation_errors(merged)
    if strict and errors:
        raise ConfigError("Lockfile generation failed:\n" + "\n".join(errors))

    return merged


def _lockfile_schema_issues(lockfile: dict) -> List[str]:
    if not isinstance(lockfile, dict):
        return ["LOCKFILE malformed: root must be an object"]
    schema = lockfile.get("schema")
    if schema is None:
        return [f"LOCKFILE schema missing (expected {LOCKFILE_SCHEMA})"]
    if schema != LOCKFILE_SCHEMA:
        return [f"LOCKFILE schema unsupported: {schema} (expected {LOCKFILE_SCHEMA})"]
    return []


def _lockfile_structure_issues(lockfile: dict) -> List[str]:
    issues: List[str] = []
    if not isinstance(lockfile, dict):
        return ["LOCKFILE malformed: root must be an object"]
    if not isinstance(lockfile.get("project"), str) or not lockfile.get("project"):
        issues.append("LOCKFILE malformed: project must be a non-empty string")
    if not isinstance(lockfile.get("components"), dict):
        issues.append("LOCKFILE malformed: components must be an object")
        return issues
    if not isinstance(lockfile.get("slices"), dict):
        issues.append("LOCKFILE malformed: slices must be an object")
    for name, comp in lockfile.get("components", {}).items():
        if not isinstance(comp, dict):
            issues.append(f"LOCKFILE malformed: component '{name}' must be an object")
            continue
        for field in ("version", "boundary_provider_version"):
            value = comp.get(field)
            if field not in comp or (value is not None and not isinstance(value, str)):
                issues.append(
                    f"LOCKFILE malformed: component '{name}' {field} "
                    "must be a string or null"
                )
        for field in ("path", "boundary_provider"):
            if not isinstance(comp.get(field), str) or not comp.get(field):
                issues.append(
                    f"LOCKFILE malformed: component '{name}' {field} "
                    "must be a non-empty string"
                )
        if comp.get("boundary_status") not in {"ok", "partial", "error"}:
            issues.append(
                f"LOCKFILE malformed: component '{name}' boundary_status must be "
                "one of ok, partial, or error"
            )
        consumers = comp.get("consumers")
        if (
            not isinstance(consumers, list)
            or not all(isinstance(item, str) for item in consumers)
            or len(consumers) != len(set(consumers))
        ):
            issues.append(
                f"LOCKFILE malformed: component '{name}' consumers must be an "
                "array of unique strings"
            )
        fps = comp.get("fingerprints")
        if not isinstance(fps, dict):
            issues.append(f"LOCKFILE malformed: component '{name}' missing fingerprints object")
        else:
            for required in ("exact", "behavior", "boundary", "compat"):
                if required not in fps:
                    issues.append(f"LOCKFILE malformed: component '{name}' missing fingerprints.{required}")
                elif fps[required] is not None and not isinstance(fps[required], str):
                    issues.append(
                        f"LOCKFILE malformed: component '{name}' fingerprints.{required} "
                        "must be a string or null"
                    )
        semver = comp.get("semver")
        if not isinstance(semver, dict):
            issues.append(
                f"LOCKFILE malformed: component '{name}' semver must be an object"
            )
        else:
            for field in ("compat_family", "api_surface", "exact_version"):
                value = semver.get(field)
                if field not in semver or (
                    value is not None and not isinstance(value, str)
                ):
                    issues.append(
                        f"LOCKFILE malformed: component '{name}' semver.{field} "
                        "must be a string or null"
                    )
        for field in (
            "version_errors", "exact_errors", "behavior_errors",
            "boundary_errors", "warnings", "vendored_copies",
        ):
            value = comp.get(field)
            if value is not None and (
                not isinstance(value, list)
                or not all(isinstance(item, str) for item in value)
            ):
                issues.append(
                    f"LOCKFILE malformed: component '{name}' {field} must be an array of strings"
                )
        boundary_metadata = comp.get("boundary_metadata")
        if boundary_metadata is not None and not isinstance(boundary_metadata, dict):
            issues.append(
                f"LOCKFILE malformed: component '{name}' boundary_metadata "
                "must be an object or null"
            )
        vendored_digests = comp.get("vendored_digests")
        if vendored_digests is not None and (
            not isinstance(vendored_digests, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in vendored_digests.items()
            )
        ):
            issues.append(
                f"LOCKFILE malformed: component '{name}' vendored_digests must "
                "be an object with string values"
            )
    if isinstance(lockfile.get("slices"), dict):
        for name, slice_entry in lockfile["slices"].items():
            if not isinstance(slice_entry, dict):
                issues.append(f"LOCKFILE malformed: slice '{name}' must be an object")
                continue
            fingerprint = slice_entry.get("fingerprint")
            if not isinstance(fingerprint, str):
                issues.append(
                    f"LOCKFILE malformed: slice '{name}' fingerprint must be a string"
                )
            if not isinstance(slice_entry.get("description"), str):
                issues.append(
                    f"LOCKFILE malformed: slice '{name}' description must be a string"
                )
            if slice_entry.get("mode") not in {
                "exact", "behavior", "boundary", "compat",
            }:
                issues.append(
                    f"LOCKFILE malformed: slice '{name}' mode must be one of "
                    "exact, behavior, boundary, or compat"
                )
            slice_components = slice_entry.get("components")
            if (
                not isinstance(slice_components, list)
                or not all(isinstance(item, str) for item in slice_components)
            ):
                issues.append(
                    f"LOCKFILE malformed: slice '{name}' components must be an array of strings"
                )
            component_digests = slice_entry.get("component_digests")
            if (
                not isinstance(component_digests, dict)
                or not all(
                    isinstance(key, str)
                    and (value is None or isinstance(value, str))
                    for key, value in component_digests.items()
                )
            ):
                issues.append(
                    f"LOCKFILE malformed: slice '{name}' component_digests must "
                    "be an object with string or null values"
                )
    return issues


def verify_lockfile(
    config: dict,
    lockfile: dict,
    repo_root: Path,
    source: Union[str, SourceMode] = "head",
    components_filter: Optional[List[str]] = None,
    allow_custom_providers: bool = False,
    fail_fast: bool = False,
    facets: Optional[List[str]] = None,
    observations: Optional[List[str]] = None,
) -> List[str]:
    """Check if the lockfile matches current repo state. Returns list of mismatches.

    When *fail_fast* is True, all selected components are still evaluated so the
    process can return the globally highest-severity exit condition; only the
    returned mismatch report is limited to one item.
    """
    source = _normalize_source(source)
    # Compute all selected entries even when only one issue is requested. This
    # is necessary to preserve the global highest-severity exit-code contract
    # across components; ``fail_fast`` limits the returned report, not safety
    # evaluation.
    limit_report = fail_fast
    fail_fast = False

    # Load custom providers once up front.
    if not isinstance(config, dict):
        return ["Config root must be an object"]
    if not isinstance(lockfile, dict):
        return ["LOCKFILE malformed: root must be an object"]
    if not isinstance(config.get("components"), dict) or not config.get("components"):
        return ["Config malformed: components must be a non-empty object"]
    if not isinstance(config.get("slices", {}), dict):
        return ["Config malformed: slices must be an object"]
    registry = create_registry()
    provider_errors = load_custom_providers(
        config.get("providers", []), allow_custom=allow_custom_providers,
        registry=registry,
    )
    if provider_errors:
        return [f"Custom provider loading failed: {e}" for e in provider_errors]

    issues = _lockfile_schema_issues(lockfile)
    issues.extend(_lockfile_structure_issues(lockfile))
    if issues:
        return issues
    if lockfile.get("project") != config.get("project", "unknown"):
        issues.append(
            f"METADATA MISMATCH project: lockfile={lockfile.get('project')!r} "
            f"current={config.get('project', 'unknown')!r}"
        )
        if fail_fast:
            return issues

    selected = set(components_filter or [])
    use_filter = len(selected) > 0
    gated_facets: Set[str] = set(facets or ("exact", "behavior", "boundary", "compat"))
    unknown_facets = gated_facets - {"exact", "behavior", "boundary", "compat"}
    if unknown_facets:
        return [f"Unknown verification facet(s): {', '.join(sorted(unknown_facets))}"]
    non_gating = observations if observations is not None else []

    # Determine which components to check.
    all_components = config.get("components", {})
    unknown_components = selected - set(all_components)
    if unknown_components:
        return [
            "Unknown verification component(s): "
            + ", ".join(sorted(unknown_components))
        ]
    if use_filter:
        check_components = {n: all_components[n] for n in components_filter if n in all_components}
    else:
        check_components = all_components

    defaults = config.get("defaults", {})
    accessor = _SourceAccessor(repo_root, source)

    # Per-component verification with optional early exit.
    computed_entries: Dict[str, dict] = {}
    for name, comp_cfg in check_components.items():
        current_comp = _compute_component_entry(
            name, comp_cfg, repo_root, source, defaults, accessor, registry
        )
        computed_entries[name] = current_comp
        locked_comp = lockfile.get("components", {}).get(name)
        if locked_comp is None:
            issues.append(f"NEW component not in lockfile: {name}")
            if fail_fast:
                return issues
            continue
        current_errors = _generation_errors({"components": {name: current_comp}})
        locked_errors = _generation_errors({"components": {name: locked_comp}})
        for message in current_errors:
            issues.append(f"CURRENT DIGEST ERROR {message}")
            if fail_fast:
                return issues
        for message in locked_errors:
            issues.append(f"LOCKED DIGEST ERROR {message}")
            if fail_fast:
                return issues
        # Highest policy severity first keeps --fail-fast compatible with the
        # documented exit-code contract.
        for facet in ("compat", "boundary", "behavior", "exact"):
            cv = current_comp["fingerprints"].get(facet)
            locked_fps = locked_comp.get("fingerprints", {})
            lv = locked_fps.get(facet)
            if cv != lv:
                message = f"MISMATCH {name}.{facet}: lockfile={_short(lv)} current={_short(cv)}"
                if facet in gated_facets:
                    issues.append(message)
                    if facet in {"boundary", "compat"}:
                        consumers = sorted(set(comp_cfg.get("consumers", [])))
                        if consumers:
                            issues.append(
                                f"AFFECTED CONSUMERS {name}: {', '.join(consumers)}"
                            )
                    if fail_fast:
                        return issues
                else:
                    non_gating.append(message)

        for field in COMPONENT_METADATA_FIELDS:
            if locked_comp.get(field) != current_comp.get(field):
                issues.append(
                    f"METADATA MISMATCH {name}.{field}: "
                    f"lockfile={locked_comp.get(field)!r} current={current_comp.get(field)!r}"
                )
                if fail_fast:
                    return issues

        # Check for vendored copy drift
        for warning in current_comp.get("warnings", []):
            issues.append(f"VENDORED DRIFT {name}: {warning}")
            if fail_fast:
                return issues

    for name in lockfile.get("components", {}):
        if name not in check_components:
            if name in all_components:
                continue
            issues.append(f"REMOVED component still in lockfile: {name}")
            if fail_fast:
                return issues

    # Check every slice affected by the selected components. This prevents a
    # component-filtered verify from silently ignoring its aggregate contract.
    slices_config = config.get("slices", {})
    if slices_config:
        slice_component_names = set()
        for sdef in slices_config.values():
            slice_component_names.update(sdef.get("components", []))
        for cname in sorted(slice_component_names):
            if cname not in computed_entries and cname in all_components:
                computed_entries[cname] = _compute_component_entry(
                    cname, all_components[cname], repo_root, source, defaults, accessor, registry
                )
        for sname, sdef in slices_config.items():
            if use_filter and not (selected & set(sdef.get("components", []))):
                continue
            current_slice = _recompute_slice_entry(sname, sdef, computed_entries, strict=False)
            locked_slice = lockfile.get("slices", {}).get(sname)
            if locked_slice is None:
                issues.append(f"NEW slice not in lockfile: {sname}")
                if fail_fast:
                    return issues
            elif locked_slice != current_slice:
                message = (
                    f"SLICE MISMATCH {sname}.{sdef.get('mode', 'exact')}: "
                    f"lockfile={_short(locked_slice.get('fingerprint'))} "
                    f"current={_short(current_slice.get('fingerprint'))}"
                )
                if sdef.get("mode", "exact") in gated_facets:
                    issues.append(message)
                    if fail_fast:
                        return issues
                else:
                    non_gating.append(message)
    for sname in lockfile.get("slices", {}):
        if sname not in slices_config:
            issues.append(f"REMOVED slice still in lockfile: {sname}")
            if fail_fast:
                return issues

    if limit_report and issues:
        def _severity(message: str) -> int:
            if ".compat:" in message or ".compat " in message:
                return 5
            if ".boundary:" in message or ".boundary " in message:
                return 4
            if ".behavior:" in message or ".behavior " in message:
                return 3
            return 1

        return [max(issues, key=_severity)]
    return issues


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

# All schema versions this tool can read and migrate from.
KNOWN_SCHEMAS = frozenset({"boundary-lock/v1", "boundary-lock/v2"})


class MigrationError(ValueError):
    """Raised when a lockfile cannot be migrated to the current schema."""


def migrate_lockfile(lockfile: dict) -> dict:
    """Upgrade *lockfile* to the current schema.

    Returns a new dict; does not mutate the input.
    Raises ``MigrationError`` if the schema is absent or unrecognised.

    Hash contract v1 lockfiles cannot be mechanically upgraded because their
    fingerprints must be recomputed from repository content.
    """
    schema = lockfile.get("schema")
    if schema is None:
        raise MigrationError(
            "Lockfile has no 'schema' field — cannot determine version to migrate from."
        )
    if schema not in KNOWN_SCHEMAS:
        raise MigrationError(
            f"Unknown lockfile schema '{schema}'. "
            f"Supported: {', '.join(sorted(KNOWN_SCHEMAS))}. "
            "You may need to upgrade boundver."
        )
    if schema == "boundary-lock/v1":
        raise MigrationError(
            "boundary-lock/v1 uses the ambiguous v1 hash framing and cannot be "
            "migrated without repository content. Run `boundver generate` with "
            "boundver 0.10 or newer to create a v2 lockfile."
        )
    migrated = dict(lockfile)
    migrated.pop("generated_at", None)          # legacy field removed in v1 final
    migrated["schema"] = LOCKFILE_SCHEMA        # normalise to current constant
    migrated.setdefault("components", {})
    migrated.setdefault("slices", {})
    return migrated
