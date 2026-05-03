"""Output / pretty-printing helpers for boundver."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._git import _git_run, _to_posix
from ._utils import _is_glob, _short, boundary_provider_name


# ---------------------------------------------------------------------------
# TTY color support (only active when stdout is an interactive terminal)
# ---------------------------------------------------------------------------

def _is_tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _is_tty() else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _is_tty() else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _is_tty() else s


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m" if _is_tty() else s


def print_diff(diff: dict) -> None:
    comps = diff["components"]

    if comps["added"]:
        print("\n  ADDED:")
        for c in comps["added"]:
            print(_green(f"    + {c['name']} @ {c.get('version', 'unversioned')}"))

    if comps["removed"]:
        print("\n  REMOVED:")
        for c in comps["removed"]:
            print(_red(f"    - {c['name']} @ {c.get('version', 'unversioned')}"))

    if comps["changed"]:
        print("\n  CHANGED:")
        for c in comps["changed"]:
            ver_str = ""
            if c["old_version"] != c["new_version"]:
                ver_str = f" ({c['old_version']} -> {c['new_version']})"
            print(_yellow(f"    ~ {c['name']}{ver_str}"))
            print(f"      {c['summary']}")
            for facet, vals in c["changed_facets"].items():
                print(f"      {facet}: {_short(vals['old'])} -> {_short(vals['new'])}")

    if comps["unchanged"]:
        print(f"\n  UNCHANGED: {len(comps['unchanged'])} components")

    slices = diff["slices"]
    if slices.get("added"):
        print("\n  SLICES ADDED:")
        for s in slices["added"]:
            print(_green(f"    + {s['name']}"))
    if slices.get("removed"):
        print("\n  SLICES REMOVED:")
        for s in slices["removed"]:
            print(_red(f"    - {s['name']}"))
    if slices["changed"]:
        print("\n  SLICES CHANGED:")
        for s in slices["changed"]:
            print(_yellow(f"    ~ {s['name']}: {_short(s['old'])} -> {_short(s['new'])}"))
    if slices["unchanged"]:
        print(f"\n  SLICES UNCHANGED: {', '.join(slices['unchanged'])}")


def print_status(lockfile: dict) -> None:
    """Print a summary of the current lockfile state."""
    comps = lockfile.get("components", {})
    slices = lockfile.get("slices", {})

    print(f"\n  Project: {lockfile.get('project', '?')}")
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

    # Explain partial status for implicit provider (common for new adopters)
    implicit_partial = [
        name for name, c in comps.items()
        if c.get("boundary_status") == "partial" and c.get("boundary_provider") == "implicit"
    ]
    if implicit_partial:
        print(f"\n  Note: {len(implicit_partial)} component(s) use the 'implicit' provider (boundary fingerprint = null).")
        print("    This is expected — implicit tracks exact changes only.")
        print("    To track API boundaries, switch to a specific provider:")
        print("      boundver add <name> <path> --provider openapi")

    # Warnings
    warnings = []
    for name, c in comps.items():
        for w in c.get("warnings", []):
            warnings.append(f"    {name}: {w}")
        for e in c.get("boundary_errors", []):
            warnings.append(f"    {name}: boundary {c.get('boundary_status', 'unknown')} - {e}")
    if warnings:
        print(_yellow(f"\n  WARNINGS ({len(warnings)}):"))
        for w in warnings:
            print(_yellow(w))

    # Slices
    print("\n  Slices:")
    for sname, sdata in slices.items():
        fp = _short(sdata.get("fingerprint"))
        mode = sdata.get("mode", "exact")
        count = len(sdata.get("components", []))
        print(f"    {sname} [{mode}] ({count} components) = {fp}")


def analyze_explain_changes(
    config: dict, repo_root: Path, component_name: str, base_ref: str = "HEAD", source: str = "head"
) -> Dict[str, Any]:
    """Analyze changed tracked files for one component and its boundary subset.

    Returns a dict with keys:
        error (str|None), component_name, component_path, effective_base,
        source, changed (list of (status, path) tuples),
        boundary_provider, boundary_paths, boundary_changed (list of (status, path) tuples).
    """
    if base_ref.lstrip().startswith("-"):
        return {"error": f"invalid base ref: {base_ref!r}"}
    comp = config.get("components", {}).get(component_name)
    if not comp:
        known = sorted(config.get("components", {}).keys())
        return {"error": f"unknown component '{component_name}'", "known": known}

    component_path = str(comp.get("path", "")).rstrip("/")
    boundary = comp.get("boundary", {})
    boundary_paths_raw = boundary.get("paths", []) if isinstance(boundary, dict) else []

    # When source=head and base_ref=HEAD, diffing HEAD vs HEAD is useless.
    effective_base = base_ref
    if source == "head" and base_ref == "HEAD":
        try:
            _git_run(repo_root, ["rev-parse", "--verify", "HEAD~1"])
            effective_base = "HEAD~1"
        except subprocess.CalledProcessError:
            pass

    # Choose diff target based on source
    diff_args = ["diff", "--name-status"]
    if source == "working-tree":
        diff_args.append(effective_base)
    elif source == "index":
        diff_args.extend(["--cached", effective_base])
    else:
        diff_args.extend([effective_base, "HEAD"])
    diff_args.extend(["--", component_path])

    try:
        diff = _git_run(repo_root, diff_args)
    except subprocess.CalledProcessError as exc:
        return {"error": f"failed to diff '{component_name}' against {effective_base}: {exc}"}

    changed: List[Tuple[str, str]] = []
    for line in diff.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        filepath = parts[-1]
        changed.append((parts[0].strip(), _to_posix(filepath.strip())))

    component_prefix = f"{_to_posix(component_path)}/"
    normalized_boundary_paths: List[str] = []
    for p in boundary_paths_raw:
        rp = _to_posix(str(p).strip().rstrip("/"))
        if rp:
            normalized_boundary_paths.append(rp)

    boundary_changed: List[Tuple[str, str]] = []
    for status, rel in changed:
        component_relative = rel
        if component_relative.startswith(component_prefix):
            component_relative = component_relative[len(component_prefix):]
        for bp in normalized_boundary_paths:
            if _is_glob(bp):
                import fnmatch
                if fnmatch.fnmatch(component_relative, bp) or fnmatch.fnmatch(component_relative, f"{bp}/*"):
                    boundary_changed.append((status, rel))
                    break
            elif component_relative == bp or component_relative.startswith(f"{bp}/"):
                boundary_changed.append((status, rel))
                break

    return {
        "error": None,
        "component_name": component_name,
        "component_path": component_path,
        "effective_base": effective_base,
        "base_ref": base_ref,
        "source": source,
        "changed": changed,
        "boundary_provider": boundary_provider_name(boundary),
        "boundary_paths": normalized_boundary_paths,
        "boundary_changed": boundary_changed,
    }


def explain_component_changes(config: dict, repo_root: Path, component_name: str, base_ref: str = "HEAD", source: str = "head") -> int:
    """Explain changed tracked files for one component and its boundary subset."""
    result = analyze_explain_changes(config, repo_root, component_name, base_ref, source)

    if result.get("error"):
        print(f"ERROR: {result['error']}", file=sys.stderr)
        if result.get("known"):
            print(f"Known components: {', '.join(result['known'])}", file=sys.stderr)
        return 2

    component_path = result["component_path"]
    effective_base = result["effective_base"]
    base_ref_actual = result["base_ref"]
    changed = result["changed"]
    boundary_paths = result["boundary_paths"]
    boundary_changed = result["boundary_changed"]

    print(f"Component: {component_name}")
    print(f"Path: {component_path}")
    print(f"Base ref: {effective_base}" + (f" (auto-resolved from {base_ref_actual})" if effective_base != base_ref_actual else ""))
    print(f"Source: {source}")

    if not changed:
        print("\nNo tracked file changes detected for this component path.")
        return 0

    print(f"\nChanged files ({len(changed)}):")
    for status, rel in changed:
        print(f"  {status:>2}  {rel}")

    if not boundary_paths:
        print("\nBoundary paths: none declared")
        return 0

    print(f"\nBoundary provider: {result['boundary_provider']}")
    print("Boundary paths:")
    for bp in boundary_paths:
        print(f"  - {bp}")

    if boundary_changed:
        print(f"\nBoundary-relevant changed files ({len(boundary_changed)}):")
        for status, rel in boundary_changed:
            print(f"  {status:>2}  {rel}")
    else:
        print("\nBoundary-relevant changed files: none")

    return 0


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg)


def _parse_components_arg(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return sorted(set(names))


def why_component(
    config: dict,
    lockfile: dict,
    repo_root: Path,
    component_name: str,
    source: str = "head",
    allow_custom_providers: bool = False,
) -> int:
    """Explain why a component's lockfile entry is out of date.

    Compares current fingerprints against the locked values and shows:
    - Which facets (exact/behavior/boundary/compat) changed and how
    - What type of change it is (impl-only, behavioral, boundary, breaking)
    - Files that changed under the component path (git diff against HEAD)

    Returns 0 if no drift, 1 if drift found, 2 on usage/config error.
    """
    result = analyze_component_drift(
        config, lockfile, repo_root, component_name,
        source=source, allow_custom_providers=allow_custom_providers,
    )
    if result is None:
        return 2  # error already printed by analyze_component_drift

    # Format output
    comp_cfg = config["components"][component_name]
    comp_path = comp_cfg.get("path", "?")
    print(f"\nComponent:  {_bold(component_name)}")
    print(f"Path:       {comp_path}")
    print(f"Source:     {source}")
    if result["version"]:
        print(f"Version:    {result['version']}")

    if not result["changes"]:
        print(_green("\nStatus: UP TO DATE — no fingerprint drift detected."))
        return 0

    changes = result["changes"]
    print(_red(f"\nStatus: DRIFTED — {len(changes)} facet(s) changed"))

    print("\nFingerprint changes:")
    for facet in ("exact", "behavior", "boundary", "compat"):
        lv = result["locked_fps"].get(facet)
        cv = result["current_fps"].get(facet)
        if facet in changes:
            print(f"  {facet:<10}  {_short(lv)}  →  {_red(_short(cv))}  (changed)")
        else:
            print(f"  {facet:<10}  {_short(lv)}  →  {_short(cv)}  (unchanged)")

    print(f"\n{_yellow('Change type:')}  {result['summary']}")

    if result["changed_files"]:
        print(f"\nModified files under {comp_path}:")
        seen: set = set()
        for status, rel in result["changed_files"]:
            if rel not in seen:
                seen.add(rel)
                print(f"  {status:>2}  {rel}")
    elif source == "index":
        print(f"\nNo staged changes under {comp_path}.")
        print(f"Drift is from changes staged after the lockfile was last generated.")
        print(f"  Tip: run `git diff --cached --name-only -- {comp_path}` to check staged files.")
    else:
        print(f"\nNo uncommitted changes under {comp_path}.")
        print(f"Drift is from commits made after the lockfile was last generated.")
        print(f"  Tip: run `git log --oneline -- {comp_path}` to find the relevant commits.")

    boundary_paths = comp_cfg.get("boundary", {}).get("paths", [])
    if boundary_paths and "boundary" in changes:
        print(f"\nBoundary paths:  {', '.join(boundary_paths)}")

    print(f"\n{_bold('Recommendation:')} run `boundver generate --components {component_name}` to update the lockfile.")
    return 1


def analyze_component_drift(
    config: dict,
    lockfile: dict,
    repo_root: Path,
    component_name: str,
    source: str = "head",
    allow_custom_providers: bool = False,
) -> Optional[dict]:
    """Analyze drift for a single component. Returns a dict with analysis results.

    Returns None on error (error printed to stderr).
    Returns a dict with keys:
        changes: Dict[str, dict]  — facets that changed
        summary: str              — human-readable change type
        changed_files: List[Tuple[str, str]]
        version: Optional[str]
        locked_fps: dict
        current_fps: dict
    """
    from ._diff import _summarize_change
    from ._lockfile import generate_lockfile

    comp_cfg = config.get("components", {}).get(component_name)
    if not comp_cfg:
        print(f"ERROR: unknown component '{component_name}'", file=sys.stderr)
        known = sorted(config.get("components", {}).keys())
        if known:
            print(f"Known components: {', '.join(known)}", file=sys.stderr)
        return None

    locked_comp = lockfile.get("components", {}).get(component_name)
    if locked_comp is None:
        print(f"Component '{component_name}' is not in the lockfile — run 'boundver generate' first.", file=sys.stderr)
        return None

    # Compute current fingerprints for just this component.
    subset_config = dict(config)
    subset_config["components"] = {component_name: comp_cfg}
    subset_config["slices"] = {}
    try:
        current_lock = generate_lockfile(subset_config, repo_root, source=source, strict=False,
                                            allow_custom_providers=allow_custom_providers)
    except (MemoryError, RecursionError, KeyboardInterrupt):
        raise
    except Exception as exc:
        print(f"ERROR: could not compute current fingerprints: {exc}", file=sys.stderr)
        return None

    current_comp = current_lock["components"][component_name]
    locked_fps = locked_comp.get("fingerprints") or {}
    current_fps = current_comp.get("fingerprints") or {}

    # Build diff of changed facets.
    changes: Dict[str, dict] = {}
    for facet in ("exact", "behavior", "boundary", "compat"):
        lv = locked_fps.get(facet)
        cv = current_fps.get(facet)
        if lv != cv:
            changes[facet] = {"locked": lv, "current": cv}

    summary = _summarize_change(changes) if changes else ""

    # Get changed files via git diff
    comp_path = comp_cfg.get("path", "?").rstrip("/")
    changed_files: List[Tuple[str, str]] = []
    if changes:
        if source == "working-tree":
            try:
                diff = _git_run(repo_root, ["diff", "HEAD", "--name-status", "--", comp_path])
                staged = _git_run(repo_root, ["diff", "--cached", "--name-status", "--", comp_path])
                for line in (diff.stdout + staged.stdout).splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        changed_files.append((parts[0].strip(), _to_posix(parts[-1].strip())))
            except subprocess.CalledProcessError:
                pass
        elif source == "index":
            try:
                staged = _git_run(repo_root, ["diff", "--cached", "--name-status", "--", comp_path])
                for line in staged.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        changed_files.append((parts[0].strip(), _to_posix(parts[-1].strip())))
            except subprocess.CalledProcessError:
                pass

    version = current_comp.get("version") or locked_comp.get("version")

    return {
        "changes": changes,
        "summary": summary,
        "changed_files": changed_files,
        "version": version,
        "locked_fps": locked_fps,
        "current_fps": current_fps,
    }
