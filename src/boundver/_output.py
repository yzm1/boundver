"""Output / pretty-printing helpers for boundver."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._git import (
    GitSourceSnapshot,
    _capture_git_source_snapshot,
    _git_run,
    _git_run_bytes,
    _to_posix,
)
from ._utils import (
    _is_glob,
    _match_path_glob,
    _normalize_declared_path,
    _short,
    boundary_provider_name,
)
from ._consumer_graph import affected_consumers


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


def _parse_name_status_z(data: bytes) -> List[Tuple[str, str]]:
    """Parse ``git diff --name-status -z`` without filename ambiguity."""
    fields = [field for field in data.split(b"\0") if field]
    changed: List[Tuple[str, str]] = []
    index = 0
    while index < len(fields):
        status = os.fsdecode(fields[index])
        index += 1
        if index >= len(fields):
            break
        path = os.fsdecode(fields[index])
        index += 1
        if status.startswith(("R", "C")) and index < len(fields):
            destination = os.fsdecode(fields[index])
            index += 1
            # A rename can remove a declared boundary path even when its
            # destination is internal. Preserve both identities for impact
            # classification. A copy leaves its source unchanged.
            if status.startswith("R"):
                changed.append((status, path))
            path = destination
        changed.append((status, path))
    return changed


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
            for field, vals in c.get("changed_metadata", {}).items():
                print(f"      {field}: {vals['old']!r} -> {vals['new']!r}")

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

    print("\n  Component details:")
    for name, component in sorted(comps.items()):
        fps = component.get("fingerprints", {})
        version = component.get("version") or "unversioned"
        provider = component.get("boundary_provider", "unknown")
        state = component.get("boundary_status", "unknown")
        print(
            f"    {name}: {component.get('path', '?')} @ {version}  "
            f"{provider}/{state}  exact={_short(fps.get('exact'))}  "
            f"boundary={_short(fps.get('boundary'))}"
        )
        consumers = component.get("consumers", [])
        if consumers:
            print(f"      consumers: {', '.join(consumers)}")
        external_consumers = component.get("external_consumers", [])
        if external_consumers:
            print(f"      external consumers: {', '.join(external_consumers)}")

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
        print("    To track a declared boundary, edit the component's boundary provider and paths in the config.")

    # Warnings
    warnings = []
    for name, c in comps.items():
        for w in c.get("warnings", []):
            warnings.append(f"    {name}: {w}")
        for e in c.get("boundary_errors", []):
            warnings.append(f"    {name}: boundary {c.get('boundary_status', 'unknown')} - {e}")
        for e in c.get("version_errors", []):
            warnings.append(f"    {name}: version error - {e}")
        for e in c.get("behavior_errors", []):
            warnings.append(f"    {name}: behavior error - {e}")
        for e in c.get("exact_errors", []):
            warnings.append(f"    {name}: exact error - {e}")
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
    config: dict,
    repo_root: Path,
    component_name: str,
    base_ref: str = "HEAD",
    source: str = "head",
    snapshot: Optional[GitSourceSnapshot] = None,
) -> Dict[str, Any]:
    """Analyze changed tracked files for one component and its boundary subset.

    Returns a dict with keys:
        error (str|None), component_name, component_path, effective_base,
        source, changed (list of (status, path) tuples),
        boundary_provider, boundary_paths, boundary_changed (list of (status, path) tuples).
    """
    if source not in {"head", "index", "working-tree"}:
        return {
            "error": (
                f"unknown source mode {source!r}; expected head, index, or "
                "working-tree"
            )
        }
    if snapshot is not None and snapshot.source != source:
        return {
            "error": (
                f"captured source mismatch: snapshot={snapshot.source!r}, "
                f"source={source!r}"
            )
        }
    if source == "working-tree" and snapshot is not None:
        return {"error": "working-tree source does not accept a Git snapshot"}
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
    # Resolve the automatic parent from the captured commit when available so
    # a concurrent branch update cannot change either side of the comparison.
    effective_base = base_ref
    if source == "head" and base_ref == "HEAD":
        captured_head = snapshot.head_oid if snapshot is not None else None
        parent_ref = f"{captured_head}~1" if captured_head else "HEAD~1"
        try:
            _git_run(repo_root, ["rev-parse", "--verify", parent_ref])
            effective_base = parent_ref
        except subprocess.CalledProcessError:
            pass

    # Choose diff target based on source
    diff_args = ["diff", "--name-status", "-z"]
    if source == "working-tree":
        diff_args.append(effective_base)
    elif source == "index" and snapshot is not None:
        if effective_base == "HEAD" and snapshot.head_oid is not None:
            effective_base = snapshot.head_oid
        if snapshot.head_oid is None and effective_base == "HEAD":
            # An unborn repository has no base tree.  Use the captured entry
            # set rather than consulting the mutable live index again.
            changed = [
                ("A", path)
                for path in sorted(snapshot.entries)
                if path == component_path
                or path.startswith(f"{component_path}/")
            ]
            diff_args = []
        else:
            diff_args.extend([effective_base, snapshot.tree_oid])
    elif source == "index":
        diff_args.extend(["--cached", effective_base])
    elif snapshot is not None:
        diff_args.extend(
            [effective_base, snapshot.head_oid or snapshot.tree_oid]
        )
    else:
        diff_args.extend([effective_base, "HEAD"])
    if diff_args:
        diff_args.extend(["--", component_path])
        try:
            diff = _git_run_bytes(
                repo_root, ["--literal-pathspecs", *diff_args]
            )
        except subprocess.CalledProcessError as exc:
            return {
                "error": (
                    f"failed to diff '{component_name}' against "
                    f"{effective_base}: {exc}"
                )
            }
        changed = _parse_name_status_z(diff.stdout)

    component_prefix = f"{_to_posix(component_path)}/"
    normalized_boundary_paths: List[str] = []
    for p in boundary_paths_raw:
        try:
            normalized_boundary_paths.append(_normalize_declared_path(p))
        except (TypeError, ValueError):
            continue

    boundary_changed: List[Tuple[str, str]] = []
    for status, rel in changed:
        component_relative = rel
        if component_relative.startswith(component_prefix):
            component_relative = component_relative[len(component_prefix):]
        for bp in normalized_boundary_paths:
            if _is_glob(bp):
                if _match_path_glob(component_relative, bp):
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


def explain_component_changes(
    config: dict,
    repo_root: Path,
    component_name: str,
    base_ref: str = "HEAD",
    source: str = "head",
    snapshot: Optional[GitSourceSnapshot] = None,
) -> int:
    """Explain changed tracked files for one component and its boundary subset."""
    result = analyze_explain_changes(
        config,
        repo_root,
        component_name,
        base_ref,
        source,
        snapshot=snapshot,
    )

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
    snapshot: Optional[GitSourceSnapshot] = None,
    transitive_consumers: bool = False,
    output_format: str = "text",
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
        snapshot=snapshot,
    )
    if result is None:
        return 2  # error already printed by analyze_component_drift

    comp_cfg = config["components"][component_name]
    comp_path = comp_cfg.get("path", "?")
    changes = result["changes"]
    consumers = (
        affected_consumers(
            config.get("components", {}),
            component_name,
            transitive=transitive_consumers,
        )
        if {"boundary", "compat"} & set(changes)
        else []
    )
    if output_format == "json":
        _print_json(
            {
                "component": component_name,
                "path": comp_path,
                "source": source,
                "version": result["version"],
                "drifted": bool(
                    changes
                    or result["metadata_changes"]
                    or result["digest_errors"]
                ),
                "changes": changes,
                "metadata_changes": result["metadata_changes"],
                "digest_errors": result["digest_errors"],
                "summary": result["summary"],
                "changed_files": [
                    {"status": status, "path": path}
                    for status, path in result["changed_files"]
                ],
                "boundary_paths": comp_cfg.get("boundary", {}).get("paths", []),
                "provider_detail": result.get("provider_explanation", ""),
                "affected_consumers": consumers,
                "transitive_consumers": transitive_consumers,
            }
        )
        return 1 if (
            changes or result["metadata_changes"] or result["digest_errors"]
        ) else 0

    # Format human-readable output.
    print(f"\nComponent:  {_bold(component_name)}")
    print(f"Path:       {comp_path}")
    print(f"Source:     {source}")
    if result["version"]:
        print(f"Version:    {result['version']}")

    if not (result["changes"] or result["metadata_changes"] or result["digest_errors"]):
        print(_green("\nStatus: UP TO DATE — no fingerprint or metadata drift detected."))
        return 0

    drift_count = len(changes) + len(result["metadata_changes"]) + len(result["digest_errors"])
    print(_red(f"\nStatus: DRIFTED — {drift_count} issue(s) detected"))

    print("\nFingerprint changes:")
    for facet in ("exact", "behavior", "boundary", "compat"):
        lv = result["locked_fps"].get(facet)
        cv = result["current_fps"].get(facet)
        if facet in changes:
            print(f"  {facet:<10}  {_short(lv)}  →  {_red(_short(cv))}  (changed)")
        else:
            print(f"  {facet:<10}  {_short(lv)}  →  {_short(cv)}  (unchanged)")

    print(f"\n{_yellow('Change type:')}  {result['summary']}")

    if result["metadata_changes"]:
        print("\nMetadata changes:")
        for field, values in result["metadata_changes"].items():
            print(f"  {field}: {values['locked']!r} → {values['current']!r}")

    if result["digest_errors"]:
        print(_red("\nFingerprint errors:"))
        for message in result["digest_errors"]:
            print(f"  {message}")

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
    if result.get("provider_explanation"):
        print(f"Provider detail: {result['provider_explanation']}")

    if consumers and ({"boundary", "compat"} & set(changes)):
        qualifier = " (transitive)" if transitive_consumers else ""
        print(f"\nAffected consumers{qualifier}: {', '.join(consumers)}")

    print(
        f"\n{_bold('Recommendation:')} run `boundver generate --components "
        f"{component_name} --source {source}` to update the lockfile."
    )
    return 1


def analyze_component_drift(
    config: dict,
    lockfile: dict,
    repo_root: Path,
    component_name: str,
    source: str = "head",
    allow_custom_providers: bool = False,
    snapshot: Optional[GitSourceSnapshot] = None,
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
    from ._lockfile import (
        COMPONENT_METADATA_FIELDS,
        _SourceAccessor,
        _generation_errors,
        generate_lockfile,
    )
    from .providers import (
        ProviderContext,
        create_registry,
        explain_provider_diff,
        get_provider,
        load_custom_providers,
    )

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

    if snapshot is not None and snapshot.source != source:
        print(
            "ERROR: captured source mismatch: "
            f"snapshot={snapshot.source!r}, source={source!r}",
            file=sys.stderr,
        )
        return None
    if source == "working-tree" and snapshot is not None:
        print(
            "ERROR: working-tree source does not accept a Git snapshot",
            file=sys.stderr,
        )
        return None
    if source in {"head", "index"} and snapshot is None:
        try:
            snapshot = _capture_git_source_snapshot(repo_root, source)
        except ValueError as exc:
            print(f"ERROR: cannot capture {source} source: {exc}", file=sys.stderr)
            return None

    # Compute current fingerprints for just this component.
    subset_config = dict(config)
    subset_config["components"] = {component_name: comp_cfg}
    subset_config["slices"] = {}
    try:
        current_lock = generate_lockfile(
            subset_config,
            repo_root,
            source=source,
            strict=False,
            allow_custom_providers=allow_custom_providers,
            snapshot=snapshot,
        )
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

    metadata_changes: Dict[str, dict] = {}
    for field in COMPONENT_METADATA_FIELDS:
        locked_value = locked_comp.get(field)
        current_value = current_comp.get(field)
        if locked_value != current_value:
            metadata_changes[field] = {
                "locked": locked_value,
                "current": current_value,
            }
    digest_errors = [
        *(f"current {message}" for message in _generation_errors(
            {"components": {component_name: current_comp}}
        )),
        *(f"locked {message}" for message in _generation_errors(
            {"components": {component_name: locked_comp}}
        )),
    ]

    if changes:
        summary = _summarize_change(changes)
    elif digest_errors:
        summary = "fingerprint computation failed"
    elif metadata_changes:
        summary = "component metadata changed"
    else:
        summary = ""

    provider_explanation = ""
    if "boundary" in changes or "boundary_metadata" in metadata_changes:
        registry = create_registry()
        load_errors = load_custom_providers(
            config.get("providers", []),
            allow_custom=allow_custom_providers,
            registry=registry,
        )
        provider_name = boundary_provider_name(comp_cfg.get("boundary", {}))
        provider = get_provider(provider_name, registry=registry)
        if provider is not None and not load_errors:
            accessor = _SourceAccessor(repo_root, source, snapshot=snapshot)
            ctx = ProviderContext(
                repo_root=repo_root,
                component_path=comp_cfg.get("path", "").rstrip("/"),
                boundary_cfg=comp_cfg.get("boundary", {}),
                source=source,
                read_file=accessor.read_file,
                list_files=accessor.list_files,
            )
            provider_explanation = explain_provider_diff(
                provider,
                locked_comp.get("boundary_metadata"),
                current_comp.get("boundary_metadata"),
                ctx,
            )

    # Get changed files via git diff
    comp_path = comp_cfg.get("path", "?").rstrip("/")
    changed_files: List[Tuple[str, str]] = []
    if changes:
        if source == "working-tree":
            try:
                diff = _git_run_bytes(repo_root, ["--literal-pathspecs", "diff", "HEAD", "--name-status", "-z", "--", comp_path])
                staged = _git_run_bytes(repo_root, ["--literal-pathspecs", "diff", "--cached", "--name-status", "-z", "--", comp_path])
                changed_files.extend(_parse_name_status_z(diff.stdout))
                changed_files.extend(_parse_name_status_z(staged.stdout))
            except subprocess.CalledProcessError:
                pass
        elif source == "index":
            if snapshot is not None and snapshot.head_oid is None:
                changed_files.extend(
                    ("A", path)
                    for path in sorted(snapshot.entries)
                    if path == comp_path or path.startswith(f"{comp_path}/")
                )
            else:
                try:
                    if snapshot is None:
                        diff_args = [
                            "diff", "--cached", "--name-status", "-z",
                        ]
                    else:
                        diff_args = [
                            "diff",
                            "--name-status",
                            "-z",
                            snapshot.head_oid,
                            snapshot.tree_oid,
                        ]
                    staged = _git_run_bytes(
                        repo_root,
                        ["--literal-pathspecs", *diff_args, "--", comp_path],
                    )
                    changed_files.extend(_parse_name_status_z(staged.stdout))
                except subprocess.CalledProcessError:
                    pass

    version = current_comp.get("version") or locked_comp.get("version")

    return {
        "changes": changes,
        "metadata_changes": metadata_changes,
        "digest_errors": digest_errors,
        "summary": summary,
        "changed_files": changed_files,
        "version": version,
        "locked_fps": locked_fps,
        "current_fps": current_fps,
        "provider_explanation": provider_explanation,
    }
