"""Lockfile generation and verification for boundver."""

import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from ._git import _git_cat_blob, _list_files_for_source, _to_posix, git_latest_tag
from ._hashing import (
    _content_only_digest,
    _enforce_content_size,
    _short,
    boundary_provider_name,
    canonical_json,
    sha256_hex,
    source_tree_digest,
)
from .providers import ProviderContext, compute_boundary, get_provider, load_custom_providers
from .versions import extract_version, parse_semver

LOCKFILE_SCHEMA = "boundary-lock/v1"
LOCKFILE_SCHEMA_URL = "https://raw.githubusercontent.com/yzm1/boundver/main/spec/boundary.lock.schema.json"


def generate_lockfile(
    config: dict, repo_root: Path, source: str = "head", strict: bool = True,
    allow_custom_providers: bool = False,
) -> dict:
    """Generate the full lockfile from config + repo state."""
    provider_errors = load_custom_providers(
        config.get("providers", []), allow_custom=allow_custom_providers
    )
    if provider_errors:
        raise ValueError("Custom provider loading failed:\n" + "\n".join(provider_errors))
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

    def _version_read_file(repo_rel: str) -> bytes:
        """Read file content respecting the source mode (for version extraction)."""
        if source == "head":
            return _git_cat_blob(repo_root, f"HEAD:{repo_rel}")
        elif source == "index":
            return _git_cat_blob(repo_root, f":{repo_rel}")
        else:
            return (repo_root / repo_rel).read_bytes()

    # --- Components ---
    for name, comp in components_config.items():
        comp_path = comp["path"]
        version = extract_version(
            repo_root, comp_path, comp.get("version_source"), git_latest_tag,
            read_file_fn=_version_read_file,
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

        # API fingerprint: resolve via registered provider
        boundary = comp.get("boundary", {})
        boundary_provider = boundary_provider_name(boundary)
        provider = get_provider(boundary_provider)
        if provider is None:
            # Unknown provider — treat as error, no digest
            api_digest = None
            boundary_status = "error"
            boundary_errors: List[str] = [f"Unknown boundary provider: {boundary_provider!r}"]
        else:
            # Build ProviderContext with git-aware callbacks
            def _make_read_file(src: str):
                def _read(repo_rel: str) -> bytes:
                    if src == "head":
                        data = _git_cat_blob(repo_root, f"HEAD:{repo_rel}")
                    elif src == "index":
                        data = _git_cat_blob(repo_root, f":{repo_rel}")
                    else:
                        full = repo_root / repo_rel
                        data = full.read_bytes()
                    _enforce_content_size(data, repo_rel)
                    return data
                return _read

            def _make_list_files(src: str):
                def _list(prefix: str) -> List[str]:
                    return _list_files_for_source(repo_root, prefix, src)
                return _list

            ctx = ProviderContext(
                repo_root=repo_root,
                component_path=comp_path,
                boundary_cfg=boundary,
                source=source,
                read_file=_make_read_file(source),
                list_files=_make_list_files(source),
            )
            try:
                api_digest, boundary_status, boundary_errors = compute_boundary(provider, ctx)
            except (OSError, subprocess.CalledProcessError, ValueError) as exc:
                api_digest = None
                boundary_status = "error"
                boundary_errors = [f"Boundary digest failed: {exc}"]

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
            "boundary_provider": boundary_provider,
            "boundary_status": boundary_status,
            "fingerprints": {
                "exact": exact_digest,
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
            # Check if vendored copies are in sync using path-normalized content digest.
            source_content_hash = _content_only_digest(repo_root, comp_path, source=source)
            for vc in comp["vendored_copies"]:
                vc_content_hash = _content_only_digest(repo_root, vc.rstrip("/"), source=source)
                entry.setdefault("vendored_digests", {})[vc] = vc_content_hash
                if vc_content_hash != source_content_hash:
                    entry.setdefault("warnings", []).append(
                        f"Vendored copy at {vc} differs from source (source={source_content_hash}, copy={vc_content_hash})"
                    )

        lockfile["components"][name] = entry

    # --- Slices ---
    for slice_name, slice_def in slices_config.items():
        mode = slice_def.get("mode", "exact")
        component_names = slice_def["components"]

        digest_parts: Dict[str, Optional[str]] = {}
        for cname in sorted(component_names):
            comp_entry = lockfile["components"].get(cname)
            if comp_entry is None:
                digest_parts[cname] = None
                continue
            fp = comp_entry["fingerprints"]
            if mode == "exact":
                digest_parts[cname] = fp.get("exact")
            elif mode == "boundary":
                if fp.get("boundary") is None and strict:
                    raise ValueError(f"Slice '{slice_name}' requires boundary digest for component '{cname}'")
                digest_parts[cname] = fp.get("boundary")
            elif mode == "compat":
                if fp.get("compat") is None and strict:
                    raise ValueError(f"Slice '{slice_name}' requires compat digest for component '{cname}'")
                digest_parts[cname] = fp.get("compat")
            else:
                raise ValueError(f"Unknown slice mode: {mode}")

        slice_hash = sha256_hex(canonical_json(digest_parts))

        lockfile["slices"][slice_name] = {
            "description": slice_def.get("description", ""),
            "mode": mode,
            "components": sorted(component_names),
            "fingerprint": slice_hash,
            "component_digests": digest_parts,
        }

    return lockfile


def _recompute_slice_entry(
    slice_name: str,
    slice_def: dict,
    components_map: Dict[str, dict],
    strict: bool = True,
) -> dict:
    mode = slice_def.get("mode", "exact")
    component_names = slice_def["components"]
    digest_parts: Dict[str, Optional[str]] = {}
    for cname in sorted(component_names):
        comp_entry = components_map.get(cname)
        if comp_entry is None:
            digest_parts[cname] = None
            continue
        fp = comp_entry.get("fingerprints", {})
        if mode == "exact":
            digest_parts[cname] = fp.get("exact")
        elif mode == "boundary":
            if fp.get("boundary") is None and strict:
                raise ValueError(f"Slice '{slice_name}' requires boundary digest for component '{cname}'")
            digest_parts[cname] = fp.get("boundary")
        elif mode == "compat":
            if fp.get("compat") is None and strict:
                raise ValueError(f"Slice '{slice_name}' requires compat digest for component '{cname}'")
            digest_parts[cname] = fp.get("compat")
        else:
            raise ValueError(f"Unknown slice mode: {mode}")
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
        raise ValueError(f"Unknown component(s): {', '.join(missing)}")

    subset_config = dict(config)
    subset_config["components"] = {n: components_cfg[n] for n in selected}
    subset_config["slices"] = {}
    subset_lock = generate_lockfile(
        subset_config, repo_root, source=source, strict=strict,
        allow_custom_providers=allow_custom_providers,
    )

    if out_path.exists():
        merged = json.loads(out_path.read_text())
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
    merged.setdefault("components", {})
    merged.setdefault("slices", {})

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
        for required in ("exact", "boundary", "compat"):
            if required not in fps:
                issues.append(f"LOCKFILE malformed: component '{name}' missing fingerprints.{required}")
    return issues


def verify_lockfile(
    config: dict,
    lockfile: dict,
    repo_root: Path,
    source: str = "head",
    components_filter: Optional[List[str]] = None,
    allow_custom_providers: bool = False,
) -> List[str]:
    """Check if the lockfile matches current repo state. Returns list of mismatches."""
    current = generate_lockfile(config, repo_root, source=source, allow_custom_providers=allow_custom_providers)
    issues = _lockfile_schema_issues(lockfile)
    issues.extend(_lockfile_structure_issues(lockfile))
    if issues:
        return issues

    selected = set(components_filter or [])
    use_filter = len(selected) > 0

    for name, current_comp in current["components"].items():
        if use_filter and name not in selected:
            continue
        locked_comp = lockfile.get("components", {}).get(name)
        if locked_comp is None:
            issues.append(f"NEW component not in lockfile: {name}")
            continue
        for facet in ("exact", "boundary", "compat"):
            cv = current_comp["fingerprints"].get(facet)
            locked_fps = locked_comp.get("fingerprints", {})
            lv = locked_fps.get(facet)
            if cv != lv:
                issues.append(
                    f"MISMATCH {name}.{facet}: lockfile={_short(lv)} current={_short(cv)}"
                )

    for name in lockfile.get("components", {}):
        if use_filter and name not in selected:
            continue
        if name not in current["components"]:
            issues.append(f"REMOVED component still in lockfile: {name}")

    # Check for vendored copy drift
    for name, comp in current["components"].items():
        if use_filter and name not in selected:
            continue
        for warning in comp.get("warnings", []):
            issues.append(f"VENDORED DRIFT {name}: {warning}")

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

