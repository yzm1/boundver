"""Lockfile generation and verification for boundver."""

import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Union

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
from .providers import PathHashProvider, ProviderContext, compute_boundary, get_provider, load_custom_providers
from .versions import extract_version, parse_semver

LOCKFILE_SCHEMA = "boundary-lock/v1"
LOCKFILE_SCHEMA_URL = "https://raw.githubusercontent.com/yzm1/boundver/main/spec/boundary.lock.schema.json"


def generate_lockfile(
    config: dict, repo_root: Path, source: Union[str, SourceMode] = "head", strict: bool = True,
    allow_custom_providers: bool = False,
) -> dict:
    """Generate the full lockfile from config + repo state."""
    provider_errors = load_custom_providers(
        config.get("providers", []), allow_custom=allow_custom_providers
    )
    if provider_errors:
        raise ProviderError("Custom provider loading failed:\n" + "\n".join(provider_errors))
    components_config = config["components"]
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
            name, comp, repo_root, source, defaults, accessor,
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
        self.source = source

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
) -> dict:
    """Compute the lockfile entry for a single component."""
    raw_path = comp.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ConfigError(f"Component '{name}' has invalid or missing 'path'")
    comp_path = raw_path.rstrip("/")
    version = extract_version(
        repo_root, comp_path, comp.get("version_source"), git_latest_tag,
        read_file_fn=accessor.version_read_file,
    )
    compat, api_ver, exact_ver = parse_semver(version)
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
    provider = get_provider(bp_name)
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
            api_digest, boundary_status, boundary_errors = compute_boundary(provider, ctx)
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            api_digest = None
            boundary_status = "error"
            boundary_errors = [f"Boundary digest failed: {exc}"]

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
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            behavior_digest = None
            boundary_errors.append(f"behavior computation failed: {exc}")

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
    if exact_errors:
        entry["exact_errors"] = exact_errors

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
    selected = sorted(set(selected_components))
    components_cfg = config.get("components", {})
    missing = [n for n in selected if n not in components_cfg]
    if missing:
        raise ConfigError(f"Unknown component(s): {', '.join(missing)}")

    subset_config = dict(config)
    subset_config["components"] = {n: components_cfg[n] for n in selected}
    subset_config["slices"] = {}
    subset_lock = generate_lockfile(
        subset_config, repo_root, source=source, strict=strict,
        allow_custom_providers=allow_custom_providers,
    )

    if out_path.exists():
        try:
            merged = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"Existing lockfile at {out_path} is not valid JSON: {exc}"
            ) from exc
    else:
        merged = {
            "$schema": LOCKFILE_SCHEMA_URL,
            "schema": LOCKFILE_SCHEMA,
            "project": config.get("project", "unknown"),
            "components": {},
            "slices": {},
        }
    merged["schema"] = LOCKFILE_SCHEMA
    merged["project"] = config.get("project", "unknown")
    if not isinstance(merged.get("components"), dict):
        merged["components"] = {}
    if not isinstance(merged.get("slices"), dict):
        merged["slices"] = {}

    for name in selected:
        merged["components"][name] = subset_lock["components"][name]

    for sname, sdef in config.get("slices", {}).items():
        slice_components = sdef.get("components", [])
        if any(c in selected for c in slice_components):
            merged["slices"][sname] = _recompute_slice_entry(
                sname, sdef, merged["components"], strict=strict
            )

    return merged


def _lockfile_schema_issues(lockfile: dict) -> List[str]:
    schema = lockfile.get("schema")
    if schema is None:
        return [f"LOCKFILE schema missing (expected {LOCKFILE_SCHEMA})"]
    if schema != LOCKFILE_SCHEMA:
        return [f"LOCKFILE schema unsupported: {schema} (expected {LOCKFILE_SCHEMA})"]
    return []


def _lockfile_structure_issues(lockfile: dict) -> List[str]:
    issues: List[str] = []
    if not isinstance(lockfile.get("components"), dict):
        issues.append("LOCKFILE malformed: components must be an object")
        return issues
    if not isinstance(lockfile.get("slices"), dict):
        issues.append("LOCKFILE malformed: slices must be an object")
    for name, comp in lockfile.get("components", {}).items():
        if not isinstance(comp, dict):
            issues.append(f"LOCKFILE malformed: component '{name}' must be an object")
            continue
        fps = comp.get("fingerprints")
        if not isinstance(fps, dict):
            issues.append(f"LOCKFILE malformed: component '{name}' missing fingerprints object")
            continue
        for required in ("exact", "behavior", "boundary", "compat"):
            if required not in fps:
                issues.append(f"LOCKFILE malformed: component '{name}' missing fingerprints.{required}")
    return issues


def verify_lockfile(
    config: dict,
    lockfile: dict,
    repo_root: Path,
    source: Union[str, SourceMode] = "head",
    components_filter: Optional[List[str]] = None,
    allow_custom_providers: bool = False,
    fail_fast: bool = False,
) -> List[str]:
    """Check if the lockfile matches current repo state. Returns list of mismatches.

    When *fail_fast* is True, returns after the first mismatch is found. This avoids
    computing fingerprints for remaining components, significantly reducing verification
    time in large monorepos when you only need a pass/fail signal.
    """
    # Load custom providers once up front.
    provider_errors = load_custom_providers(
        config.get("providers", []), allow_custom=allow_custom_providers
    )
    if provider_errors:
        return [f"Custom provider loading failed: {e}" for e in provider_errors]

    issues = _lockfile_schema_issues(lockfile)
    issues.extend(_lockfile_structure_issues(lockfile))
    if issues:
        return issues

    selected = set(components_filter or [])
    use_filter = len(selected) > 0

    # Determine which components to check.
    all_components = config.get("components", {})
    if use_filter:
        check_components = {n: all_components[n] for n in components_filter if n in all_components}
    else:
        check_components = all_components

    defaults = config.get("defaults", {})
    accessor = _SourceAccessor(repo_root, source)

    # Per-component verification with optional early exit.
    computed_entries: Dict[str, dict] = {}
    for name, comp_cfg in check_components.items():
        current_comp = _compute_component_entry(name, comp_cfg, repo_root, source, defaults, accessor)
        computed_entries[name] = current_comp
        locked_comp = lockfile.get("components", {}).get(name)
        if locked_comp is None:
            issues.append(f"NEW component not in lockfile: {name}")
            if fail_fast:
                return issues
            continue
        for facet in ("exact", "behavior", "boundary", "compat"):
            cv = current_comp["fingerprints"].get(facet)
            locked_fps = locked_comp.get("fingerprints", {})
            lv = locked_fps.get(facet)
            if cv != lv:
                issues.append(
                    f"MISMATCH {name}.{facet}: lockfile={_short(lv)} current={_short(cv)}"
                )
                if fail_fast:
                    return issues

        # Check for vendored copy drift
        for warning in current_comp.get("warnings", []):
            issues.append(f"VENDORED DRIFT {name}: {warning}")
            if fail_fast:
                return issues

    for name in lockfile.get("components", {}):
        if use_filter and name not in selected:
            continue
        if name not in check_components:
            issues.append(f"REMOVED component still in lockfile: {name}")
            if fail_fast:
                return issues

    # Check slice fingerprints (skip when using component filter, as slices
    # are not regenerated in that case).
    if not use_filter:
        slices_config = config.get("slices", {})
        if slices_config:
            for sname, sdef in slices_config.items():
                current_slice = _recompute_slice_entry(sname, sdef, computed_entries, strict=False)
                locked_slice = lockfile.get("slices", {}).get(sname)
                if locked_slice is None:
                    issues.append(f"NEW slice not in lockfile: {sname}")
                    if fail_fast:
                        return issues
                elif locked_slice.get("fingerprint") != current_slice.get("fingerprint"):
                    issues.append(
                        f"SLICE MISMATCH {sname}: lockfile={_short(locked_slice.get('fingerprint'))} "
                        f"current={_short(current_slice.get('fingerprint'))}"
                    )
                    if fail_fast:
                        return issues
            for sname in lockfile.get("slices", {}):
                if sname not in slices_config:
                    issues.append(f"REMOVED slice still in lockfile: {sname}")
                    if fail_fast:
                        return issues

    return issues


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

# All schema versions this tool can read and migrate from.
KNOWN_SCHEMAS = frozenset({"boundary-lock/v1"})


class MigrationError(ValueError):
    """Raised when a lockfile cannot be migrated to the current schema."""


def migrate_lockfile(lockfile: dict) -> dict:
    """Upgrade *lockfile* to the current schema.

    Returns a new dict; does not mutate the input.
    Raises ``MigrationError`` if the schema is absent or unrecognised.

    Today only ``boundary-lock/v1`` exists so this normalises in-place:
    strips the legacy ``generated_at`` field, ensures ``schema`` is the
    current value, and defaults ``components``/``slices`` to empty dicts.
    When a v2 is introduced, add a migration step here before normalising.
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
    migrated = dict(lockfile)
    migrated.pop("generated_at", None)          # legacy field removed in v1 final
    migrated["schema"] = LOCKFILE_SCHEMA        # normalise to current constant
    migrated.setdefault("components", {})
    migrated.setdefault("slices", {})
    return migrated

