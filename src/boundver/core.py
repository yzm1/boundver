#!/usr/bin/env python3
"""
boundver: Git-aware, faceted contract-change detection.

Generates a lockfile where each component has:
    - exact fingerprint     (did anything change?)
    - behavior fingerprint  (did the behavioral contract change?)
    - boundary fingerprint  (did the public boundary change?)
    - compat fingerprint    (did the compatibility family change?)

Slices group components and produce their own stable fingerprints.
Adding an unrelated component does NOT change existing slice fingerprints.

Start here:
    boundver init --discover
    boundver validate-config
    boundver generate --source working-tree
    boundver verify --source working-tree

Common commands:
    boundver generate [--config boundary.config.json] [--out boundary.lock.json]
    boundver verify  [--config boundary.config.json] [--lock boundary.lock.json]
    boundver diff    <old.lock.json> <new.lock.json>
    boundver slice   <slice_name> [--lock boundary.lock.json]
    boundver validate-config [--config boundary.config.json]
    boundver status  [--config boundary.config.json] [--lock boundary.lock.json]

Requires: Git and Python 3.9+.
"""

import argparse
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict as Dict
from typing import List, Optional

# Sub-module imports — each group lives in a focused module.
#
# Imports written as ``name as name`` below are deliberate compatibility
# re-exports for callers that imported implementation helpers from
# ``boundver.core`` before the module was split up. They are not needed by
# this file itself, but removing them would be an unnecessary API break. New
# code should import those helpers from their owning modules instead.
from ._git import (
    GitSourceSnapshot,
    _capture_git_source_snapshot,
    _git_run as _git_run,
    _is_ignored as _is_ignored,
    _list_files_for_source as _list_files_for_source,
    _to_posix as _to_posix,
    changed_components_since_ref,
    dirty_component_paths,
    git_latest_tag as git_latest_tag,
    git_root,
    list_head_files as list_head_files,
    _git_cat_blob as _git_cat_blob,
    _git_batch_cat as _git_batch_cat,
)
from ._hashing import (
    MAX_HASH_FILE_BYTES as MAX_HASH_FILE_BYTES,
    MAX_HASH_FILES as MAX_HASH_FILES,
    _content_only_digest as _content_only_digest,
    _enforce_content_size as _enforce_content_size,
    _enforce_hash_guardrails as _enforce_hash_guardrails,
    _read_path_content as _read_path_content,
    canonical_json as canonical_json,
    sha256_hex as sha256_hex,
    source_tree_digest as source_tree_digest,
)
from ._utils import (
    FACETS,
    FACET_SET,
    SOURCE_MODES,
    SOURCE_MODE_SET,
    _available_component_facets,
    _bounded_json_dumps,
    BoundverError as BoundverError,
    ConfigError,
    GuardrailError as GuardrailError,
    LockfileError,
    ProviderError as ProviderError,
    _issue_facet,
    _short,
)
from ._config import (
    config_warnings,
    _is_str_list as _is_str_list,
    _load_config_schema as _load_config_schema,
    _schema_engine_errors as _schema_engine_errors,
    _schema_required_fields as _schema_required_fields,
    discover_components,
    find_config_file,
    load_config_file,
    validate_config,
)
from ._lockfile import (
    LOCKFILE_SCHEMA as LOCKFILE_SCHEMA,
    LOCKFILE_SCHEMA_URL as LOCKFILE_SCHEMA_URL,
    MigrationError,
    _generation_errors,
    _lockfile_schema_issues,
    _lockfile_structure_issues,
    _recompute_slice_entry as _recompute_slice_entry,
    generate_lockfile,
    generate_lockfile_for_components,
    load_lockfile_file,
    migrate_lockfile,
    verify_lockfile,
)
from ._diff import _summarize_change as _summarize_change
from ._diff import diff_lockfiles
from ._output import (
    _bold as _bold,
    _green,
    _is_tty as _is_tty,
    _log,
    _parse_components_arg,
    _print_json,
    _red,
    _yellow,
    analyze_explain_changes as analyze_explain_changes,
    configure_cli_streams,
    explain_component_changes,
    print_diff,
    print_status,
    safe_print as _safe_print,
    why_component,
)
from ._completions import (
    _BASH_COMPLETION as _BASH_COMPLETION,
    _COMPLETION_SCRIPTS,
    _FISH_COMPLETION as _FISH_COMPLETION,
    _ZSH_COMPLETION as _ZSH_COMPLETION,
)
from ._consumer_graph import resolve_slice_components
from .versions import (
    _extract_json_field as _extract_json_field,
    _extract_toml_field as _extract_toml_field,
    _extract_yaml_field as _extract_yaml_field,
    extract_version as extract_version,
    parse_semver as parse_semver,
    yaml as yaml,
)


# Command handlers are also exercised directly by embedders and tests, so use
# the same encoding-safe writer even when ``main()`` did not configure streams.
print = _safe_print


# ---------------------------------------------------------------------------
# Exit code constants
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_DRIFT = 1       # Gated drift detected
EXIT_USAGE = 2       # Usage/config error (missing file, parse error, unknown name)
EXIT_BEHAVIOR = 3    # Highest gated drift is behavioral
EXIT_BOUNDARY = 4    # Highest gated drift is boundary
EXIT_COMPAT = 5      # Compatibility-family drift


def _get_version() -> str:
    """Return the installed package version."""
    try:
        from importlib.metadata import version as _meta_version
        return _meta_version("boundver")
    except Exception:
        return "unknown"


def _resolve_allow_custom(args, config: dict) -> bool:
    """Return caller-controlled custom-provider authorization.

    Repository configuration is deliberately not an authorization boundary:
    checking out an untrusted branch must never be enough to import its Python.
    """
    return bool(getattr(args, "allow_custom_providers", False))


def _parse_facets_arg(raw: str, config: dict) -> List[str]:
    value = raw.strip() if isinstance(raw, str) else ""
    configured = config.get("defaults", {}).get("verify_facets", [])
    facets = [p.strip() for p in value.split(",") if p.strip()] if value else configured
    return sorted(set(facets or FACETS))


def _facet_policy_payload(
    config: dict, explicit_facets: Optional[List[str]]
) -> dict:
    """Describe effective component and slice gating without hiding overrides."""
    raw_defaults = config.get("defaults", {})
    configured_defaults = (
        raw_defaults.get("verify_facets")
        if isinstance(raw_defaults, dict) and "verify_facets" in raw_defaults
        else None
    )
    default_facets = (
        sorted(set(configured_defaults))
        if isinstance(configured_defaults, list)
        else None
    )
    components = config.get("components", {})
    effective_components = {
        name: sorted(
            set(
                explicit_facets
                if explicit_facets is not None
                else (
                    component["verify_facets"]
                    if isinstance(component.get("verify_facets"), list)
                    else (
                        default_facets
                        if default_facets is not None
                        else _available_component_facets(component)
                    )
                )
            )
        )
        for name, component in sorted(components.items())
        if isinstance(component, dict)
    }
    effective_slices = {}
    for name, definition in sorted(config.get("slices", {}).items()):
        if not isinstance(definition, dict):
            continue
        mode = definition.get("mode", "exact")
        members = resolve_slice_components(definition, components)
        gated = (
            mode in explicit_facets
            if explicit_facets is not None
            else any(mode in effective_components.get(member, []) for member in members)
        )
        effective_slices[name] = {"mode": mode, "gated": gated}
    return {
        "explicit": explicit_facets,
        "defaults": default_facets,
        "components": effective_components,
        "slices": effective_slices,
    }


def _drift_exit_code(issues: List[str]) -> int:
    """Return an exit code for the highest-severity gated fingerprint drift."""
    safety_prefixes = (
        "Config root", "LOCKFILE", "Custom provider loading failed",
        "Config invalid", "Config unavailable",
        "Verification error",
        "Unknown verification facet", "Unknown verification component",
        "CURRENT DIGEST ERROR",
        "LOCKED DIGEST ERROR",
        "UNAVAILABLE FACET",
    )
    if any(issue.startswith(safety_prefixes) for issue in issues):
        return EXIT_USAGE
    facets = {_issue_facet(issue) for issue in issues}
    if "compat" in facets:
        return EXIT_COMPAT
    if "boundary" in facets:
        return EXIT_BOUNDARY
    if "behavior" in facets:
        return EXIT_BEHAVIOR
    return EXIT_DRIFT


def _load_lockfile(
    path: Path,
    *,
    repo_root: Optional[Path] = None,
    snapshot: Optional[GitSourceSnapshot] = None,
) -> dict:
    return load_lockfile_file(path, repo_root=repo_root, snapshot=snapshot)


def _capture_operation_snapshot(
    repo_root: Path,
    source: str,
) -> Optional[GitSourceSnapshot]:
    """Capture the one immutable Git source used by an operation."""
    if source not in SOURCE_MODE_SET:
        raise ConfigError(
            f"Unknown source mode {source!r}; expected head, index, or working-tree"
        )
    if source == "working-tree":
        return None
    try:
        return _capture_git_source_snapshot(repo_root, source)
    except ValueError as exc:
        raise ConfigError(f"Cannot capture {source} source: {exc}") from exc


def _require_valid_lockfile(lockfile: dict) -> None:
    issues = [
        *_lockfile_schema_issues(lockfile),
        *_lockfile_structure_issues(lockfile),
    ]
    if issues:
        raise LockfileError("Lockfile validation failed:\n" + "\n".join(issues))


def _verify_lock_preflight_issues(config: dict, lockfile: dict) -> List[str]:
    """Return lock-wide issues that must be checked before scoped verification.

    Component and slice membership are global lock invariants.  They cannot be
    skipped merely because a caller requested a subset of component
    fingerprints.
    """
    structural_issues = [
        *_lockfile_schema_issues(lockfile),
        *_lockfile_structure_issues(lockfile),
    ]
    issues = list(structural_issues)
    if structural_issues:
        return issues

    configured_project = config.get("project", "unknown")
    if lockfile.get("project") != configured_project:
        issues.append(
            f"METADATA MISMATCH project: lockfile={lockfile.get('project')!r} "
            f"current={configured_project!r}"
        )

    locked_names = (
        set(lockfile.get("components", {}))
        if isinstance(lockfile.get("components"), dict)
        else set()
    )
    configured_names = set(config.get("components", {}))
    if locked_names != configured_names:
        issues.append(
            "LOCKFILE component set differs from config: "
            f"locked={sorted(locked_names)} configured={sorted(configured_names)}"
        )

    locked_slices = (
        set(lockfile.get("slices", {}))
        if isinstance(lockfile.get("slices"), dict)
        else set()
    )
    configured_slices = set(config.get("slices", {}))
    if locked_slices != configured_slices:
        issues.append(
            "LOCKFILE slice set differs from config: "
            f"locked={sorted(locked_slices)} configured={sorted(configured_slices)}"
        )

    issues.extend(
        f"LOCKED DIGEST ERROR {message}"
        for message in _generation_errors(lockfile)
    )
    return issues


def _ensure_lock_outside_components(
    repo_root: Path, lock_path: Path, config: dict
) -> None:
    absolute_lock = lock_path if lock_path.is_absolute() else repo_root / lock_path
    for name, component in config.get("components", {}).items():
        if not isinstance(component, dict):
            continue
        component_path = component.get("path")
        if not isinstance(component_path, str):
            continue
        component_root = repo_root / os.path.normpath(component_path.strip())
        try:
            absolute_lock.resolve().relative_to(component_root.resolve())
        except (ValueError, OSError):
            continue
        raise ConfigError(
            f"Lockfile path {absolute_lock} is inside component '{name}' "
            f"({component_path}); choose an output outside every component to "
            "avoid self-referential fingerprints"
        )


def _ensure_json_mutation_path(path: Path, command: str) -> None:
    if path.suffix.lower() != ".json":
        raise ConfigError(
            f"`boundver {command}` only writes JSON configs. "
            "Use boundary.config.json or edit your YAML/TOML config directly."
        )


def _write_text_atomic(path: Path, text: str) -> None:
    """Replace *path* only after the complete UTF-8 payload is durable.

    The temporary file lives beside the target so ``os.replace`` is atomic on
    the target filesystem.  This prevents an interrupted generation/update
    from leaving a truncated lockfile behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: Optional[int] = None
    if os.name != "nt":
        try:
            existing_mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            pass
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        if existing_mode is not None:
            os.fchmod(fd, existing_mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        if os.name != "nt":
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _run_cli_handler(command: str, handler, *handler_args) -> None:
    """Run a command with operational failures converted to usage errors."""
    try:
        handler(*handler_args)
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        print(f"ERROR: {command} failed: {detail}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def _cmd_completions(args) -> None:
    sys.stdout.write(_COMPLETION_SCRIPTS.get(args.shell, ""))


def _cmd_migrate_lock(args) -> None:
    lock_path = Path(args.lock)
    if not lock_path.is_absolute():
        lock_path = Path.cwd() / args.lock
    if not lock_path.exists():
        print(f"error: lockfile not found: {lock_path}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        old_lock = _load_lockfile(lock_path)
    except LockfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        migrated = migrate_lockfile(old_lock)
    except MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    out = _bounded_json_dumps(migrated, indent=2, sort_keys=False) + "\n"
    if args.dry_run:
        sys.stdout.write(out)
    else:
        _write_text_atomic(lock_path, out)
        if migrated.get("schema") == old_lock.get("schema"):
            _log("Already at current schema, no changes written.")
        else:
            _log(f"Migrated {lock_path} -> schema {migrated['schema']}")


def _cmd_diff(args) -> None:
    for label, fpath in (("old", args.old), ("new", args.new)):
        if not Path(fpath).exists():
            print(f"ERROR: {label} lockfile not found: {fpath}", file=sys.stderr)
            sys.exit(EXIT_USAGE)
    try:
        old = _load_lockfile(Path(args.old))
        new = _load_lockfile(Path(args.new))
        _require_valid_lockfile(old)
        _require_valid_lockfile(new)
    except LockfileError as exc:
        print(f"ERROR: Invalid lockfile: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    result = diff_lockfiles(old, new)
    if args.format == "json":
        _print_json(result)
    else:
        print_diff(result)


def _cmd_generate(args, repo_root: Path) -> None:
    try:
        snapshot = _capture_operation_snapshot(repo_root, args.source)
        config_path = find_config_file(
            repo_root, args.config, snapshot=snapshot
        )
        config = load_config_file(
            config_path, repo_root=repo_root, snapshot=snapshot
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    allow_custom = _resolve_allow_custom(args, config)
    config_errors = validate_config(
        config,
        repo_root,
        allow_custom_providers=allow_custom,
        source=args.source,
        snapshot=snapshot,
    )
    if config_errors:
        print(f"ERROR: Config is invalid ({len(config_errors)} issues):", file=sys.stderr)
        for error in config_errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        _ensure_lock_outside_components(repo_root, repo_root / args.out, config)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    # Warn when --source head but working tree has uncommitted changes
    if args.source == "head" and not args.quiet:
        comp_paths = [c.get("path", "") for c in config.get("components", {}).values()]
        try:
            dirty = dirty_component_paths(repo_root, comp_paths)
        except (OSError, ValueError) as exc:
            # This check is advisory: generation still reads the immutable
            # captured HEAD tree. A malformed or oversized status listing must
            # not turn that safe operation into an unhandled traceback.
            dirty = []
            print(
                _yellow(
                    "WARNING: Could not inspect uncommitted component changes: "
                    f"{exc}"
                ),
                file=sys.stderr,
            )
        if dirty:
            print(
                _yellow("WARNING: Using --source head but these components have uncommitted changes:"),
                file=sys.stderr,
            )
            for d in dirty:
                print(f"  {d}", file=sys.stderr)
            print(
                "  Fingerprints will reflect the last commit, not your working tree.",
                file=sys.stderr,
            )
            print(
                "  Use --source working-tree to include local changes.",
                file=sys.stderr,
            )
    components_filter = _parse_components_arg(args.components)
    try:
        if components_filter:
            lockfile = generate_lockfile_for_components(
                config,
                repo_root,
                selected_components=components_filter,
                out_path=repo_root / args.out,
                source=args.source,
                strict=(not args.allow_partial),
                allow_custom_providers=allow_custom,
                snapshot=snapshot,
            )
        else:
            lockfile = generate_lockfile(
                config, repo_root, source=args.source, strict=(not args.allow_partial),
                allow_custom_providers=allow_custom,
                snapshot=snapshot,
            )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("Run: boundver validate-config", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    out_path = repo_root / args.out
    if args.dry_run:
        _log(f"Dry run: lockfile not written ({out_path})", quiet=(args.quiet or args.format == "json"))
    else:
        _write_text_atomic(out_path, _bounded_json_dumps(lockfile, indent=2) + "\n")
        _log(f"Generated {out_path}", quiet=(args.quiet or args.format == "json"))
    if args.verbose and not args.quiet and args.format != "json":
        print(f"Generation source={args.source} strict={not args.allow_partial}")
    if args.format == "json":
        _print_json(lockfile)
    elif not args.quiet:
        print_status(lockfile)
    # First-run hint: if source=head and all components have exact_errors,
    # the user likely hasn't committed yet.
    if args.source == "head" and not args.quiet and args.format != "json":
        comps = lockfile.get("components", {})
        if comps and all(c.get("exact_errors") for c in comps.values()):
            print(
                _yellow("\nHint: All components have errors at HEAD. "
                        "If you haven't committed yet, try:\n"
                        "  boundver generate --source working-tree")
            )


def _print_verify_json(
    *,
    ok: bool,
    updated: bool,
    issues: List[str],
    resolved_issues: List[str],
    observations: List[str],
    facets: Optional[List[str]],
    facet_policy: dict,
    components_filter: List[str],
    changed_components: List[str],
) -> None:
    """Print the stable structured result for every verify outcome."""
    _print_json(
        {
            "ok": ok,
            "updated": updated,
            "issues": issues,
            "resolved_issues": resolved_issues,
            "observations": observations,
            "facets": facets,
            "facet_policy": facet_policy,
            "components_filter": components_filter,
            "changed_components": changed_components,
        }
    )


def _cmd_verify(args, repo_root: Path) -> None:
    try:
        snapshot = _capture_operation_snapshot(repo_root, args.source)
        config_path = find_config_file(
            repo_root, args.config, snapshot=snapshot
        )
        config = load_config_file(
            config_path, repo_root=repo_root, snapshot=snapshot
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    allow_custom = _resolve_allow_custom(args, config)
    config_errors = validate_config(
        config,
        repo_root,
        allow_custom_providers=allow_custom,
        source=args.source,
        snapshot=snapshot,
    )
    if config_errors:
        print(f"ERROR: Config is invalid ({len(config_errors)} issues):", file=sys.stderr)
        for error in config_errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    lock_path = repo_root / args.lock
    try:
        _ensure_lock_outside_components(repo_root, lock_path, config)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        lockfile = _load_lockfile(
            lock_path, repo_root=repo_root, snapshot=snapshot
        )
    except (FileNotFoundError, LockfileError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    components_filter = _parse_components_arg(args.components)
    requested_components_filter = list(components_filter)
    unknown = [
        name for name in components_filter
        if name not in config.get("components", {})
    ]
    if unknown:
        print(
            f"ERROR: unknown --components entries: {', '.join(unknown)}",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)
    gated_facets = _parse_facets_arg(args.facets, config)
    explicit_gated_facets = gated_facets if args.facets.strip() else None
    facet_policy = _facet_policy_payload(config, explicit_gated_facets)
    unknown_facets = sorted(
        set(gated_facets) - FACET_SET
    )
    if unknown_facets:
        print(
            f"ERROR: unknown --facets entries: {', '.join(unknown_facets)}",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)
    structural_issues = [
        *_lockfile_schema_issues(lockfile),
        *_lockfile_structure_issues(lockfile),
    ]
    preflight_issues = _verify_lock_preflight_issues(config, lockfile)
    changed_components: List[str] = []
    scoped_preflight_update = (
        bool(preflight_issues)
        and args.update
        and not structural_issues
        and bool(requested_components_filter)
        and not args.changed_from
    )
    if scoped_preflight_update:
        preflight_issues.append(
            "LOCKFILE scoped --update cannot repair global component or slice "
            "preflight issues; rerun without --components after reviewing the "
            "full lock"
        )
    if (
        preflight_issues
        and args.update
        and not structural_issues
        and not scoped_preflight_update
    ):
        try:
            # Preflight invariants cover the complete lock.  A scoped repair
            # could leave a missing unselected component or slice unresolved.
            updated = generate_lockfile(
                config, repo_root, source=args.source, strict=True,
                allow_custom_providers=allow_custom,
                snapshot=snapshot,
            )
        except ValueError as exc:
            print(f"ERROR: update failed: {exc}", file=sys.stderr)
            sys.exit(EXIT_USAGE)
        _write_text_atomic(lock_path, _bounded_json_dumps(updated, indent=2) + "\n")
        if args.format == "json":
            _print_verify_json(
                ok=True,
                updated=True,
                issues=[],
                resolved_issues=preflight_issues,
                observations=[],
                facets=explicit_gated_facets,
                facet_policy=facet_policy,
                components_filter=components_filter,
                changed_components=changed_components,
            )
        elif not args.quiet:
            print(_green(f"Updated {lock_path} after successful preflight repair."))
        return
    if preflight_issues:
        if args.format == "json":
            _print_verify_json(
                ok=False,
                updated=False,
                issues=preflight_issues,
                resolved_issues=[],
                observations=[],
                facets=explicit_gated_facets,
                facet_policy=facet_policy,
                components_filter=components_filter,
                changed_components=changed_components,
            )
        else:
            print("ERROR: lockfile preflight failed:", file=sys.stderr)
            for issue in preflight_issues:
                print(f"  - {issue}", file=sys.stderr)
        sys.exit(_drift_exit_code(preflight_issues))
    if args.changed_from:
        try:
            auto = changed_components_since_ref(
                config,
                repo_root,
                args.changed_from,
                source=args.source,
                snapshot=snapshot,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(EXIT_USAGE)
        if components_filter:
            auto_set = set(auto)
            components_filter = [c for c in components_filter if c in auto_set]
        else:
            components_filter = auto
        # A path diff is only a scheduling hint, not proof that unselected lock
        # entries are internally current. Recompute every component so stale
        # metadata, provider upgrades, and digest errors cannot pass merely
        # because another component happened to be selected. Keep the resolved
        # selection in JSON for observability.
        if args.verbose and not args.quiet and args.format != "json":
            if components_filter:
                print(
                    "Changed component paths: " + ", ".join(components_filter)
                    + "; validating full lock integrity."
                )
            else:
                print("No component paths changed; validating full lock integrity.")
        changed_components = list(components_filter)
        reported_components_filter = []
        components_filter = []
    else:
        reported_components_filter = list(components_filter)
    observations: List[str] = []
    try:
        issues = verify_lockfile(
            config, lockfile, repo_root, source=args.source, components_filter=components_filter,
            allow_custom_providers=allow_custom,
            fail_fast=getattr(args, "fail_fast", False),
            facets=explicit_gated_facets,
            observations=observations,
            snapshot=snapshot,
            transitive_consumers=args.transitive,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    if issues:
        unavailable_issues = [
            issue for issue in issues
            if issue.startswith("UNAVAILABLE FACET ")
        ]
        if args.format != "json" and not args.quiet:
            print(_red(f"LOCKFILE OUT OF DATE ({len(issues)} issues):") + "\n")
            for issue in issues:
                print(f"  {issue}")
            if observations:
                print(_yellow("\nNON-GATING DRIFT:"))
                for observation in observations:
                    print(f"  {observation}")
            if not args.update:
                print("\nInspect with `boundver why <component>`, then run `boundver verify --update` after review.")
        # Regeneration cannot manufacture a facet that the configuration does
        # not define. Treat this as a controlled policy/input error and leave
        # the reviewed lock bytes untouched even when --update was requested.
        if args.update and unavailable_issues:
            if args.format == "json":
                _print_verify_json(
                    ok=False,
                    updated=False,
                    issues=issues,
                    resolved_issues=[],
                    observations=observations,
                    facets=explicit_gated_facets,
                    facet_policy=facet_policy,
                    components_filter=reported_components_filter,
                    changed_components=changed_components,
                )
            sys.exit(EXIT_USAGE)
        if args.update:
            try:
                if requested_components_filter and not args.changed_from:
                    updated = generate_lockfile_for_components(
                        config,
                        repo_root,
                        selected_components=requested_components_filter,
                        out_path=lock_path,
                        source=args.source,
                        strict=True,
                        allow_custom_providers=allow_custom,
                        snapshot=snapshot,
                        existing_lockfile=lockfile,
                    )
                else:
                    updated = generate_lockfile(
                        config, repo_root, source=args.source, strict=True,
                        allow_custom_providers=allow_custom,
                        snapshot=snapshot,
                    )
            except ValueError as exc:
                print(f"ERROR: update failed: {exc}", file=sys.stderr)
                sys.exit(EXIT_USAGE)
            _write_text_atomic(lock_path, _bounded_json_dumps(updated, indent=2) + "\n")
            if args.format == "json":
                _print_verify_json(
                    ok=True,
                    updated=True,
                    issues=[],
                    resolved_issues=issues,
                    observations=observations,
                    facets=explicit_gated_facets,
                    facet_policy=facet_policy,
                    components_filter=reported_components_filter,
                    changed_components=changed_components,
                )
            if args.format != "json" and not args.quiet:
                print(_green(f"Updated {lock_path} after successful generation."))
            return
        if args.format == "json":
            _print_verify_json(
                ok=False,
                updated=False,
                issues=issues,
                resolved_issues=[],
                observations=observations,
                facets=explicit_gated_facets,
                facet_policy=facet_policy,
                components_filter=reported_components_filter,
                changed_components=changed_components,
            )
        sys.exit(_drift_exit_code(issues))
    else:
        if args.update and observations:
            try:
                if requested_components_filter and not args.changed_from:
                    updated = generate_lockfile_for_components(
                        config,
                        repo_root,
                        selected_components=requested_components_filter,
                        out_path=lock_path,
                        source=args.source,
                        strict=True,
                        allow_custom_providers=allow_custom,
                        snapshot=snapshot,
                        existing_lockfile=lockfile,
                    )
                else:
                    updated = generate_lockfile(
                        config, repo_root, source=args.source, strict=True,
                        allow_custom_providers=allow_custom,
                        snapshot=snapshot,
                    )
            except ValueError as exc:
                print(f"ERROR: update failed: {exc}", file=sys.stderr)
                sys.exit(EXIT_USAGE)
            _write_text_atomic(lock_path, _bounded_json_dumps(updated, indent=2) + "\n")
            if args.format == "json":
                _print_verify_json(
                    ok=True,
                    updated=True,
                    issues=[],
                    resolved_issues=[],
                    observations=observations,
                    facets=explicit_gated_facets,
                    facet_policy=facet_policy,
                    components_filter=reported_components_filter,
                    changed_components=changed_components,
                )
            elif not args.quiet:
                print(_green(f"Updated {lock_path} after successful generation."))
            return
        if args.format == "json":
            _print_verify_json(
                ok=True,
                updated=False,
                issues=[],
                resolved_issues=[],
                observations=observations,
                facets=explicit_gated_facets,
                facet_policy=facet_policy,
                components_filter=reported_components_filter,
                changed_components=changed_components,
            )
        if args.verbose and not args.quiet and args.format != "json":
            print(_green(f"Verified source={args.source} with 0 issues."))
        elif args.format != "json" and not args.quiet:
            print(_green("Lockfile is up to date."))
            if observations:
                print(_yellow(f"Non-gating drift observed ({len(observations)}):"))
                for observation in observations:
                    print(f"  {observation}")


def _cmd_slice(args, repo_root: Path) -> None:
    lock_path = repo_root / args.lock
    if not lock_path.exists():
        print(f"ERROR: Lockfile not found: {lock_path} \u2014 run 'boundver generate' first.", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        lockfile = _load_lockfile(lock_path)
    except LockfileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        _require_valid_lockfile(lockfile)
    except LockfileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    sl = lockfile.get("slices", {}).get(args.name)
    if sl is None:
        print(f"ERROR: Slice '{args.name}' not found.", file=sys.stderr)
        print(f"Available: {', '.join(lockfile.get('slices', {}).keys())}")
        sys.exit(EXIT_USAGE)
    if args.format == "json":
        _print_json(
            {
                "name": args.name,
                "description": sl.get("description", ""),
                "mode": sl.get("mode", "exact"),
                "fingerprint": sl["fingerprint"],
                "components": [
                    {
                        "name": cname,
                        "version": lockfile.get("components", {})
                        .get(cname, {})
                        .get("version"),
                        "digest": sl.get("component_digests", {}).get(cname),
                    }
                    for cname in sl.get("components", [])
                ],
            }
        )
        return
    print(f"\n  Slice: {args.name}")
    print(f"  Mode: {sl.get('mode', 'exact')}")
    print(f"  Fingerprint: {sl['fingerprint']}")
    print("  Components:")
    for cname in sl.get("components", []):
        digest = sl.get("component_digests", {}).get(cname)
        comp = lockfile.get("components", {}).get(cname, {})
        ver = comp.get("version", "unversioned")
        print(f"    {cname} @ {ver}  ({_short(digest)})")


def _cmd_validate_config(args, repo_root: Path) -> None:
    config_path = find_config_file(repo_root, args.config)
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        config = load_config_file(config_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    allow_custom = _resolve_allow_custom(args, config)
    errors = validate_config(
        config, repo_root, allow_custom_providers=allow_custom
    )
    warnings = config_warnings(config, repo_root)
    if errors:
        print(_red(f"CONFIG INVALID ({len(errors)} issues):"))
        for err in errors:
            print(f"  - {err}")
        sys.exit(EXIT_USAGE)
    if warnings:
        print(_yellow(f"CONFIG WARNINGS ({len(warnings)}):"))
        for warning in warnings:
            print(f"  - {warning}")
    print(_green("Config is valid."))


def _cmd_init(args, repo_root: Path) -> None:
    config_path = repo_root / args.out
    try:
        _ensure_json_mutation_path(config_path, "init")
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    if config_path.exists() and not args.force:
        print(f"ERROR: Config already exists: {config_path}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        discovered = discover_components(repo_root) if args.discover else {}
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        print(f"ERROR: component discovery failed: {detail}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    if args.discover and not discovered:
        print(
            "ERROR: No tracked component could be discovered. Root manifests "
            "need a tracked src/, lib/, app/, or single Python package directory; "
            "otherwise run `boundver init` and edit the scaffold.",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)
    starter = {
        "$schema": "https://raw.githubusercontent.com/yzm1/boundver/v0.12.0/boundary.config.schema.json",
        "project": repo_root.name,
        "defaults": {"compat_mode": "major"},
        "components": discovered if args.discover else {
            "example-component": {
                "path": "src",
                "version_source": None,
                "boundary": {"provider": "implicit", "paths": []},
            }
        },
    }
    _write_text_atomic(config_path, _bounded_json_dumps(starter, indent=2) + "\n")
    print(f"Created {config_path} with {len(starter['components'])} component(s).")
    print("Next: review the config, then run `boundver validate-config` and `boundver generate`.")


def _cmd_add(args, repo_root: Path) -> None:
    config_path = find_config_file(repo_root, args.config)
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        print("Run: boundver init", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        _ensure_json_mutation_path(config_path, "add")
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        config = load_config_file(config_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    existing_errors = validate_config(config, repo_root)
    if existing_errors:
        print(
            f"ERROR: Config is invalid ({len(existing_errors)} issues):",
            file=sys.stderr,
        )
        for error in existing_errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    components = config.get("components", {})
    if args.name in components:
        print(f"ERROR: Component '{args.name}' already exists in config.", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    add_path = args.path.replace("\\", "/").rstrip("/")
    if not add_path or ".." in Path(add_path).parts or Path(add_path).is_absolute():
        print(f"ERROR: Invalid component path: {args.path!r}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    boundary_paths = [p.strip() for p in args.paths.split(",") if p.strip()] if args.paths else []
    components[args.name] = {
        "path": add_path,
        "version_source": None,
        "boundary": {"provider": args.provider, "paths": boundary_paths},
    }
    config["components"] = components
    config_errors = validate_config(config, repo_root)
    if config_errors:
        print(
            f"ERROR: Adding '{args.name}' would leave an invalid config:",
            file=sys.stderr,
        )
        for error in config_errors:
            print(f"  - {error}", file=sys.stderr)
        print(
            "Correct the component path, provider, boundary paths, or policy "
            "before retrying.",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)
    _write_text_atomic(config_path, _bounded_json_dumps(config, indent=2) + "\n")
    print(f"Added component '{args.name}' at path '{args.path}'")
    print(f"Run: boundver generate --components {args.name}")


def _cmd_remove(args, repo_root: Path) -> None:
    config_path = find_config_file(repo_root, args.config)
    if not config_path.exists():
        print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        _ensure_json_mutation_path(config_path, "remove")
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        config = load_config_file(config_path)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    existing_errors = validate_config(config, repo_root)
    if existing_errors:
        print(
            f"ERROR: Config is invalid ({len(existing_errors)} issues):",
            file=sys.stderr,
        )
        for error in existing_errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    components = config.get("components", {})
    if not isinstance(components, dict):
        print("ERROR: Config field 'components' must be an object.", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    if args.name not in components:
        print(f"ERROR: Component '{args.name}' not found in config.", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    del components[args.name]
    config["components"] = components
    # Explicit slices can drop the removed member automatically. A graph
    # closure seed or incoming consumer edge is semantic and must be repaired
    # deliberately rather than silently rewritten.
    for slice_def in config.get("slices", {}).values():
        if not isinstance(slice_def, dict):
            continue
        comp_list = slice_def.get("components", [])
        if isinstance(comp_list, list) and args.name in comp_list:
            comp_list.remove(args.name)

    config_errors = validate_config(config, repo_root)
    if config_errors:
        print(
            f"ERROR: Removing '{args.name}' would leave an invalid config:",
            file=sys.stderr,
        )
        for error in config_errors:
            print(f"  - {error}", file=sys.stderr)
        print(
            "Update incoming consumers, closure slices, or retain at least one "
            "component before retrying.",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)
    _write_text_atomic(config_path, _bounded_json_dumps(config, indent=2) + "\n")
    print(f"Removed component '{args.name}'")
    print("Run: boundver generate")


def _cmd_discover(args, repo_root: Path) -> None:
    try:
        discovered = discover_components(repo_root)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        print(f"ERROR: component discovery failed: {detail}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    payload = {"count": len(discovered), "components": discovered}
    if args.format == "json":
        _print_json(payload)
    else:
        print(f"Discovered {len(discovered)} components:")
        for name, comp in discovered.items():
            print(f"  - {name}: {comp['path']}")


def _cmd_status(args, repo_root: Path) -> None:
    lock_path = repo_root / args.lock
    try:
        snapshot = _capture_operation_snapshot(repo_root, args.source)
        lockfile = _load_lockfile(
            lock_path, repo_root=repo_root, snapshot=snapshot
        )
    except (FileNotFoundError, LockfileError, ConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    if lockfile is not None:
        structure_issues = [
            *_lockfile_schema_issues(lockfile),
            *_lockfile_structure_issues(lockfile),
        ]
        status_payload = {
            "lockfile": lockfile,
            "issues": structure_issues,
            "warnings": [],
        }
        if args.format != "json" and not args.quiet:
            if structure_issues:
                print(_red("LOCKFILE INVALID:"))
                for issue in structure_issues:
                    print(f"  {issue}")
            else:
                print_status(lockfile)
        # Collect component warnings
        has_warnings = False
        components = lockfile.get("components", {})
        if not isinstance(components, dict):
            components = {}
        for name, comp in components.items():
            if not isinstance(comp, dict):
                continue
            for w in comp.get("warnings", []):
                status_payload["warnings"].append(f"{name}: {w}")
                has_warnings = True
            for e in comp.get("boundary_errors", []):
                status_payload["warnings"].append(f"{name}: boundary {comp.get('boundary_status', 'unknown')} - {e}")
                has_warnings = True
            for error_field, label in (
                ("version_errors", "version"),
                ("behavior_errors", "behavior"),
                ("exact_errors", "exact"),
            ):
                for e in comp.get(error_field, []):
                    status_payload["warnings"].append(f"{name}: {label} error - {e}")
                    has_warnings = True
        # Also verify if config exists
        try:
            config_path = find_config_file(
                repo_root, args.config, snapshot=snapshot
            )
            config = load_config_file(
                config_path, repo_root=repo_root, snapshot=snapshot
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"WARNING: Config parse error: {exc}", file=sys.stderr)
            status_payload["issues"].append(f"Config unavailable: {exc}")
            config = None
        if config is not None and not structure_issues:
            allow_custom = _resolve_allow_custom(args, config)
            config_errors = validate_config(
                config,
                repo_root,
                allow_custom_providers=allow_custom,
                source=args.source,
                snapshot=snapshot,
            )
            if config_errors:
                issues = [f"Config invalid: {error}" for error in config_errors]
            else:
                try:
                    issues = verify_lockfile(
                        config, lockfile, repo_root, source=args.source,
                        allow_custom_providers=allow_custom,
                        snapshot=snapshot,
                    )
                except ValueError as exc:
                    issues = [f"Verification error: {exc}"]
            status_payload["issues"].extend(issues)
            if issues:
                if not args.quiet and args.format != "json":
                    print(f"\n  DRIFT DETECTED ({len(issues)} issues):")
                    for issue in issues:
                        print(f"    {issue}")
        if args.format == "json":
            _print_json(status_payload)
        # --strict: exit non-zero if any drift or warnings
        if getattr(args, "strict", False) and (status_payload["issues"] or has_warnings):
            sys.exit(_drift_exit_code(status_payload["issues"]))
    else:
        print(f"No lockfile found at {lock_path}. Run 'generate' first.")
        sys.exit(EXIT_USAGE)


def _cmd_explain(args, repo_root: Path) -> None:
    try:
        snapshot = _capture_operation_snapshot(repo_root, args.source)
        config_path = find_config_file(
            repo_root, args.config, snapshot=snapshot
        )
        config = load_config_file(
            config_path, repo_root=repo_root, snapshot=snapshot
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    allow_custom = _resolve_allow_custom(args, config)
    config_errors = validate_config(
        config,
        repo_root,
        allow_custom_providers=allow_custom,
        source=args.source,
        snapshot=snapshot,
    )
    if config_errors:
        print(
            f"ERROR: Config is invalid ({len(config_errors)} issues):",
            file=sys.stderr,
        )
        for error in config_errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    rc = explain_component_changes(
        config,
        repo_root,
        args.component,
        base_ref=args.base_ref,
        source=args.source,
        snapshot=snapshot,
    )
    if rc != 0:
        sys.exit(rc)


def _cmd_why(args, repo_root: Path) -> None:
    lock_path = repo_root / args.lock
    try:
        snapshot = _capture_operation_snapshot(repo_root, args.source)
        config_path = find_config_file(
            repo_root, args.config, snapshot=snapshot
        )
        config = load_config_file(
            config_path, repo_root=repo_root, snapshot=snapshot
        )
        lockfile = _load_lockfile(
            lock_path, repo_root=repo_root, snapshot=snapshot
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        _require_valid_lockfile(lockfile)
    except LockfileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    allow_custom = _resolve_allow_custom(args, config)
    config_errors = validate_config(
        config,
        repo_root,
        allow_custom_providers=allow_custom,
        source=args.source,
        snapshot=snapshot,
    )
    if config_errors:
        print(
            f"ERROR: Config is invalid ({len(config_errors)} issues):",
            file=sys.stderr,
        )
        for error in config_errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    rc = why_component(
        config,
        lockfile,
        repo_root,
        args.component,
        source=args.source,
        allow_custom_providers=allow_custom,
        snapshot=snapshot,
        transitive_consumers=args.transitive,
        output_format=args.format,
    )
    if rc != 0:
        sys.exit(rc)


def main():
    configure_cli_streams()
    # argparse normally accepts global options only before the subcommand.
    # Normalize the two verbosity flags so documented/completed post-command
    # forms work as well (for example `boundver status --quiet`).
    try:
        option_boundary = sys.argv.index("--", 2)
    except ValueError:
        option_boundary = len(sys.argv)
    for option in ("--quiet", "--verbose"):
        try:
            option_index = sys.argv.index(option, 2, option_boundary)
        except ValueError:
            continue
        sys.argv.pop(option_index)
        sys.argv.insert(1, option)
    parser = argparse.ArgumentParser(
        description="Detect declared component, behavior, boundary, and compatibility drift",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {_get_version()}")
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("--quiet", action="store_true", help="Reduce non-error human-readable output")
    verbosity.add_argument("--verbose", action="store_true", help="Print additional progress diagnostics")
    sub = parser.add_subparsers(dest="command")

    # generate
    gen = sub.add_parser(
        "generate",
        help="Generate or update the lockfile",
        epilog=(
            "Examples:\n"
            "  boundver generate\n"
            "  boundver generate --source working-tree\n"
            "  boundver generate --components auth-service,billing --dry-run\n"
            "  boundver generate --format json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    gen.add_argument("--config", default="boundary.config.json", help="Config file path")
    gen.add_argument("--out", default="boundary.lock.json", help="Output lockfile path")
    gen.add_argument(
        "--source", choices=SOURCE_MODES, default="head",
        help="Snapshot to fingerprint: last commit, staged index, or tracked files on disk",
    )
    gen.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow intentional null facet inputs in slices (not provider or source errors)",
    )
    gen.add_argument("--dry-run", action="store_true", help="Compute lockfile and print status without writing output")
    gen.add_argument("--components", default="", help="Comma-separated component names to regenerate")
    gen.add_argument("--format", choices=["json", "text"], default="text", help="Output format")
    gen.add_argument(
        "--allow-custom-providers", action="store_true",
        help="Allow loading external provider modules declared in the config 'providers' key "
             "(or set BOUNDVER_ALLOW_CUSTOM_PROVIDERS=1)",
    )
    ver = sub.add_parser(
        "verify",
        help="Check lockfile matches current repo state",
        description=(
            "Verify the lockfile is up to date with the current repo state.\n\n"
            "Exit codes:\n"
            "  0  Selected facets match\n"
            "  1  Exact or metadata drift\n"
            "  2  Usage, configuration, or digest error\n"
            "  3  Behavioral drift\n"
            "  4  Boundary drift\n"
            "  5  Compatibility-family drift\n"
        ),
        epilog=(
            "Examples:\n"
            "  boundver verify\n"
            "  boundver verify --components auth-service\n"
            "  boundver verify --changed-from main\n"
            "  boundver verify --source working-tree --format json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ver.add_argument("--config", default="boundary.config.json")
    ver.add_argument("--lock", default="boundary.lock.json")
    ver.add_argument("--source", choices=SOURCE_MODES, default="head",
                     help="Snapshot to compare: last commit, staged index, or tracked files on disk")
    ver.add_argument("--components", default="", help="Comma-separated component names to verify")
    ver.add_argument(
        "--changed-from", default="",
        help="Report components changed since a Git ref while verifying full lock integrity",
    )
    ver.add_argument("--fail-fast", action="store_true",
                     help="Report only the highest-severity mismatch")
    ver.add_argument(
        "--facets", default="",
        help=(
            "Comma-separated CLI-wide gate override (default: component "
            "verify_facets, then config defaults, then all available facets)"
        ),
    )
    ver.add_argument(
        "--transitive", action="store_true",
        help="Report the transitive downstream consumer closure for boundary/compat drift",
    )
    ver.add_argument(
        "--update", action="store_true",
        help="After reporting drift, atomically regenerate the lockfile if computation succeeds",
    )
    ver.add_argument("--format", choices=["json", "text"], default="text", help="Output format")
    ver.add_argument(
        "--allow-custom-providers", action="store_true",
        help="Allow loading external provider modules declared in the config 'providers' key",
    )

    # diff
    dif = sub.add_parser("diff", help="Diff two lockfiles")
    dif.add_argument("old", help="Old lockfile")
    dif.add_argument("new", help="New lockfile")
    dif.add_argument("--format", choices=["json", "text"], default="text", help="Output format")

    # slice
    sl = sub.add_parser("slice", help="Show fingerprint for a specific slice")
    sl.add_argument("name", help="Slice name")
    sl.add_argument("--lock", default="boundary.lock.json")
    sl.add_argument("--format", choices=["json", "text"], default="text", help="Output format")

    # validate-config (and check-config alias)
    vc = sub.add_parser(
        "validate-config",
        help="Validate config for strict boundary rules",
        epilog=(
            "Examples:\n"
            "  boundver validate-config\n"
            "  boundver validate-config --config custom-config.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    vc.add_argument("--config", default="boundary.config.json")
    vc.add_argument(
        "--allow-custom-providers", action="store_true",
        help="Allow loading external provider modules declared in the config 'providers' key",
    )
    cc = sub.add_parser("check-config", help="Alias for validate-config")
    cc.add_argument("--config", default="boundary.config.json")
    cc.add_argument(
        "--allow-custom-providers", action="store_true",
        help="Allow loading external provider modules declared in the config 'providers' key",
    )

    # init
    init = sub.add_parser(
        "init",
        help="Create a starter boundary.config.json",
        epilog=(
            "Examples:\n"
            "  boundver init\n"
            "  boundver init --discover  # auto-detect components from manifests\n"
            "\n"
            "After init, if you haven't committed yet, generate with:\n"
            "  boundver generate --source working-tree\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    init.add_argument("--out", default="boundary.config.json", help="Output config file path")
    init.add_argument("--force", action="store_true", help="Overwrite existing file")
    init.add_argument("--discover", action="store_true", help="Auto-discover components from common manifests")

    # add
    add = sub.add_parser("add", help="Add a component to the config")
    add.add_argument("name", help="Component name")
    add.add_argument("path", help="Component path relative to repo root")
    add.add_argument("--provider", default="implicit", help="Boundary provider (default: implicit)")
    add.add_argument("--paths", default="", help="Comma-separated boundary paths")
    add.add_argument("--config", default="boundary.config.json", help="Config file path")

    # remove
    rm = sub.add_parser("remove", help="Remove a component from the config")
    rm.add_argument("name", help="Component name to remove")
    rm.add_argument("--config", default="boundary.config.json", help="Config file path")

    # status
    st = sub.add_parser(
        "status",
        help="Show lockfile summary and warnings",
        epilog=(
            "Examples:\n"
            "  boundver status\n"
            "  boundver status --format json\n"
            "  boundver status --strict  # exit non-zero on drift (for CI)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    st.add_argument("--config", default="boundary.config.json")
    st.add_argument("--lock", default="boundary.lock.json")
    st.add_argument("--source", choices=SOURCE_MODES, default="head")
    st.add_argument("--format", choices=["json", "text"], default="text", help="Output format")
    st.add_argument("--strict", action="store_true",
                    help="Exit non-zero if any drift or warnings are detected (useful for CI)")
    st.add_argument(
        "--allow-custom-providers", action="store_true",
        help="Allow loading external provider modules declared in the config 'providers' key",
    )

    # explain
    ex = sub.add_parser("explain", help="Explain changed files for a component")
    ex.add_argument("component", help="Component name from config")
    ex.add_argument("--config", default="boundary.config.json")
    ex.add_argument("--base-ref", default="HEAD", help="Git ref to diff against (default: HEAD)")
    ex.add_argument("--source", choices=SOURCE_MODES, default="head",
                    help="Source to diff against base-ref (default: head)")
    ex.add_argument(
        "--allow-custom-providers", action="store_true",
        help="Allow loading external provider modules declared in the config 'providers' key",
    )

    why = sub.add_parser(
        "why",
        help="Explain why a component's lockfile is out of date",
        description=(
            "Compare current fingerprints against the lockfile and explain what changed.\n\n"
            "Shows which facets (exact/behavior/boundary/compat) drifted, what type of change\n"
            "it is, and which files are responsible.\n\n"
            "Exit codes:\n"
            "  0  Component is up to date\n"
            "  1  Component has drifted\n"
            "  2  Usage error (unknown component, missing config, etc.)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    why.add_argument("component", help="Component name from config")
    why.add_argument("--config", default="boundary.config.json")
    why.add_argument("--lock", default="boundary.lock.json")
    why.add_argument("--source", choices=SOURCE_MODES, default="head",
                     help="Fingerprint source to compare against (default: head)")
    why.add_argument("--format", choices=["json", "text"], default="text", help="Output format")
    why.add_argument(
        "--transitive", action="store_true",
        help="Report the transitive downstream consumer closure",
    )
    why.add_argument(
        "--allow-custom-providers", action="store_true",
        help="Allow loading external provider modules declared in the config 'providers' key",
    )

    disc = sub.add_parser("discover", help="Find Git-tracked component manifests")
    disc.add_argument("--format", choices=["json", "text"], default="text", help="Output format")

    # migrate-lock
    ml = sub.add_parser(
        "migrate-lock",
        help="Normalize a current lock or explain why regeneration is required",
        description=(
            "Normalize a supported current-schema boundary.lock.json in place. "
            "Hash-contract v1/v2 locks and v3 locks carrying semantic-config/v1 "
            "cannot be upgraded safely and are rejected with instructions to "
            "regenerate from repository content. Use --dry-run "
            "to print the normalized current lock without writing it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ml.add_argument("--lock", default="boundary.lock.json",
                    help="Path to lockfile (default: boundary.lock.json)")
    ml.add_argument("--dry-run", action="store_true",
                    help="Print normalized JSON to stdout without writing the file")

    # completions
    comp = sub.add_parser(
        "completions",
        help="Emit shell completion scripts",
        description=(
            "Print a shell completion script to stdout.\n\n"
            "Installation:\n"
            "  bash:  boundver completions --shell bash >> ~/.bash_completion\n"
            "  zsh:   boundver completions --shell zsh > ~/.zfunc/_boundver\n"
            "         (add ~/.zfunc to $fpath before compinit)\n"
            "  fish:  boundver completions --shell fish > ~/.config/fish/completions/boundver.fish\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    comp.add_argument("--shell", required=True, choices=["bash", "zsh", "fish"],
                      help="Target shell")

    args = parser.parse_args()

    # Honour the BOUNDVER_ALLOW_CUSTOM_PROVIDERS env var as a fallback so
    # automation pipelines don't need to thread the flag through every call site.
    if os.environ.get("BOUNDVER_ALLOW_CUSTOM_PROVIDERS", "").strip() in {"1", "true", "yes"}:
        if hasattr(args, "allow_custom_providers"):
            args.allow_custom_providers = True

    if args.command is None:
        parser.print_help()
        sys.exit(EXIT_USAGE)

    # Commands that don't need a git repo.
    if args.command == "completions":
        _run_cli_handler(args.command, _cmd_completions, args)
        return
    if args.command == "migrate-lock":
        _run_cli_handler(args.command, _cmd_migrate_lock, args)
        return
    if args.command == "diff":
        _run_cli_handler(args.command, _cmd_diff, args)
        return

    try:
        repo_root = git_root()
    except (subprocess.CalledProcessError, OSError):
        print(
            "ERROR: Not inside a git repository.\n"
            "\n"
            "boundver requires git history to compute fingerprints.\n"
            "Common fixes:\n"
            "  - In CI: set fetch-depth: 0 (GitHub Actions) or GIT_DEPTH: 0 (GitLab)\n"
            "  - In Docker: copy .git into the build context or mount it\n"
            "  - Locally: run from within a git-initialized directory (git init)\n",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)

    # Dispatch to command handlers.
    _COMMANDS = {
        "generate": _cmd_generate,
        "verify": _cmd_verify,
        "slice": _cmd_slice,
        "validate-config": _cmd_validate_config,
        "check-config": _cmd_validate_config,
        "init": _cmd_init,
        "add": _cmd_add,
        "remove": _cmd_remove,
        "discover": _cmd_discover,
        "status": _cmd_status,
        "explain": _cmd_explain,
        "why": _cmd_why,
    }
    handler = _COMMANDS.get(args.command)
    if handler:
        _run_cli_handler(args.command, handler, args, repo_root)


if __name__ == "__main__":
    main()
