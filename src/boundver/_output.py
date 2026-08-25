"""Output / pretty-printing helpers for boundver."""

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ._git import (
    GitSourceSnapshot,
    _capture_git_source_snapshot,
    _git_cat_blob,
    _git_name_status,
    _git_run,
    _to_posix,
    _validated_git_object_id,
)
from ._utils import (
    FACETS,
    SOURCE_MODE_SET,
    _bounded_json_dumps,
    _effective_component_facets,
    _is_glob,
    _match_path_glob,
    _normalize_declared_path,
    _short,
    boundary_provider_name,
)
from ._consumer_graph import affected_consumer_groups, affected_consumers
from ._lockfile import MAX_LOCKFILE_BYTES, parse_lockfile_bytes


_MAX_DIAGNOSTIC_LOCK_HISTORY_COMMITS = 128
_MAX_DIAGNOSTIC_LOCK_HISTORY_BYTES = 64 * 1024 * 1024


def _root_commit_fallback(
    repo_root: Path,
    target: str,
    reason: str,
) -> Tuple[str, str]:
    """Return a broad, non-silent base when precise lock history is unavailable."""
    try:
        result = _git_run(
            repo_root,
            ["rev-list", "--first-parent", "--max-parents=0", target],
        )
        roots = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if roots:
            root = _validated_git_object_id(
                roots[-1],
                "git root-commit lookup",
            )
            return root, f"root commit fallback ({reason})"
    except (OSError, ValueError, subprocess.CalledProcessError):
        pass
    return target, f"current commit fallback ({reason})"


def _component_lock_history_base(
    repo_root: Path,
    target: str,
    normalized_lock: str,
    component_name: str,
    locked_component: dict,
) -> Optional[Tuple[str, str]]:
    """Find where the current component entry entered the lock's first-parent history."""
    result = _git_run(
        repo_root,
        [
            "log",
            "--first-parent",
            f"--max-count={_MAX_DIAGNOSTIC_LOCK_HISTORY_COMMITS + 1}",
            "--format=%H",
            target,
            "--",
            normalized_lock,
        ],
    )
    commits = [
        _validated_git_object_id(line.strip(), "git lock-history lookup")
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    if not commits:
        return None

    matched_base: Optional[str] = None
    consumed_bytes = 0
    for commit in commits:
        remaining = _MAX_DIAGNOSTIC_LOCK_HISTORY_BYTES - consumed_bytes
        if remaining <= 0:
            return _root_commit_fallback(
                repo_root,
                target,
                "component lock history exceeded the byte limit",
            )
        try:
            data = _git_cat_blob(
                repo_root,
                f"{commit}:{normalized_lock}",
                max_bytes=min(MAX_LOCKFILE_BYTES, remaining),
            )
            consumed_bytes += len(data)
            historical = parse_lockfile_bytes(
                data,
                f"{commit}:{normalized_lock}",
            )
            historical_components = historical.get("components", {})
            historical_component = (
                historical_components.get(component_name)
                if isinstance(historical_components, dict)
                else None
            )
        except (OSError, ValueError, subprocess.CalledProcessError):
            if matched_base is not None:
                return _root_commit_fallback(
                    repo_root,
                    target,
                    "component lock history could not be read safely",
                )
            return None

        if historical_component == locked_component:
            matched_base = commit
            continue
        if matched_base is not None:
            return (
                matched_base,
                f"commit that introduced the current lock entry for {component_name}",
            )
        return None

    if matched_base is None:
        return None
    if len(commits) > _MAX_DIAGNOSTIC_LOCK_HISTORY_COMMITS:
        return _root_commit_fallback(
            repo_root,
            target,
            "component lock history exceeded the commit limit",
        )
    return (
        matched_base,
        f"commit that introduced the current lock entry for {component_name}",
    )


def _resolve_lock_history_base(
    repo_root: Path,
    source: str,
    snapshot: Optional[GitSourceSnapshot],
    lock_path: Optional[str],
    *,
    component_name: Optional[str] = None,
    locked_component: Optional[dict] = None,
) -> Tuple[Optional[str], str]:
    """Resolve a diagnostic base without persisting commit-specific lock data."""
    if source != "head":
        return "HEAD", "default for staged or working-tree diagnostics"
    target = snapshot.head_oid if snapshot is not None else None
    if target is None:
        try:
            result = _git_run(repo_root, ["rev-parse", "--verify", "HEAD^{commit}"])
            target = _validated_git_object_id(
                result.stdout.strip(),
                "git HEAD lookup",
            )
        except (OSError, ValueError, subprocess.CalledProcessError):
            target = None
    if target and lock_path:
        try:
            normalized_lock = _normalize_declared_path(lock_path)
            if component_name and isinstance(locked_component, dict):
                component_base = _component_lock_history_base(
                    repo_root,
                    target,
                    normalized_lock,
                    component_name,
                    locked_component,
                )
                if component_base is not None:
                    return component_base
            result = _git_run(
                repo_root,
                [
                    "log",
                    "--first-parent",
                    "-1",
                    "--format=%H",
                    target,
                    "--",
                    normalized_lock,
                ],
            )
            candidate = result.stdout.strip()
            if candidate:
                return (
                    _validated_git_object_id(
                        candidate,
                        "git log lock-history lookup",
                    ),
                    f"last commit that changed HEAD:{normalized_lock}",
                )
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass
    if target:
        parent_ref = f"{target}~1"
        try:
            _git_run(repo_root, ["rev-parse", "--verify", parent_ref])
        except (OSError, subprocess.CalledProcessError):
            return target, "root commit fallback"
        return parent_ref, "previous-commit fallback (lock history unavailable)"
    return None, "no committed diagnostic base is available"


def _encoding_safe_text(text: str, stream: Any) -> str:
    """Return ``text`` losslessly representable by ``stream``'s encoding."""
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return text
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        try:
            return text.encode(encoding, "backslashreplace").decode(encoding)
        except LookupError:
            return text.encode("ascii", "backslashreplace").decode("ascii")
    return text


def safe_print(
    *values: object,
    sep: Optional[str] = " ",
    end: Optional[str] = "\n",
    file: Any = None,
    flush: bool = False,
) -> None:
    """Print without failing when a redirected stream cannot encode Unicode.

    ``backslashreplace`` is reversible enough for diagnostics and, unlike
    replacement characters or ignored bytes, retains every code point.
    """
    if sep is None:
        sep = " "
    elif not isinstance(sep, str):
        raise TypeError(f"sep must be None or a string, not {type(sep).__name__}")
    if end is None:
        end = "\n"
    elif not isinstance(end, str):
        raise TypeError(f"end must be None or a string, not {type(end).__name__}")

    stream = sys.stdout if file is None else file
    text = sep.join(str(value) for value in values) + end
    safe = _encoding_safe_text(text, stream)
    try:
        stream.write(safe)
    except UnicodeEncodeError:
        # Some proxy streams do not accurately expose their final encoding.
        stream.write(safe.encode("ascii", "backslashreplace").decode("ascii"))
    if flush:
        stream.flush()


def configure_cli_streams() -> None:
    """Make argparse and direct stream writes safe on legacy code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            # In-memory/proxy streams may not support reconfiguration. Calls
            # through ``safe_print`` still use an encoding-aware fallback.
            continue


# Keep every human-output path in this module safe, including direct callers
# that do not enter through ``core.main``.
print = safe_print


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


def _display_path(path: object) -> str:
    """Return an ASCII, single-line representation safe for terminal output."""
    rendered: List[str] = []
    for character in str(path):
        codepoint = ord(character)
        if 0x20 <= codepoint <= 0x7E:
            rendered.append(character)
        elif 0xDC80 <= codepoint <= 0xDCFF:
            rendered.append(f"\\x{codepoint - 0xDC00:02x}")
        elif codepoint <= 0xFF:
            rendered.append(f"\\x{codepoint:02x}")
        elif codepoint <= 0xFFFF:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(f"\\U{codepoint:08x}")
    return "".join(rendered)


def _display_value(value: object) -> str:
    """Render metadata without depending on CPython's integer digit setting."""
    return _bounded_json_dumps(value, ensure_ascii=True, sort_keys=True)


def print_diff(diff: dict) -> None:
    lock_metadata = diff.get("changed_metadata", {})
    if lock_metadata:
        print("\n  LOCKFILE METADATA CHANGED:")
        for field, values in lock_metadata.items():
            print(
                f"    {field}: {_display_value(values['old'])} -> "
                f"{_display_value(values['new'])}"
            )

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
                print(
                    f"      {field}: {_display_value(vals['old'])} -> "
                    f"{_display_value(vals['new'])}"
                )

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
            for field, values in s.get("changed_metadata", {}).items():
                print(
                    f"      {field}: {_display_value(values['old'])} -> "
                    f"{_display_value(values['new'])}"
                )
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
        print("    This is expected -- implicit tracks exact changes only.")
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
    base_ref: Optional[str] = None,
    source: str = "head",
    snapshot: Optional[GitSourceSnapshot] = None,
    lock_path: Optional[str] = "boundary.lock.json",
    lockfile: Optional[dict] = None,
) -> Dict[str, Any]:
    """Analyze changed tracked files for one component and its boundary subset.

    Returns a dict with keys:
        error (str|None), component_name, component_path, effective_base,
        source, changed (list of (status, path) tuples),
        boundary_provider, boundary_paths, boundary_changed (list of (status, path) tuples).
    """
    if source not in SOURCE_MODE_SET:
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
    if isinstance(base_ref, str) and base_ref.lstrip().startswith("-"):
        return {"error": f"invalid base ref: {base_ref!r}"}
    comp = config.get("components", {}).get(component_name)
    if not comp:
        known = sorted(config.get("components", {}).keys())
        return {"error": f"unknown component '{component_name}'", "known": known}
    if source in {"head", "index"} and snapshot is None:
        try:
            snapshot = _capture_git_source_snapshot(repo_root, source)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            detail = str(exc).strip() or type(exc).__name__
            return {"error": f"cannot capture {source} source: {detail}"}

    component_path = str(comp.get("path", "")).rstrip("/")
    boundary = comp.get("boundary", {})
    boundary_paths_raw = boundary.get("paths", []) if isinstance(boundary, dict) else []

    auto_base = not base_ref
    if auto_base:
        locked_component = None
        if isinstance(lockfile, dict):
            raw_components = lockfile.get("components", {})
            if isinstance(raw_components, dict):
                candidate = raw_components.get(component_name)
                if isinstance(candidate, dict):
                    locked_component = candidate
        effective_base, base_origin = _resolve_lock_history_base(
            repo_root,
            source,
            snapshot,
            lock_path,
            component_name=component_name,
            locked_component=locked_component,
        )
    else:
        effective_base = base_ref
        base_origin = "explicit --base-ref"
    if effective_base is None:
        return {"error": "cannot determine a committed diagnostic base"}
    root_commit_target: Optional[str] = None
    if source == "head" and base_origin == "root commit fallback":
        root_commit_target = snapshot.head_oid if snapshot is not None else "HEAD"

    # Choose diff target based on source
    if root_commit_target is not None:
        diff_args = [
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-status",
            "-r",
            "-z",
            root_commit_target,
        ]
    else:
        diff_args = ["diff", "--name-status", "-z"]
    if root_commit_target is not None:
        pass
    elif source == "working-tree":
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
                if component_path in {"", "."}
                or path == component_path
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
        diff_args.append("--")
        if component_path not in {"", "."}:
            diff_args.append(component_path)
        try:
            changed = _git_name_status(
                repo_root,
                ["--literal-pathspecs", *diff_args],
            )
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            return {
                "error": (
                    f"failed to diff '{component_name}' against "
                    f"{effective_base}: {exc}"
                )
            }

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
        "base_origin": base_origin,
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
    base_ref: Optional[str] = None,
    source: str = "head",
    snapshot: Optional[GitSourceSnapshot] = None,
    lock_path: Optional[str] = "boundary.lock.json",
    lockfile: Optional[dict] = None,
) -> int:
    """Explain changed tracked files for one component and its boundary subset."""
    result = analyze_explain_changes(
        config,
        repo_root,
        component_name,
        base_ref,
        source,
        snapshot=snapshot,
        lock_path=lock_path,
        lockfile=lockfile,
    )

    if result.get("error"):
        print(f"ERROR: {result['error']}", file=sys.stderr)
        if result.get("known"):
            print(f"Known components: {', '.join(result['known'])}", file=sys.stderr)
        return 2

    component_path = result["component_path"]
    effective_base = result["effective_base"]
    base_origin = result.get("base_origin", "resolved diagnostic base")
    changed = result["changed"]
    boundary_paths = result["boundary_paths"]
    boundary_changed = result["boundary_changed"]

    print(f"Component: {component_name}")
    print(f"Path: {component_path}")
    print(f"Base ref: {effective_base} ({base_origin})")
    print(f"Source: {source}")
    gated_facets = _effective_component_facets(config, component_name)
    print(f"Gated facets: {', '.join(sorted(gated_facets)) or 'none'}")

    if not changed:
        print("\nNo tracked file changes detected for this component path.")
        return 0

    print(f"\nChanged files ({len(changed)}):")
    for status, rel in changed:
        print(f"  {status:>2}  {_display_path(rel)}")

    if not boundary_paths:
        print("\nBoundary paths: none declared")
        return 0

    print(f"\nBoundary provider: {result['boundary_provider']}")
    print("Boundary paths:")
    for bp in boundary_paths:
        print(f"  - {_display_path(bp)}")

    if boundary_changed:
        print(f"\nBoundary-relevant changed files ({len(boundary_changed)}):")
        for status, rel in boundary_changed:
            print(f"  {status:>2}  {_display_path(rel)}")
    else:
        print("\nBoundary-relevant changed files: none")

    return 0


def _print_json(data: Any) -> None:
    print(_bounded_json_dumps(data, indent=2, sort_keys=True))


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
    diagnostic_base_ref: Optional[str] = None,
    lock_path: Optional[str] = "boundary.lock.json",
    lock_provenance: Optional[str] = None,
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
        diagnostic_base_ref=diagnostic_base_ref,
        lock_path=lock_path,
    )
    if result is None:
        return 2  # error already printed by analyze_component_drift

    comp_cfg = config["components"][component_name]
    comp_path = comp_cfg.get("path", "?")
    changes = result["changes"]
    gated_facets = _effective_component_facets(config, component_name)
    gated_changes = sorted(set(changes) & gated_facets)
    non_gating_changes = sorted(set(changes) - gated_facets)
    has_gated_drift = bool(
        gated_changes or result["metadata_changes"] or result["digest_errors"]
    )
    has_observed_drift = bool(
        changes or result["metadata_changes"] or result["digest_errors"]
    )
    consumer_groups = (
        affected_consumer_groups(
            config.get("components", {}),
            component_name,
            transitive=transitive_consumers,
        )
        if {"boundary", "compat"} & set(changes)
        else {"components": [], "external_consumers": []}
    )
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
                "lock_provenance": lock_provenance,
                "version": result["version"],
                "drifted": has_gated_drift,
                "observed_drift": has_observed_drift,
                "changes": changes,
                "gated_facets": sorted(gated_facets),
                "gated_changes": gated_changes,
                "non_gating_changes": non_gating_changes,
                "metadata_changes": result["metadata_changes"],
                "digest_errors": result["digest_errors"],
                "summary": result["summary"],
                "changed_files": [
                    {"status": status, "path": path}
                    for status, path in result["changed_files"]
                ],
                "changed_files_status": result.get(
                    "changed_files_status",
                    "ok" if result["changed_files"] else "not-run",
                ),
                "changed_files_error": result.get("changed_files_error"),
                "diagnostic_base": result.get("diagnostic_base"),
                "diagnostic_base_origin": result.get("diagnostic_base_origin"),
                "boundary_paths": comp_cfg.get("boundary", {}).get("paths", []),
                "provider_detail": result.get("provider_explanation", ""),
                "affected_consumers": consumers,
                "affected_components": consumer_groups["components"],
                "affected_external_consumers": consumer_groups[
                    "external_consumers"
                ],
                "transitive_consumers": transitive_consumers,
            }
        )
        return 1 if has_gated_drift else 0

    # Format human-readable output.
    print(f"\nComponent:  {_bold(component_name)}")
    print(f"Path:       {_display_path(comp_path)}")
    print(f"Source:     {source}")
    if lock_provenance:
        print(f"Lock:       {lock_provenance}")
    print(f"Gated:      {', '.join(sorted(gated_facets)) or 'none'}")
    if result["version"]:
        print(f"Version:    {result['version']}")

    if not has_observed_drift:
        print(_green("\nStatus: UP TO DATE -- no fingerprint or metadata drift detected."))
        return 0

    drift_count = (
        len(gated_changes)
        + len(result["metadata_changes"])
        + len(result["digest_errors"])
    )
    if has_gated_drift:
        print(_red(f"\nStatus: DRIFTED -- {drift_count} gated issue(s) detected"))
    else:
        print(
            _green("\nStatus: UP TO DATE -- no gated drift detected.")
        )
        print(
            _yellow(
                f"Non-gating drift observed ({len(non_gating_changes)} facet(s))."
            )
        )

    print("\nFingerprint changes:")
    for facet in FACETS:
        lv = result["locked_fps"].get(facet)
        cv = result["current_fps"].get(facet)
        if facet in changes:
            classification = "gating" if facet in gated_facets else "non-gating"
            rendered = _red(_short(cv)) if facet in gated_facets else _yellow(_short(cv))
            print(
                f"  {facet:<10}  {_short(lv)}  ->  {rendered}  "
                f"(changed, {classification})"
            )
        else:
            print(f"  {facet:<10}  {_short(lv)}  ->  {_short(cv)}  (unchanged)")

    print(f"\n{_yellow('Change type:')}  {result['summary']}")

    if result["metadata_changes"]:
        print("\nMetadata changes:")
        for field, values in result["metadata_changes"].items():
            print(
                f"  {field}: {_display_value(values['locked'])} -> "
                f"{_display_value(values['current'])}"
            )

    if result["digest_errors"]:
        print(_red("\nFingerprint errors:"))
        for message in result["digest_errors"]:
            print(f"  {message}")

    changed_files_status = result.get(
        "changed_files_status",
        "ok" if result["changed_files"] else "not-run",
    )
    if changed_files_status == "error":
        detail = result.get("changed_files_error") or "unknown diagnostic error"
        print(
            _red(
                f"\nChanged-file diagnostics failed under "
                f"{_display_path(comp_path)}: {detail}"
            )
        )
    elif result["changed_files"]:
        print(f"\nModified files under {_display_path(comp_path)}:")
        seen: set = set()
        for status, rel in result["changed_files"]:
            if rel not in seen:
                seen.add(rel)
                print(f"  {status:>2}  {_display_path(rel)}")
    elif changed_files_status == "ok" and source == "index":
        print(
            f"\nChanged-file diagnostics found no staged tracked changes "
            f"under {_display_path(comp_path)}."
        )
        print(
            f"  Tip: run `git diff --cached --name-only -- "
            f"{_display_path(comp_path)}` to inspect the index."
        )
    elif changed_files_status == "ok" and source == "working-tree":
        print(
            f"\nChanged-file diagnostics found no tracked staged or "
            f"unstaged changes under {_display_path(comp_path)}."
        )
    else:
        print(f"\nChanged-file diagnostics were not run for source={source}.")
        if source == "head":
            print(
                f"  Tip: run `git log --oneline -- "
                f"{_display_path(comp_path)}` to inspect component history."
            )

    boundary_paths = comp_cfg.get("boundary", {}).get("paths", [])
    if boundary_paths and "boundary" in changes:
        print(f"\nBoundary paths:  {', '.join(_display_path(path) for path in boundary_paths)}")
    if result.get("provider_explanation"):
        print(f"Provider detail: {result['provider_explanation']}")

    if result.get("diagnostic_base"):
        print(
            f"Diagnostic base: {result['diagnostic_base']} "
            f"({result.get('diagnostic_base_origin') or 'resolved'})"
        )

    if consumers and ({"boundary", "compat"} & set(changes)):
        qualifier = " (transitive)" if transitive_consumers else ""
        print(f"\nAffected consumers{qualifier}: {', '.join(consumers)}")

    if has_gated_drift:
        print(
            f"\n{_bold('Recommendation:')} run `boundver generate --components "
            f"{component_name} --source {source}` to update the lockfile."
        )
        return 1
    print("\nNo lockfile update is required by the effective facet policy.")
    return 0


def analyze_component_drift(
    config: dict,
    lockfile: dict,
    repo_root: Path,
    component_name: str,
    source: str = "head",
    allow_custom_providers: bool = False,
    snapshot: Optional[GitSourceSnapshot] = None,
    diagnostic_base_ref: Optional[str] = None,
    lock_path: Optional[str] = "boundary.lock.json",
) -> Optional[dict]:
    """Analyze drift for a single component. Returns a dict with analysis results.

    Returns None on error (error printed to stderr).
    Returns a dict with keys:
        changes: Dict[str, dict]  — facets that changed
        summary: str              — human-readable change type
        changed_files: List[Tuple[str, str]]
        changed_files_status: str — ok, error, or not-run
        changed_files_error: Optional[str]
        version: Optional[str]
        locked_fps: dict
        current_fps: dict
    """
    if (
        isinstance(diagnostic_base_ref, str)
        and diagnostic_base_ref.lstrip().startswith("-")
    ):
        print(
            f"ERROR: invalid diagnostic base ref: {diagnostic_base_ref!r}",
            file=sys.stderr,
        )
        return None

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
        print(f"Component '{component_name}' is not in the lockfile -- run 'boundver generate' first.", file=sys.stderr)
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
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            detail = str(exc).strip() or type(exc).__name__
            print(
                f"ERROR: cannot capture {source} source: {detail}",
                file=sys.stderr,
            )
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
    for facet in FACETS:
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
                read_file_limited=accessor.read_file_limited,
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
    component_pathspec = "." if comp_path in {"", "."} else comp_path
    changed_files: List[Tuple[str, str]] = []
    changed_files_status = "not-run"
    changed_files_error: Optional[str] = None
    diagnostic_base: Optional[str] = None
    diagnostic_base_origin: Optional[str] = None
    if changes:
        if source == "working-tree":
            diagnostic_base = diagnostic_base_ref or "HEAD"
            diagnostic_base_origin = (
                "explicit --base-ref"
                if diagnostic_base_ref
                else "default for working-tree diagnostics"
            )
            try:
                diagnostic_files: List[Tuple[str, str]] = []
                diagnostic_files.extend(
                    _git_name_status(
                        repo_root,
                        [
                            "--literal-pathspecs",
                            "diff",
                            diagnostic_base,
                            "--name-status",
                            "-z",
                            "--",
                            component_pathspec,
                        ],
                    )
                )
                diagnostic_files.extend(
                    _git_name_status(
                        repo_root,
                        [
                            "--literal-pathspecs",
                            "diff",
                            "--cached",
                            "--name-status",
                            "-z",
                            "--",
                            component_pathspec,
                        ],
                    )
                )
                changed_files = diagnostic_files
                changed_files_status = "ok"
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                changed_files = []
                changed_files_status = "error"
                changed_files_error = str(exc).strip() or type(exc).__name__
        elif source == "index":
            diagnostic_base = diagnostic_base_ref or "HEAD"
            diagnostic_base_origin = (
                "explicit --base-ref"
                if diagnostic_base_ref
                else "default for staged diagnostics"
            )
            if snapshot is not None and snapshot.head_oid is None:
                changed_files.extend(
                    ("A", path)
                    for path in sorted(snapshot.entries)
                    if comp_path in {"", "."}
                    or path == comp_path
                    or path.startswith(f"{comp_path}/")
                )
                changed_files_status = "ok"
            else:
                try:
                    if diagnostic_base_ref:
                        target = snapshot.tree_oid if snapshot is not None else "--cached"
                        diff_args = (
                            ["diff", "--name-status", "-z", diagnostic_base, target]
                            if snapshot is not None
                            else [
                                "diff", "--cached", "--name-status", "-z",
                                diagnostic_base,
                            ]
                        )
                    elif snapshot is None:
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
                    changed_files.extend(
                        _git_name_status(
                            repo_root,
                            [
                                "--literal-pathspecs",
                                *diff_args,
                                "--",
                                component_pathspec,
                            ],
                        )
                    )
                    changed_files_status = "ok"
                except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                    changed_files = []
                    changed_files_status = "error"
                    changed_files_error = str(exc).strip() or type(exc).__name__
        elif source == "head":
            try:
                if diagnostic_base_ref:
                    diagnostic_base = diagnostic_base_ref
                    diagnostic_base_origin = "explicit --base-ref"
                else:
                    diagnostic_base, diagnostic_base_origin = (
                        _resolve_lock_history_base(
                            repo_root,
                            source,
                            snapshot,
                            lock_path,
                            component_name=component_name,
                            locked_component=locked_comp,
                        )
                    )
                if diagnostic_base is None:
                    raise ValueError("no committed diagnostic base is available")
                target = (
                    snapshot.head_oid
                    if snapshot is not None and snapshot.head_oid is not None
                    else "HEAD"
                )
                changed_files = _git_name_status(
                    repo_root,
                    [
                        "--literal-pathspecs",
                        "diff",
                        "--name-status",
                        "-z",
                        diagnostic_base,
                        target,
                        "--",
                        component_pathspec,
                    ],
                )
                changed_files_status = "ok"
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                changed_files = []
                changed_files_status = "error"
                changed_files_error = str(exc).strip() or type(exc).__name__

    # ``git diff HEAD`` already includes staged changes, while the additional
    # cached diff is useful for unborn/fallback cases.  Keep one stable entry
    # per path so JSON and text views expose the same file set.
    deduplicated_files: List[Tuple[str, str]] = []
    seen_paths: set = set()
    for status, path in changed_files:
        if path in seen_paths:
            continue
        seen_paths.add(path)
        deduplicated_files.append((status, path))
    changed_files = deduplicated_files

    version = current_comp.get("version") or locked_comp.get("version")

    return {
        "changes": changes,
        "metadata_changes": metadata_changes,
        "digest_errors": digest_errors,
        "summary": summary,
        "changed_files": changed_files,
        "changed_files_status": changed_files_status,
        "changed_files_error": changed_files_error,
        "diagnostic_base": diagnostic_base,
        "diagnostic_base_origin": diagnostic_base_origin,
        "version": version,
        "locked_fps": locked_fps,
        "current_fps": current_fps,
        "provider_explanation": provider_explanation,
    }
