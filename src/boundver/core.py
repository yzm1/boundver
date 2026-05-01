#!/usr/bin/env python3
"""
boundary-lock: Semantic version manifest with faceted fingerprints.

Generates a lockfile where each component has:
  - exact fingerprint  (did the implementation change?)
  - api fingerprint    (did the public boundary change?)
  - compat fingerprint (did the compatibility family change?)

Slices group components and produce their own stable fingerprints.
Adding an unrelated component does NOT change existing slice fingerprints.

Usage:
    boundver generate [--config boundary.config.json] [--out boundary.lock.json]
    boundver verify  [--config boundary.config.json] [--lock boundary.lock.json]
    boundver diff    <old.lock.json> <new.lock.json>
    boundver slice   <slice_name> [--config boundary.config.json] [--lock boundary.lock.json]
    boundver validate-config [--config boundary.config.json]
    boundver status  [--config boundary.config.json] [--lock boundary.lock.json]

Requires: git (for tree hashes), Python 3.8+
No external dependencies.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Canonical JSON (RFC 8785 subset: sorted keys, no whitespace, UTF-8)
# ---------------------------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """Deterministic JSON for hashing. Sorted keys, compact separators."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def boundary_provider_name(boundary: dict) -> str:
    """Return boundary provider name."""
    return boundary.get("provider") or "unknown"


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def git_root() -> Path:
    """Find the repository root."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip())


def _git_run(repo_root: Path, args: List[str]) -> subprocess.CompletedProcess:
    """Run git against a specific repository root regardless of process CWD."""
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True, text=True, check=True,
    )


def git_tree_hash(repo_root: Path, path: str, source: str = "head") -> Optional[str]:
    """Get a canonical SHA-256 digest for a path from HEAD, index, or working tree."""
    target = repo_root / path
    content_parts: List[str] = []

    if source == "head":
        files = _head_files_for_path(repo_root, path)
        for rel in files:
            content_parts.append(f"file:{rel}\n")
            content_parts.append(_read_path_content(repo_root, repo_root / rel, source))
        return sha256_hex("".join(content_parts)) if content_parts else None

    if not target.exists():
        return None
    if target.is_file():
        files = [str(target.relative_to(repo_root))]
    else:
        files = [
            str(f.relative_to(repo_root))
            for f in sorted(target.rglob("*"))
            if f.is_file() and not _is_ignored(f)
        ]

    for rel in files:
        content_parts.append(f"file:{rel}\n")
        content_parts.append(_read_path_content(repo_root, repo_root / rel, source))
    return sha256_hex("".join(content_parts)) if content_parts else None


def _head_files_for_path(repo_root: Path, path: str) -> List[str]:
    """List files at a repo-relative path as represented in HEAD."""
    try:
        result = _git_run(repo_root, ["ls-tree", "-r", "--name-only", "HEAD", path])
    except subprocess.CalledProcessError:
        return []

    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if files:
        return files

    try:
        result = _git_run(repo_root, ["cat-file", "-t", f"HEAD:{path}"])
    except subprocess.CalledProcessError:
        return []
    return [path] if result.stdout.strip() == "blob" else []


def git_hash_files(repo_root: Path, component_path: str, relative_paths: List[str], source: str = "working-tree") -> Optional[str]:
    """
    Hash the contents of specific files/directories within a component.
    Used for boundary (API) fingerprints where only certain paths matter.
    """
    content_parts = []
    for rel in sorted(relative_paths):
        full = repo_root / component_path / rel
        if full.is_file():
            content_parts.append(f"file:{rel}\n")
            content_parts.append(_read_path_content(repo_root, full, source))
        elif full.is_dir():
            for child in sorted(full.rglob("*")):
                if child.is_file() and not _is_ignored(child):
                    child_rel = str(child.relative_to(repo_root / component_path))
                    content_parts.append(f"file:{child_rel}\n")
                    content_parts.append(_read_path_content(repo_root, child, source))
    if not content_parts:
        return None
    return sha256_hex("".join(content_parts))


def _read_path_content(repo_root: Path, full_path: Path, source: str) -> str:
    rel = str(full_path.relative_to(repo_root))
    if source == "index":
        try:
            result = _git_run(repo_root, ["show", f":{rel}"])
            return result.stdout
        except subprocess.CalledProcessError:
            return full_path.read_text(errors="replace")
    if source == "head":
        try:
            result = _git_run(repo_root, ["show", f"HEAD:{rel}"])
            return result.stdout
        except subprocess.CalledProcessError:
            return full_path.read_text(errors="replace")
    return full_path.read_text(errors="replace")


def git_latest_tag(prefix: str) -> Optional[str]:
    """Find the latest git tag matching a prefix, extract the version part."""
    try:
        result = subprocess.run(
            ["git", "tag", "--list", f"{prefix}*", "--sort=-v:refname"],
            capture_output=True, text=True, check=True,
        )
        tags = result.stdout.strip().split("\n")
        if tags and tags[0]:
            return tags[0][len(prefix):]
        return None
    except subprocess.CalledProcessError:
        return None


def _is_ignored(path: Path) -> bool:
    name = path.name
    return (
        name.startswith(".")
        or name == "__pycache__"
        or name == "node_modules"
        or name.endswith(".pyc")
        or name == "dist"
        or name == "build"
    )


# ---------------------------------------------------------------------------
# Version extraction
# ---------------------------------------------------------------------------

def extract_version(repo_root: Path, component_path: str, version_source: Optional[dict]) -> Optional[str]:
    """Extract the current version string from whatever source is configured."""
    if version_source is None:
        return None

    if "git_tag_prefix" in version_source:
        return git_latest_tag(version_source["git_tag_prefix"])

    file_rel = version_source.get("file")
    field_path = version_source.get("field")
    if not file_rel or not field_path:
        return None

    full_path = repo_root / component_path / file_rel
    if not full_path.exists():
        return None

    if file_rel.endswith(".json"):
        return _extract_json_field(full_path, field_path)
    elif file_rel.endswith(".toml"):
        return _extract_toml_field(full_path, field_path)
    elif file_rel.endswith(".yaml") or file_rel.endswith(".yml"):
        return _extract_yaml_field(full_path, field_path)
    return None


def _extract_json_field(path: Path, field_path: str) -> Optional[str]:
    try:
        data = json.loads(path.read_text())
        for key in field_path.split("."):
            data = data[key]
        return str(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _extract_toml_field(path: Path, field_path: str) -> Optional[str]:
    """Minimal TOML field extraction — no external deps.
    Handles [section] headers and key = "value" lines.
    Supports dotted field paths like 'project.version'.
    """
    text = path.read_text()
    keys = field_path.split(".")
    current_section = ""

    if len(keys) == 2:
        target_section = keys[0]
        target_key = keys[1]
    elif len(keys) == 1:
        target_section = ""
        target_key = keys[0]
    else:
        return None

    for line in text.splitlines():
        line = line.strip()
        section_match = re.match(r"^\[([^\]]+)\]", line)
        if section_match:
            current_section = section_match.group(1).strip()
            continue
        if current_section == target_section:
            kv_match = re.match(r'^(\w[\w-]*)\s*=\s*"([^"]*)"', line)
            if kv_match and kv_match.group(1) == target_key:
                return kv_match.group(2)
    return None


def _extract_yaml_field(path: Path, field_path: str) -> Optional[str]:
    """Minimal YAML field extraction for simple top-level dotted paths.
    Handles 'info.version' style paths in OpenAPI specs.
    """
    text = path.read_text()
    keys = field_path.split(".")
    indent_stack = []
    current_path: List[str] = []

    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        # Pop path elements when indent decreases
        while indent_stack and indent <= indent_stack[-1]:
            indent_stack.pop()
            if current_path:
                current_path.pop()

        kv_match = re.match(r"^([\w.-]+)\s*:\s*(.+)$", stripped)
        if kv_match:
            key = kv_match.group(1)
            value = kv_match.group(2).strip().strip("'\"")
            test_path = current_path + [key]
            if test_path == keys:
                return value
        else:
            section_match = re.match(r"^([\w.-]+)\s*:\s*$", stripped)
            if section_match:
                current_path.append(section_match.group(1))
                indent_stack.append(indent)

    return None


# ---------------------------------------------------------------------------
# SemVer parsing
# ---------------------------------------------------------------------------

def parse_semver(version: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Parse a semver string into (major, major.minor, full).
    Returns (compat_family, api_surface, exact_version).
    """
    if not version:
        return (None, None, None)
    match = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?", version)
    if not match:
        return (None, None, version)
    major = match.group(1)
    minor = match.group(2)
    patch = match.group(3) or "0"
    return (major, f"{major}.{minor}", f"{major}.{minor}.{patch}")


# ---------------------------------------------------------------------------
# Lockfile generation
# ---------------------------------------------------------------------------

def generate_lockfile(config: dict, repo_root: Path, source: str = "head", strict: bool = True) -> dict:
    """Generate the full lockfile from config + repo state."""
    components_config = config["components"]
    slices_config = config.get("slices", {})
    defaults = config.get("defaults", {})

    lockfile = {
        "schema": "boundary-lock/v1",
        "project": config.get("project", "unknown"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "components": {},
        "slices": {},
    }

    # --- Components ---
    for name, comp in components_config.items():
        comp_path = comp["path"]
        version = extract_version(repo_root, comp_path, comp.get("version_source"))
        compat, api_ver, exact_ver = parse_semver(version)
        compat_mode = defaults.get("compat_mode", "major")

        # Exact fingerprint: git tree hash of the whole component directory
        exact_digest = git_tree_hash(repo_root, comp_path, source=source)

        # API fingerprint: hash only the boundary paths (if any)
        boundary = comp.get("boundary", {})
        boundary_paths = boundary.get("paths", [])
        api_digest = None
        boundary_status = "ok"
        boundary_errors: List[str] = []
        boundary_provider = boundary_provider_name(boundary)
        if boundary_paths:
            api_digest = git_hash_files(repo_root, comp_path, boundary_paths, source=source)
            if api_digest is None:
                boundary_status = "error"
                boundary_errors.append("Declared boundary paths produced no digest")
        else:
            if boundary_provider == "implicit":
                boundary_status = "partial"
                boundary_errors.append("No boundary paths declared for implicit boundary")
            elif boundary_provider == "leaf":
                boundary_status = "ok"
            else:
                boundary_status = "error"
                boundary_errors.append("No boundary paths declared for explicit boundary kind")

        # Compatibility fingerprint: derived from semver major (or major.minor)
        compat_digest = None
        compat_identity = None
        if compat_mode in {"major", "semver_major"}:
            compat_identity = compat
        elif compat_mode == "semver_major_minor":
            compat_identity = api_ver

        if compat_identity is not None:
            compat_digest = sha256_hex(f"{name}@compat:{compat_identity}")

        entry = {
            "version": version,
            "path": comp_path,
            "boundary_provider": boundary_provider,
            "boundary_status": boundary_status,
            "fingerprints": {
                "exact": exact_digest,
                "api": api_digest,
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

        # Flag vendored copies
        if "vendored_copies" in comp:
            entry["vendored_copies"] = comp["vendored_copies"]
            # Check if vendored copies are in sync
            for vc in comp["vendored_copies"]:
                vc_hash = git_tree_hash(repo_root, vc.rstrip("/"), source=source)
                entry.setdefault("vendored_digests", {})[vc] = vc_hash
                if vc_hash != exact_digest:
                    entry.setdefault("warnings", []).append(
                        f"Vendored copy at {vc} differs from source (source={exact_digest}, copy={vc_hash})"
                    )

        lockfile["components"][name] = entry

    # --- Slices ---
    for slice_name, slice_def in slices_config.items():
        mode = slice_def.get("mode", "exact")  # exact | api | compat
        component_names = slice_def["components"]

        # Collect the relevant digest for each component in the slice
        digest_parts = {}
        for cname in sorted(component_names):
            comp_entry = lockfile["components"].get(cname)
            if comp_entry is None:
                digest_parts[cname] = None
                continue
            fp = comp_entry["fingerprints"]
            if mode == "exact":
                digest_parts[cname] = fp.get("exact")
            elif mode == "api":
                if fp.get("api") is None and strict:
                    raise ValueError(f"Slice '{slice_name}' requires api digest for component '{cname}'")
                digest_parts[cname] = fp.get("api")
            elif mode == "compat":
                if fp.get("compat") is None and strict:
                    raise ValueError(f"Slice '{slice_name}' requires compat digest for component '{cname}'")
                digest_parts[cname] = fp.get("compat")
            else:
                raise ValueError(f"Unknown slice mode: {mode}")

        # Slice hash = hash of canonical JSON of {component: digest} for selected components
        slice_hash = sha256_hex(canonical_json(digest_parts))

        lockfile["slices"][slice_name] = {
            "description": slice_def.get("description", ""),
            "mode": mode,
            "components": sorted(component_names),
            "fingerprint": slice_hash,
            "component_digests": digest_parts,
        }

    return lockfile


def validate_config(config: dict, repo_root: Path) -> List[str]:
    errors: List[str] = []
    if not isinstance(config, dict):
        return ["Config root must be a JSON object"]

    for required_key in ("project", "components", "slices"):
        if required_key not in config:
            errors.append(f"Missing required top-level field: {required_key}")

    supported_modes = {"exact", "api", "compat"}
    compat_mode = config.get("defaults", {}).get("compat_mode", "major")
    if compat_mode not in {"major", "semver_major", "semver_major_minor"}:
        errors.append(f"Unsupported defaults.compat_mode: {compat_mode}")

    components = config.get("components", {})
    slices = config.get("slices", {})
    if not isinstance(components, dict):
        errors.append("Field 'components' must be an object")
        components = {}
    if not isinstance(slices, dict):
        errors.append("Field 'slices' must be an object")
        slices = {}

    for name, comp in components.items():
        if "path" not in comp:
            errors.append(f"Component '{name}' missing required field: path")
            continue
        boundary = comp.get("boundary", {})
        if not isinstance(boundary, dict):
            errors.append(f"Component '{name}' boundary must be an object")
            continue
        if "kind" in boundary:
            errors.append(
                f"Component '{name}' uses legacy boundary.kind; use boundary.provider instead"
            )
        if "provider" not in boundary:
            errors.append(f"Component '{name}' missing required field: boundary.provider")
        kind = boundary_provider_name(boundary)
        paths = boundary.get("paths", [])
        if kind == "service-definition" and not paths:
            errors.append(f"Component '{name}' has service-definition boundary with empty paths")
        for rel in paths:
            full = repo_root / comp["path"] / rel
            if not full.exists():
                errors.append(f"Component '{name}' boundary path missing: {rel}")

    for sname, sdef in slices.items():
        mode = sdef.get("mode", "exact")
        if mode not in supported_modes:
            errors.append(f"Slice '{sname}' has unknown mode: {mode}")
        for cname in sdef.get("components", []):
            if cname not in components:
                errors.append(f"Slice '{sname}' references unknown component: {cname}")
                continue
            if mode == "api":
                kind = boundary_provider_name(components[cname].get("boundary", {}))
                paths = components[cname].get("boundary", {}).get("paths", [])
                if kind in {"leaf", "implicit"}:
                    errors.append(f"Slice '{sname}' in api mode cannot include '{cname}' ({kind})")
                if not paths:
                    errors.append(f"Slice '{sname}' in api mode includes '{cname}' with no boundary paths")

    return errors


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def diff_lockfiles(old: dict, new: dict) -> dict:
    """Produce a human-readable diff between two lockfiles."""
    result = {
        "components": {"added": [], "removed": [], "changed": [], "unchanged": []},
        "slices": {"changed": [], "unchanged": []},
    }

    old_comps = old.get("components", {})
    new_comps = new.get("components", {})

    all_names = sorted(set(old_comps.keys()) | set(new_comps.keys()))
    for name in all_names:
        if name not in old_comps:
            result["components"]["added"].append({
                "name": name,
                "version": new_comps[name].get("version"),
            })
        elif name not in new_comps:
            result["components"]["removed"].append({
                "name": name,
                "version": old_comps[name].get("version"),
            })
        else:
            old_fp = old_comps[name].get("fingerprints", {})
            new_fp = new_comps[name].get("fingerprints", {})
            changes = {}
            for facet in ("exact", "api", "compat"):
                ov = old_fp.get(facet)
                nv = new_fp.get(facet)
                if ov != nv:
                    changes[facet] = {"old": ov, "new": nv}
            if changes:
                entry = {
                    "name": name,
                    "old_version": old_comps[name].get("version"),
                    "new_version": new_comps[name].get("version"),
                    "changed_facets": changes,
                }
                # Summarize which levels changed
                entry["summary"] = _summarize_change(changes)
                result["components"]["changed"].append(entry)
            else:
                result["components"]["unchanged"].append(name)

    # Slice diffs
    old_slices = old.get("slices", {})
    new_slices = new.get("slices", {})
    for sname in sorted(set(old_slices.keys()) | set(new_slices.keys())):
        old_fp = old_slices.get(sname, {}).get("fingerprint")
        new_fp = new_slices.get(sname, {}).get("fingerprint")
        if old_fp != new_fp:
            result["slices"]["changed"].append({
                "name": sname,
                "old": old_fp,
                "new": new_fp,
            })
        else:
            result["slices"]["unchanged"].append(sname)

    return result


def _summarize_change(changes: dict) -> str:
    facets = list(changes.keys())
    if facets == ["exact"]:
        return "implementation-only change (API stable)"
    elif "api" in facets and "compat" not in facets:
        return "API surface changed (compatible)"
    elif "compat" in facets:
        return "BREAKING: compatibility family changed"
    return "changed: " + ", ".join(facets)


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def verify_lockfile(config: dict, lockfile: dict, repo_root: Path, source: str = "head") -> List[str]:
    """Check if the lockfile matches current repo state. Returns list of mismatches."""
    current = generate_lockfile(config, repo_root, source=source)
    issues = []

    for name, current_comp in current["components"].items():
        locked_comp = lockfile.get("components", {}).get(name)
        if locked_comp is None:
            issues.append(f"NEW component not in lockfile: {name}")
            continue
        for facet in ("exact", "api", "compat"):
            cv = current_comp["fingerprints"].get(facet)
            lv = locked_comp["fingerprints"].get(facet)
            if cv != lv:
                issues.append(
                    f"MISMATCH {name}.{facet}: lockfile={_short(lv)} current={_short(cv)}"
                )

    for name in lockfile.get("components", {}):
        if name not in current["components"]:
            issues.append(f"REMOVED component still in lockfile: {name}")

    # Check for vendored copy drift
    for name, comp in current["components"].items():
        for warning in comp.get("warnings", []):
            issues.append(f"VENDORED DRIFT {name}: {warning}")

    return issues


def _short(h: Optional[str]) -> str:
    if h is None:
        return "none"
    return h[:12] + "..."


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

def print_diff(diff: dict) -> None:
    comps = diff["components"]

    if comps["added"]:
        print("\n  ADDED:")
        for c in comps["added"]:
            print(f"    + {c['name']} @ {c.get('version', 'unversioned')}")

    if comps["removed"]:
        print("\n  REMOVED:")
        for c in comps["removed"]:
            print(f"    - {c['name']} @ {c.get('version', 'unversioned')}")

    if comps["changed"]:
        print("\n  CHANGED:")
        for c in comps["changed"]:
            ver_str = ""
            if c["old_version"] != c["new_version"]:
                ver_str = f" ({c['old_version']} -> {c['new_version']})"
            print(f"    ~ {c['name']}{ver_str}")
            print(f"      {c['summary']}")
            for facet, vals in c["changed_facets"].items():
                print(f"      {facet}: {_short(vals['old'])} -> {_short(vals['new'])}")

    if comps["unchanged"]:
        print(f"\n  UNCHANGED: {len(comps['unchanged'])} components")

    slices = diff["slices"]
    if slices["changed"]:
        print("\n  SLICES CHANGED:")
        for s in slices["changed"]:
            print(f"    ~ {s['name']}: {_short(s['old'])} -> {_short(s['new'])}")
    if slices["unchanged"]:
        print(f"\n  SLICES UNCHANGED: {', '.join(slices['unchanged'])}")


def print_status(lockfile: dict) -> None:
    """Print a summary of the current lockfile state."""
    comps = lockfile.get("components", {})
    slices = lockfile.get("slices", {})

    print(f"\n  Project: {lockfile.get('project', '?')}")
    print(f"  Generated: {lockfile.get('generated_at', '?')}")
    print(f"  Components: {len(comps)}")
    print(f"  Slices: {len(slices)}")

    # Version coverage
    versioned = sum(1 for c in comps.values() if c.get("version"))
    unversioned = len(comps) - versioned
    print(f"\n  Versioned: {versioned}  |  Unversioned: {unversioned}")

    # Boundary coverage
    boundary_kinds: Dict[str, int] = {}
    boundary_states: Dict[str, int] = {}
    for c in comps.values():
        kind = c.get("boundary_provider", "unknown")
        boundary_kinds[kind] = boundary_kinds.get(kind, 0) + 1
        state = c.get("boundary_status", "unknown")
        boundary_states[state] = boundary_states.get(state, 0) + 1
    print("\n  Boundary coverage:")
    for kind, count in sorted(boundary_kinds.items()):
        print(f"    {kind}: {count}")
    print("\n  Boundary extraction status:")
    for state, count in sorted(boundary_states.items()):
        print(f"    {state}: {count}")

    # Warnings
    warnings = []
    for name, c in comps.items():
        for w in c.get("warnings", []):
            warnings.append(f"    {name}: {w}")
        for e in c.get("boundary_errors", []):
            warnings.append(f"    {name}: boundary {c.get('boundary_status', 'unknown')} - {e}")
    if warnings:
        print(f"\n  WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(w)

    # Slices
    print("\n  Slices:")
    for sname, sdata in slices.items():
        fp = _short(sdata.get("fingerprint"))
        mode = sdata.get("mode", "exact")
        count = len(sdata.get("components", []))
        print(f"    {sname} [{mode}] ({count} components) = {fp}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="boundary-lock: semantic version manifest with faceted fingerprints",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    # generate
    gen = sub.add_parser("generate", help="Generate or update the lockfile")
    gen.add_argument("--config", default="boundary.config.json", help="Config file path")
    gen.add_argument("--out", default="boundary.lock.json", help="Output lockfile path")
    gen.add_argument("--source", choices=["head", "index", "working-tree"], default="head", help="Fingerprint source")
    gen.add_argument("--allow-partial", action="store_true", help="Allow missing api/compat digests in slices")

    # verify
    ver = sub.add_parser("verify", help="Check lockfile matches current repo state")
    ver.add_argument("--config", default="boundary.config.json")
    ver.add_argument("--lock", default="boundary.lock.json")
    ver.add_argument("--source", choices=["head", "index", "working-tree"], default="head")

    # diff
    dif = sub.add_parser("diff", help="Diff two lockfiles")
    dif.add_argument("old", help="Old lockfile")
    dif.add_argument("new", help="New lockfile")

    # slice
    sl = sub.add_parser("slice", help="Show fingerprint for a specific slice")
    sl.add_argument("name", help="Slice name")
    sl.add_argument("--lock", default="boundary.lock.json")

    # validate-config
    vc = sub.add_parser("validate-config", help="Validate config for strict boundary rules")
    vc.add_argument("--config", default="boundary.config.json")

    cc = sub.add_parser("check-config", help="Alias for validate-config")
    cc.add_argument("--config", default="boundary.config.json")

    # status
    st = sub.add_parser("status", help="Show lockfile summary and warnings")
    st.add_argument("--config", default="boundary.config.json")
    st.add_argument("--lock", default="boundary.lock.json")
    st.add_argument("--source", choices=["head", "index", "working-tree"], default="head")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    try:
        repo_root = git_root()
    except subprocess.CalledProcessError:
        print("ERROR: Not inside a git repository.", file=sys.stderr)
        sys.exit(1)

    if args.command == "generate":
        config_path = repo_root / args.config
        if not config_path.exists():
            print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
            sys.exit(1)
        config = json.loads(config_path.read_text())
        lockfile = generate_lockfile(config, repo_root, source=args.source, strict=(not args.allow_partial))
        out_path = repo_root / args.out
        out_path.write_text(json.dumps(lockfile, indent=2) + "\n")
        print(f"Generated {out_path}")
        print_status(lockfile)

    elif args.command == "verify":
        config = json.loads((repo_root / args.config).read_text())
        lockfile = json.loads((repo_root / args.lock).read_text())
        issues = verify_lockfile(config, lockfile, repo_root, source=args.source)
        if issues:
            print(f"LOCKFILE OUT OF DATE ({len(issues)} issues):\n")
            for issue in issues:
                print(f"  {issue}")
            sys.exit(1)
        else:
            print("Lockfile is up to date.")

    elif args.command == "diff":
        old = json.loads(Path(args.old).read_text())
        new = json.loads(Path(args.new).read_text())
        result = diff_lockfiles(old, new)
        print_diff(result)

    elif args.command == "slice":
        lockfile = json.loads((repo_root / args.lock).read_text())
        sl = lockfile.get("slices", {}).get(args.name)
        if sl is None:
            print(f"ERROR: Slice '{args.name}' not found.", file=sys.stderr)
            print(f"Available: {', '.join(lockfile.get('slices', {}).keys())}")
            sys.exit(1)
        print(f"\n  Slice: {args.name}")
        print(f"  Mode: {sl.get('mode', 'exact')}")
        print(f"  Fingerprint: {sl['fingerprint']}")
        print(f"  Components:")
        for cname in sl.get("components", []):
            digest = sl.get("component_digests", {}).get(cname)
            comp = lockfile.get("components", {}).get(cname, {})
            ver = comp.get("version", "unversioned")
            print(f"    {cname} @ {ver}  ({_short(digest)})")

    elif args.command in {"validate-config", "check-config"}:
        config_path = repo_root / args.config
        if not config_path.exists():
            print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
            sys.exit(1)
        config = json.loads(config_path.read_text())
        errors = validate_config(config, repo_root)
        if errors:
            print(f"CONFIG INVALID ({len(errors)} issues):")
            for err in errors:
                print(f"  - {err}")
            sys.exit(1)
        print("Config is valid.")

    elif args.command == "status":
        lock_path = repo_root / args.lock
        if lock_path.exists():
            lockfile = json.loads(lock_path.read_text())
            print_status(lockfile)
            # Also verify if config exists
            config_path = repo_root / args.config
            if config_path.exists():
                config = json.loads(config_path.read_text())
                issues = verify_lockfile(config, lockfile, repo_root, source=args.source)
                if issues:
                    print(f"\n  DRIFT DETECTED ({len(issues)} issues):")
                    for issue in issues:
                        print(f"    {issue}")
        else:
            print(f"No lockfile found at {lock_path}. Run 'generate' first.")
            sys.exit(1)


if __name__ == "__main__":
    main()
