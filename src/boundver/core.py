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

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict as Dict
from typing import List, Optional, Set

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
    SOURCE_MODE_SET,
    _effective_component_facets,
    _is_windows_reparse_point,
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
    dump_config,
    find_config_file,
    load_config_file,
    validate_config,
)
from ._lockfile import (
    DIFFABLE_SEMANTIC_CONFIG_VERSIONS,
    LOCKFILE_SCHEMA as LOCKFILE_SCHEMA,
    LOCKFILE_SCHEMA_URL as LOCKFILE_SCHEMA_URL,
    MigrationError,
    _generation_errors,
    _lockfile_schema_issues,
    _lockfile_structure_issues,
    _recompute_slice_entry as _recompute_slice_entry,
    generate_lockfile,
    generate_lockfile_for_components,
    dump_lockfile,
    load_lockfile_file,
    migrate_lockfile,
    verify_lockfile,
)
from ._diff import _summarize_change as _summarize_change
from ._diff import diff_lockfiles, require_compatible_lockfile_schemas
from ._output import (
    _bold as _bold,
    _display_path,
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
from ._cli_parser import build_parser
from ._consumer_graph import resolve_slice_components
from ._discovery import compare_discovery_to_config, normalize_discovery_exclusions
from ._migration_analysis import analyze_selector_migration
from ._baseline import (
    BaselineError,
    apply_baseline,
    baseline_change_ids,
    baseline_context,
    create_baseline,
    dump_baseline,
    load_baseline,
    load_baseline_with_bytes,
    replace_baseline_if_unchanged,
    write_baseline_create_only,
)
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
EXIT_DRIFT = 1  # Gated drift detected
EXIT_USAGE = 2  # Usage/config error (missing file, parse error, unknown name)
EXIT_BEHAVIOR = 3  # Highest gated drift is behavioral
EXIT_BOUNDARY = 4  # Highest gated drift is boundary
EXIT_COMPAT = 5  # Compatibility-family drift


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


def _facet_policy_payload(config: dict, explicit_facets: Optional[List[str]]) -> dict:
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
        name: sorted(_effective_component_facets(config, name, explicit_facets))
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
        "Config root",
        "LOCKFILE",
        "Custom provider loading failed",
        "Config invalid",
        "Config unavailable",
        "Verification error",
        "Unknown verification facet",
        "Unknown verification component",
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


def _operation_input_provenance(
    repo_root: Path,
    source: str,
    snapshot: Optional[GitSourceSnapshot],
    *,
    config_path: Path,
    lock_path: Path,
) -> dict:
    """Describe the exact config/lock view selected for one operation."""
    absolute_root = Path(os.path.abspath(repo_root))

    def path_label(path: Path) -> str:
        absolute_path = Path(os.path.abspath(path))
        try:
            return absolute_path.relative_to(absolute_root).as_posix()
        except ValueError:
            return absolute_path.as_posix()

    if source == "head" and snapshot is not None:
        selector = f"HEAD@{snapshot.head_oid or snapshot.tree_oid}"
    elif source == "index" and snapshot is not None:
        selector = f"INDEX@{snapshot.tree_oid}"
    else:
        selector = "WORKING-TREE"
    return {
        "source": source,
        "tree": snapshot.tree_oid if snapshot is not None else None,
        "commit": snapshot.head_oid if snapshot is not None else None,
        "config": f"{selector}:{path_label(config_path)}",
        "lock": f"{selector}:{path_label(lock_path)}",
    }


def _require_valid_lockfile(
    lockfile: dict,
    *,
    allowed_config_contracts: Optional[Set[str]] = None,
) -> None:
    schema_issues = _lockfile_schema_issues(lockfile)
    if schema_issues:
        raise LockfileError("Lockfile validation failed:\n" + "\n".join(schema_issues))
    issues = _lockfile_structure_issues(
        lockfile,
        allowed_config_contracts=allowed_config_contracts,
        running_version=_get_version(),
    )
    if issues:
        raise LockfileError("Lockfile validation failed:\n" + "\n".join(issues))


def _require_diffable_lockfile(lockfile: dict) -> None:
    """Validate one lock for the explicitly read-only historical diff path."""
    _require_valid_lockfile(
        lockfile,
        allowed_config_contracts=set(DIFFABLE_SEMANTIC_CONFIG_VERSIONS),
    )


def _verify_lock_preflight_issues(config: dict, lockfile: dict) -> List[str]:
    """Return lock-wide issues that must be checked before scoped verification.

    Component and slice membership are global lock invariants.  They cannot be
    skipped merely because a caller requested a subset of component
    fingerprints.
    """
    structural_issues = [
        *_lockfile_schema_issues(lockfile),
        *_lockfile_structure_issues(lockfile, running_version=_get_version()),
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
        f"LOCKED DIGEST ERROR {message}" for message in _generation_errors(lockfile)
    )
    return issues


def _ensure_lock_outside_components(
    repo_root: Path,
    lock_path: Path,
    config: dict,
    *,
    config_path: Path,
) -> None:
    """Reject lock paths that can overwrite or fingerprint themselves.

    Compare both lexical absolute paths and filesystem-resolved paths.  The
    lexical comparison preserves the selected Git view when a working-tree
    symlink differs from ``head`` or ``index``; the resolved comparison catches
    aliases through symlinks, junctions, and other reparse points.
    """

    def normalized_paths(path: Path, label: str) -> tuple[Path, Path]:
        absolute = path if path.is_absolute() else repo_root / path

        def resolve_before_parents(candidate: Path) -> Path:
            """Resolve each prefix before interpreting a following ``..``."""
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            parts = candidate.parts
            if not parts or not candidate.anchor:
                raise ConfigError(
                    f"Cannot safely resolve {label} path {candidate}: "
                    "path has no absolute filesystem anchor"
                )
            current = Path(candidate.anchor)
            for part in parts[1:]:
                if part == "..":
                    current = current.resolve(strict=False).parent
                else:
                    current = current / part
            return current.resolve(strict=False)

        try:
            # Filesystems may follow a symlink before interpreting a later
            # ``..`` segment.  Path.resolve() and abspath() can collapse the
            # parent first on some platforms, validating a different
            # destination from the one opened by the writer.
            resolved = resolve_before_parents(absolute)
        except (OSError, RuntimeError) as exc:
            raise ConfigError(
                f"Cannot safely resolve {label} path {absolute}: {exc}"
            ) from exc
        lexical = Path(os.path.abspath(absolute))
        return lexical, resolved

    def is_within(candidate: tuple[Path, Path], root: tuple[Path, Path]) -> bool:
        for candidate_path, root_path in zip(candidate, root):
            try:
                candidate_path.relative_to(root_path)
            except (ValueError, OSError):
                continue
            return True
        return False

    lock_paths = normalized_paths(lock_path, "lockfile")
    config_paths = normalized_paths(config_path, "selected config")
    try:
        same_existing_file = lock_paths[1].samefile(config_paths[1])
    except OSError:
        same_existing_file = False
    if same_existing_file or any(
        lock == selected for lock, selected in zip(lock_paths, config_paths)
    ):
        raise ConfigError(
            f"Lockfile path {lock_paths[0]} aliases the selected config "
            f"{config_paths[0]}; choose a different output path"
        )

    for name, component in config.get("components", {}).items():
        if not isinstance(component, dict):
            continue
        component_path = component.get("path")
        if not isinstance(component_path, str):
            continue
        protected_roots = [("component", component_path)]
        vendored = component.get("vendored_copies", [])
        if isinstance(vendored, list):
            protected_roots.extend(
                ("vendored copy", path)
                for path in vendored
                if isinstance(path, str)
            )
        for root_kind, root_path in protected_roots:
            protected_paths = normalized_paths(
                repo_root / os.path.normpath(root_path.strip()),
                f"{root_kind} root",
            )
            if not is_within(lock_paths, protected_paths):
                continue
            raise ConfigError(
                f"Lockfile path {lock_paths[0]} is inside {root_kind} root "
                f"'{root_path}' declared by component '{name}'; choose an output "
                "outside every component and vendored-copy root to avoid "
                "self-referential fingerprints"
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


def _write_lockfile_atomic(path: Path, value: dict) -> None:
    """Serialize a readable lock completely before opening a temp file."""
    _write_text_atomic(path, dump_lockfile(value))


def _write_config_atomic(path: Path, value: dict) -> None:
    """Serialize a readable config completely before opening a temp file."""
    _write_text_atomic(path, dump_config(value))


def _resolve_baseline_path(repo_root: Path, raw_path: str) -> Path:
    """Return a normalized lexical baseline path inside the repository.

    Keeping the lexical path ensures an unstaged symlink cannot select
    a different committed/staged baseline entry for a head/index operation.
    """
    lexical_root = Path(os.path.abspath(repo_root))
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = lexical_root / candidate
    lexical_candidate = Path(os.path.abspath(candidate))
    try:
        lexical_candidate.relative_to(lexical_root)
    except (OSError, ValueError) as exc:
        raise BaselineError(
            "verification baseline paths must stay within the repository"
        ) from exc
    if lexical_candidate.suffix.lower() != ".json":
        raise BaselineError("verification baseline path must end in .json")
    return lexical_candidate


def _ensure_baseline_outside_components(
    repo_root: Path, baseline_path: Path, config: dict
) -> None:
    for name, component in config.get("components", {}).items():
        if not isinstance(component, dict):
            continue
        component_path = component.get("path")
        if not isinstance(component_path, str):
            continue
        protected_roots = [("component", component_path)]
        vendored = component.get("vendored_copies", [])
        if isinstance(vendored, list):
            protected_roots.extend(
                ("vendored copy", path) for path in vendored if isinstance(path, str)
            )
        for root_kind, root_path in protected_roots:
            protected_root = Path(
                os.path.abspath(repo_root / os.path.normpath(root_path.strip()))
            )
            try:
                baseline_path.relative_to(protected_root)
            except (ValueError, OSError):
                continue
            raise BaselineError(
                f"verification baseline {baseline_path} is inside {root_kind} "
                f"root '{root_path}' declared by component '{name}'; choose a "
                "repository path outside every hashed component or vendored-copy "
                "root to avoid self-referential drift"
            )


def _ensure_plain_baseline_disk_path(repo_root: Path, baseline_path: Path) -> None:
    """Reject live symlink/reparse traversal for disk reads and writes."""
    lexical_root = Path(os.path.abspath(repo_root))
    try:
        relative = baseline_path.relative_to(lexical_root)
    except ValueError as exc:
        raise BaselineError(
            "verification baseline paths must stay within the repository"
        ) from exc
    current = lexical_root
    for part in relative.parts[:-1]:
        current = current / part
        try:
            identity = current.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(identity.st_mode) or _is_windows_reparse_point(identity):
            raise BaselineError(
                "verification baseline path must not traverse a symlink, "
                f"junction, or reparse-point ancestor: {baseline_path}"
            )
    try:
        identity = baseline_path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(identity.st_mode) or _is_windows_reparse_point(identity):
        raise BaselineError(
            "verification baseline path must be a regular file, not a symlink, "
            f"junction, or reparse point: {baseline_path}"
        )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _run_cli_handler(command: str, handler, *handler_args) -> None:
    """Run a command with operational failures converted to usage errors."""
    try:
        handler(*handler_args)
    except (OSError, subprocess.CalledProcessError, BoundverError) as exc:
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
    migration_reason = None
    try:
        migrated = migrate_lockfile(old_lock)
    except MigrationError as exc:
        if not args.explain:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(EXIT_USAGE)
        migrated = None
        migration_action = "regenerate"
        migration_reason = str(exc)[:4096]
    else:
        migration_action = "none" if migrated == old_lock else "normalize"
    if args.explain:
        try:
            repo_root = git_root()
            snapshot = _capture_operation_snapshot(repo_root, args.source)
            analysis_snapshot = snapshot
            if args.source == "working-tree":
                tracked_snapshot = _capture_git_source_snapshot(repo_root, "index")
                if tracked_snapshot.entries or tracked_snapshot.head_oid is not None:
                    visible_entries = {
                        path: entry
                        for path, entry in tracked_snapshot.entries.items()
                        if (repo_root / path).exists()
                        or (repo_root / path).is_symlink()
                    }
                    analysis_snapshot = GitSourceSnapshot(
                        source="working-tree",
                        tree_oid=tracked_snapshot.tree_oid,
                        entries=visible_entries,
                        head_oid=tracked_snapshot.head_oid,
                        filemode=tracked_snapshot.filemode,
                    )
            config_path = find_config_file(repo_root, args.config, snapshot=snapshot)
            config = load_config_file(
                config_path, repo_root=repo_root, snapshot=snapshot
            )
            lock_label = Path(args.lock).as_posix()
            if len(lock_label) > 4096:
                raise ConfigError("Lockfile path exceeds the diagnostic limit")
            analysis = analyze_selector_migration(
                config,
                repo_root,
                source=args.source,
                snapshot=analysis_snapshot,
                lock_path=lock_label,
                lock_schema=old_lock.get("schema"),
                migration_action=migration_action,
                migration_reason=migration_reason,
            )
        except (FileNotFoundError, ValueError, ConfigError, GuardrailError) as exc:
            print(f"error: migration analysis failed: {exc}", file=sys.stderr)
            sys.exit(EXIT_USAGE)
        if args.format == "json":
            _print_json(analysis)
        else:
            summary = analysis["summary"]
            print(
                "Migration selector analysis "
                f"({analysis['source']['mode']}): "
                f"{summary['declaration_count']} declaration(s), "
                f"{summary['changed_declaration_count']} changed, "
                f"{summary['uncompared_declaration_count']} not comparable"
            )
            print(f"Lock action: {analysis['lock']['action']}")
            if analysis["lock"]["reason"]:
                print(f"  {analysis['lock']['reason']}")
            changed_declarations = [
                declaration
                for declaration in analysis["declarations"]
                if declaration["analysis_status"] == "compared"
                and declaration["impact"] != "unchanged"
            ]
            uncompared_declarations = [
                declaration
                for declaration in analysis["declarations"]
                if declaration["analysis_status"] != "compared"
            ]
            unchanged_count = summary["compared_declaration_count"] - len(
                changed_declarations
            )
            if unchanged_count:
                print(f"Unchanged comparable declarations: {unchanged_count}")
            for declaration in uncompared_declarations:
                print(
                    f"  {declaration['component']}.{declaration['facet']} "
                    f"{declaration['selector']!r}: not comparable "
                    f"({declaration['analysis_status']})"
                )
                print(f"    {declaration['detail']}")
            if not changed_declarations:
                print("No comparable selector match sets changed.")
            for declaration in changed_declarations:
                print(
                    f"  {declaration['component']}.{declaration['facet']} "
                    f"{declaration['selector']!r}: {declaration['impact']} "
                    f"(v0.10={declaration['legacy_match_count']}, "
                    f"current={declaration['current_match_count']})"
                )
                for path in declaration["legacy_only_examples"]:
                    print(f"    - legacy only: {path}")
                if declaration["legacy_only_omitted"]:
                    print(
                        f"    - legacy only: +{declaration['legacy_only_omitted']} more"
                    )
                for path in declaration["current_only_examples"]:
                    print(f"    + current only: {path}")
                if declaration["current_only_omitted"]:
                    print(
                        "    + current only: "
                        f"+{declaration['current_only_omitted']} more"
                    )
        return
    assert migrated is not None
    out = dump_lockfile(migrated)
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
        require_compatible_lockfile_schemas(old, new)
        _require_diffable_lockfile(old)
        _require_diffable_lockfile(new)
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
        config_path = find_config_file(repo_root, args.config, snapshot=snapshot)
        config = load_config_file(config_path, repo_root=repo_root, snapshot=snapshot)
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
        require_slice_facets=not args.allow_partial,
    )
    if config_errors:
        print(
            f"ERROR: Config is invalid ({len(config_errors)} issues):", file=sys.stderr
        )
        for error in config_errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        _ensure_lock_outside_components(
            repo_root,
            repo_root / args.out,
            config,
            config_path=config_path,
        )
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
                    f"WARNING: Could not inspect uncommitted component changes: {exc}"
                ),
                file=sys.stderr,
            )
        if dirty:
            print(
                _yellow(
                    "WARNING: Using --source head but these components have uncommitted changes:"
                ),
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
                running_version=_get_version(),
            )
        else:
            lockfile = generate_lockfile(
                config,
                repo_root,
                source=args.source,
                strict=(not args.allow_partial),
                allow_custom_providers=allow_custom,
                snapshot=snapshot,
            )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "Review the reported provider, source, or facet error. Use "
            "--allow-partial only when null slice facet inputs are intentional.",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)
    out_path = repo_root / args.out
    if args.dry_run:
        _log(
            f"Dry run: lockfile not written ({out_path})",
            quiet=(args.quiet or args.format == "json"),
        )
    else:
        _write_lockfile_atomic(out_path, lockfile)
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
            print()
            print(
                _yellow(
                    "Hint: All components have errors at HEAD. "
                    "If you haven't committed yet, try:"
                )
            )
            print(_yellow("  boundver generate --source working-tree"))


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
    inputs: dict,
    consumer_impact: List[dict],
    baseline: Optional[dict] = None,
) -> None:
    """Print the stable structured result for every verify outcome."""
    payload = {
        "ok": ok,
        "updated": updated,
        "issues": issues,
        "resolved_issues": resolved_issues,
        "observations": observations,
        "facets": facets,
        "facet_policy": facet_policy,
        "components_filter": components_filter,
        "changed_components": changed_components,
        "inputs": inputs,
        "consumer_impact": sorted(
            consumer_impact,
            key=lambda row: row.get("component", ""),
        ),
    }
    if baseline is not None:
        payload["baseline"] = baseline
    _print_json(payload)


def _cmd_verify(args, repo_root: Path) -> None:
    baseline_mode = next(
        (
            mode
            for mode in ("baseline", "write_baseline", "update_baseline")
            if getattr(args, mode, "")
        ),
        None,
    )
    if baseline_mode is not None and args.update:
        print(
            "ERROR: verification baselines cannot be combined with lockfile --update",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)
    if baseline_mode is not None and args.fail_fast:
        print(
            "ERROR: verification baselines require the complete issue set; "
            "remove --fail-fast",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)
    try:
        snapshot = _capture_operation_snapshot(repo_root, args.source)
        config_path = find_config_file(repo_root, args.config, snapshot=snapshot)
        config = load_config_file(config_path, repo_root=repo_root, snapshot=snapshot)
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
            f"ERROR: Config is invalid ({len(config_errors)} issues):", file=sys.stderr
        )
        for error in config_errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    lock_path = repo_root / args.lock
    try:
        _ensure_lock_outside_components(
            repo_root,
            lock_path,
            config,
            config_path=config_path,
        )
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    try:
        lockfile = _load_lockfile(lock_path, repo_root=repo_root, snapshot=snapshot)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if args.source == "head":
            print(
                f"Hint: `--source head` reads committed files. Commit `{args.lock}`, "
                "or use `--source working-tree` before committing.",
                file=sys.stderr,
            )
        elif args.source == "index":
            print(
                f"Hint: `--source index` reads staged files. Stage `{args.lock}`, "
                "or use `--source working-tree` before staging.",
                file=sys.stderr,
            )
        sys.exit(EXIT_USAGE)
    except LockfileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    inputs = _operation_input_provenance(
        repo_root,
        args.source,
        snapshot,
        config_path=config_path,
        lock_path=lock_path,
    )
    input_line = f"Inputs: config={inputs['config']} lock={inputs['lock']}"
    components_filter = _parse_components_arg(args.components)
    requested_components_filter = list(components_filter)
    unknown = [
        name for name in components_filter if name not in config.get("components", {})
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
    unknown_facets = sorted(set(gated_facets) - FACET_SET)
    if unknown_facets:
        print(
            f"ERROR: unknown --facets entries: {', '.join(unknown_facets)}",
            file=sys.stderr,
        )
        sys.exit(EXIT_USAGE)
    structural_issues = [
        *_lockfile_schema_issues(lockfile),
        *_lockfile_structure_issues(lockfile, running_version=_get_version()),
    ]
    preflight_issues = _verify_lock_preflight_issues(config, lockfile)
    changed_components: List[str] = []
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
        # because another component happened to be selected. Resolve the hint
        # before preflight so every text and JSON outcome reports it.
        if not args.quiet and args.format != "json":
            if components_filter:
                print(
                    f"Changed component paths ({len(components_filter)}): "
                    + ", ".join(
                        _display_path(component) for component in components_filter
                    )
                    + "; validating full lock integrity."
                )
            else:
                print(
                    "Changed component paths (0): none; "
                    "validating full lock integrity."
                )
        changed_components = list(components_filter)
        reported_components_filter = []
        components_filter = []
    else:
        reported_components_filter = list(components_filter)
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
                config,
                repo_root,
                source=args.source,
                strict=True,
                allow_custom_providers=allow_custom,
                snapshot=snapshot,
            )
        except ValueError as exc:
            print(f"ERROR: update failed: {exc}", file=sys.stderr)
            sys.exit(EXIT_USAGE)
        _write_lockfile_atomic(lock_path, updated)
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
                inputs=inputs,
                consumer_impact=[],
            )
        elif not args.quiet:
            print(input_line)
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
                inputs=inputs,
                consumer_impact=[],
            )
        else:
            if not args.quiet:
                print(input_line, file=sys.stderr)
            print("ERROR: lockfile preflight failed:", file=sys.stderr)
            for issue in preflight_issues:
                print(f"  - {issue}", file=sys.stderr)
        sys.exit(_drift_exit_code(preflight_issues))
    observations: List[str] = []
    consumer_impact: List[dict] = []
    try:
        issues = verify_lockfile(
            config,
            lockfile,
            repo_root,
            source=args.source,
            components_filter=components_filter,
            allow_custom_providers=allow_custom,
            fail_fast=getattr(args, "fail_fast", False),
            facets=explicit_gated_facets,
            observations=observations,
            snapshot=snapshot,
            transitive_consumers=args.transitive,
            consumer_impact=consumer_impact,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    baseline_info: Optional[dict] = None
    if baseline_mode is not None:
        raw_baseline_path = getattr(args, baseline_mode)
        try:
            baseline_path = _resolve_baseline_path(repo_root, raw_baseline_path)
            _ensure_baseline_outside_components(repo_root, baseline_path, config)
            if baseline_mode != "baseline" or snapshot is None:
                _ensure_plain_baseline_disk_path(repo_root, baseline_path)
            if baseline_path in {
                Path(os.path.abspath(config_path)),
                Path(os.path.abspath(lock_path)),
            }:
                raise BaselineError(
                    "verification baseline must not overwrite the config or lockfile"
                )
            context = baseline_context(
                config=config,
                lockfile=lockfile,
                source=args.source,
                components_filter=reported_components_filter,
                facets=explicit_gated_facets,
                transitive=args.transitive,
                facet_policy=facet_policy,
            )
            baseline_label = baseline_path.relative_to(
                Path(os.path.abspath(repo_root))
            ).as_posix()
            if len(baseline_label) > 4096:
                raise BaselineError(
                    "verification baseline path exceeds the output contract limit"
                )
            if baseline_mode == "baseline":
                stored_baseline = load_baseline(
                    baseline_path,
                    repo_root=repo_root,
                    snapshot=snapshot,
                )
                issues, acknowledged, stale_ids = apply_baseline(
                    stored_baseline, context, issues
                )
                baseline_info = {
                    "action": "applied",
                    "path": baseline_label,
                    "baselined_issues": [
                        issue[:4096] for issue in acknowledged[:10000]
                    ],
                    "stale_ids": stale_ids,
                    "added_ids": [],
                    "removed_ids": [],
                }
            else:
                previous = None
                previous_raw = None
                if baseline_mode == "write_baseline":
                    if baseline_path.exists():
                        raise BaselineError(
                            f"verification baseline already exists: {baseline_label}; "
                            "use --update-baseline after reviewing current debt"
                        )
                else:
                    previous, previous_raw = load_baseline_with_bytes(
                        baseline_path,
                        repo_root=repo_root,
                        snapshot=snapshot,
                    )
                captured_issues = list(issues)
                if previous is not None:
                    # Updating is only a ratchet operation for the exact same
                    # verification scope. A changed source/policy requires a
                    # separately reviewed create workflow.
                    apply_baseline(previous, context, captured_issues)
                replacement = create_baseline(context, captured_issues)
                added_ids, removed_ids = baseline_change_ids(previous, replacement)
                if baseline_mode == "update_baseline" and added_ids:
                    raise BaselineError(
                        "--update-baseline is shrink-only and current verification "
                        f"contains {len(added_ids)} new violation identity/identities; "
                        "fix the new violations instead of baselining them"
                    )
                replacement_text = dump_baseline(replacement)
                if baseline_mode == "write_baseline":
                    write_baseline_create_only(
                        baseline_path,
                        replacement_text,
                        repo_root=repo_root,
                    )
                else:
                    if previous_raw is None:
                        raise BaselineError(
                            "selected verification baseline bytes are unavailable"
                        )
                    replace_baseline_if_unchanged(
                        baseline_path,
                        replacement_text,
                        previous_raw,
                        repo_root=repo_root,
                    )
                issues = []
                baseline_info = {
                    "action": (
                        "created" if baseline_mode == "write_baseline" else "updated"
                    ),
                    "path": baseline_label,
                    "baselined_issues": [
                        issue[:4096] for issue in captured_issues[:10000]
                    ],
                    "stale_ids": [],
                    "added_ids": added_ids,
                    "removed_ids": removed_ids,
                }
        except BaselineError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(EXIT_USAGE)
    if issues:
        unavailable_issues = [
            issue for issue in issues if issue.startswith("UNAVAILABLE FACET ")
        ]
        unavailable_guidance = (
            "Unavailable facets cannot be generated from the current configuration. "
            "Add the missing facet inputs or select only available facets; "
            "`--update` will not modify the lock while these issues remain."
        )
        if args.format != "json" and not args.quiet:
            print(input_line)
            print(_red(f"LOCKFILE OUT OF DATE ({len(issues)} issues):"))
            print()
            for issue in issues:
                print(f"  {issue}")
            if observations:
                print()
                print(_yellow("NON-GATING DRIFT:"))
                for observation in observations:
                    print(f"  {observation}")
            if baseline_info and baseline_info["baselined_issues"]:
                print()
                print(_yellow("KNOWN BASELINE VIOLATIONS:"))
                for issue in baseline_info["baselined_issues"]:
                    print(f"  {issue}")
            if not args.update and unavailable_issues:
                print()
                print("Inspect with `boundver why <component>`.")
                print(unavailable_guidance)
            elif not args.update:
                print()
                print("Inspect with `boundver why <component>`, then run `boundver verify --update` after review.")
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
                    inputs=inputs,
                    consumer_impact=consumer_impact,
                )
            elif not args.quiet:
                print()
                print("LOCKFILE NOT UPDATED: " + unavailable_guidance)
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
                        running_version=_get_version(),
                    )
                else:
                    updated = generate_lockfile(
                        config,
                        repo_root,
                        source=args.source,
                        strict=True,
                        allow_custom_providers=allow_custom,
                        snapshot=snapshot,
                    )
            except ValueError as exc:
                print(f"ERROR: update failed: {exc}", file=sys.stderr)
                sys.exit(EXIT_USAGE)
            _write_lockfile_atomic(lock_path, updated)
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
                    inputs=inputs,
                    consumer_impact=consumer_impact,
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
                inputs=inputs,
                consumer_impact=consumer_impact,
                baseline=baseline_info,
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
                        running_version=_get_version(),
                    )
                else:
                    updated = generate_lockfile(
                        config,
                        repo_root,
                        source=args.source,
                        strict=True,
                        allow_custom_providers=allow_custom,
                        snapshot=snapshot,
                    )
            except ValueError as exc:
                print(f"ERROR: update failed: {exc}", file=sys.stderr)
                sys.exit(EXIT_USAGE)
            _write_lockfile_atomic(lock_path, updated)
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
                    inputs=inputs,
                    consumer_impact=consumer_impact,
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
                inputs=inputs,
                consumer_impact=consumer_impact,
                baseline=baseline_info,
            )
        if args.format != "json" and not args.quiet:
            print(input_line)
        if baseline_info is not None and not args.quiet and args.format != "json":
            baselined_count = len(baseline_info["baselined_issues"])
            stale_count = len(baseline_info["stale_ids"])
            if baseline_info["action"] in {"created", "updated"}:
                print(
                    _green(
                        f"Baseline {baseline_info['action']} at "
                        f"{baseline_info['path']} with {baselined_count} "
                        "reviewed violation(s)."
                    )
                )
                if baselined_count:
                    print("Reviewed baseline violations:")
                    for issue in baseline_info["baselined_issues"]:
                        print(f"  {issue}")
            elif baselined_count:
                print(
                    _yellow(
                        f"Acknowledged {baselined_count} known baseline "
                        "violation(s); no new violations found."
                    )
                )
                for issue in baseline_info["baselined_issues"]:
                    print(f"  {issue}")
            else:
                print(_green("No new baseline violations found."))
            if stale_count:
                print(
                    _yellow(
                        f"Baseline has {stale_count} stale violation id(s); "
                        "review and run --update-baseline to remove them."
                    )
                )
        elif args.verbose and not args.quiet and args.format != "json":
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
        print(
            f"ERROR: Lockfile not found: {lock_path} \u2014 run 'boundver generate' first.",
            file=sys.stderr,
        )
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
    print()
    print(f"  Slice: {args.name}")
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
        config,
        repo_root,
        allow_custom_providers=allow_custom,
        require_slice_facets=not args.allow_partial,
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
        "$schema": "https://raw.githubusercontent.com/yzm1/boundver/v0.14.1/boundary.config.schema.json",
        "project": repo_root.name,
        "defaults": {"compat_mode": "major"},
        "components": discovered
        if args.discover
        else {
            "example-component": {
                "path": "src",
                "version_source": None,
                "boundary": {"provider": "implicit", "paths": []},
            }
        },
    }
    _write_config_atomic(config_path, starter)
    print(f"Created {config_path} with {len(starter['components'])} component(s).")
    print(
        "Next: review the config, then run `boundver validate-config` and `boundver generate`."
    )


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
        print(
            f"ERROR: Component '{args.name}' already exists in config.", file=sys.stderr
        )
        sys.exit(EXIT_USAGE)
    add_path = args.path.replace("\\", "/").rstrip("/")
    if not add_path or ".." in Path(add_path).parts or Path(add_path).is_absolute():
        print(f"ERROR: Invalid component path: {args.path!r}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    boundary_paths = (
        [p.strip() for p in args.paths.split(",") if p.strip()] if args.paths else []
    )
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
    _write_config_atomic(config_path, config)
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
    _write_config_atomic(config_path, config)
    print(f"Removed component '{args.name}'")
    print("Run: boundver generate")


def _cmd_discover(args, repo_root: Path) -> None:
    try:
        exclusions = normalize_discovery_exclusions(
            getattr(args, "exclude", []),
        )
        discovered = discover_components(
            repo_root,
            excluded_paths=exclusions,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        print(f"ERROR: component discovery failed: {detail}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    payload = {
        "count": len(discovered),
        "components": discovered,
        "excluded": exclusions,
    }
    config_diff = None
    if args.diff_config:
        try:
            config_path = find_config_file(repo_root, args.config)
            config = load_config_file(config_path)
            config_diff = compare_discovery_to_config(discovered, config)
        except (FileNotFoundError, ValueError, ConfigError) as exc:
            print(f"ERROR: discovery config comparison failed: {exc}", file=sys.stderr)
            sys.exit(EXIT_USAGE)
        payload["config_diff"] = config_diff
    if args.format == "json":
        _print_json(payload)
    else:
        print(f"Discovered {len(discovered)} components:")
        for name, comp in discovered.items():
            print(f"  - {name}: {comp['path']}")
        if config_diff is not None:
            print(
                "Config comparison: "
                f"{config_diff['registered_count']} registered, "
                f"{config_diff['unregistered_count']} unregistered, "
                f"{config_diff['not_discovered_count']} configured root(s) "
                "without a discoverable manifest"
            )
            if config_diff["unregistered"]:
                print("  Discovered but unregistered:")
                for entry in config_diff["unregistered"]:
                    print(f"    - {entry['name']}: {entry['path']}")
            if config_diff["not_discovered"]:
                print("  Configured but not discovered:")
                for entry in config_diff["not_discovered"]:
                    print(f"    - {entry['name']}: {entry['path']}")


def _cmd_status(args, repo_root: Path) -> None:
    lock_path = repo_root / args.lock
    try:
        snapshot = _capture_operation_snapshot(repo_root, args.source)
        lockfile = _load_lockfile(lock_path, repo_root=repo_root, snapshot=snapshot)
    except (FileNotFoundError, LockfileError, ConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    if lockfile is not None:
        structure_issues = [
            *_lockfile_schema_issues(lockfile),
            *_lockfile_structure_issues(lockfile, running_version=_get_version()),
        ]
        status_payload = {
            "lockfile": lockfile,
            "issues": structure_issues,
            "warnings": [],
            "observations": [],
            "facet_policy": None,
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
                status_payload["warnings"].append(
                    f"{name}: boundary {comp.get('boundary_status', 'unknown')} - {e}"
                )
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
            config_path = find_config_file(repo_root, args.config, snapshot=snapshot)
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
                # Policy construction assumes the validated component/slice
                # shapes.  Keep malformed configs on the controlled diagnostic
                # path instead of letting presentation metadata traceback.
                status_payload["facet_policy"] = _facet_policy_payload(config, None)
                observations: List[str] = []
                try:
                    issues = verify_lockfile(
                        config,
                        lockfile,
                        repo_root,
                        source=args.source,
                        allow_custom_providers=allow_custom,
                        snapshot=snapshot,
                        observations=observations,
                    )
                except ValueError as exc:
                    issues = [f"Verification error: {exc}"]
                status_payload["observations"] = observations
            status_payload["issues"].extend(issues)
            if issues:
                if not args.quiet and args.format != "json":
                    print()
                    print(f"  DRIFT DETECTED ({len(issues)} issues):")
                    for issue in issues:
                        print(f"    {issue}")
            if status_payload["observations"] and not args.quiet and args.format != "json":
                print()
                print(
                    f"  NON-GATING DRIFT ({len(status_payload['observations'])} observations):"
                )
                for observation in status_payload["observations"]:
                    print(f"    {observation}")
        if args.format == "json":
            _print_json(status_payload)
        # --strict: exit non-zero for gated drift or warnings. Observations are
        # intentionally non-gating under the effective component policy.
        if getattr(args, "strict", False) and (
            status_payload["issues"] or has_warnings
        ):
            sys.exit(_drift_exit_code(status_payload["issues"]))
    else:
        print(f"No lockfile found at {lock_path}. Run 'generate' first.")
        sys.exit(EXIT_USAGE)


def _cmd_explain(args, repo_root: Path) -> None:
    try:
        snapshot = _capture_operation_snapshot(repo_root, args.source)
        config_path = find_config_file(repo_root, args.config, snapshot=snapshot)
        config = load_config_file(config_path, repo_root=repo_root, snapshot=snapshot)
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
    lockfile = None
    try:
        lockfile = _load_lockfile(
            repo_root / args.lock,
            repo_root=repo_root,
            snapshot=snapshot,
        )
    except (FileNotFoundError, LockfileError):
        # Explain remains usable before the first lock is created. In that
        # case the analyzer reports its explicit previous-commit fallback.
        pass
    rc = explain_component_changes(
        config,
        repo_root,
        args.component,
        base_ref=args.base_ref,
        source=args.source,
        snapshot=snapshot,
        lock_path=args.lock,
        lockfile=lockfile,
    )
    if rc != 0:
        sys.exit(rc)


def _cmd_why(args, repo_root: Path) -> None:
    lock_path = repo_root / args.lock
    try:
        snapshot = _capture_operation_snapshot(repo_root, args.source)
        config_path = find_config_file(repo_root, args.config, snapshot=snapshot)
        config = load_config_file(config_path, repo_root=repo_root, snapshot=snapshot)
        lockfile = _load_lockfile(lock_path, repo_root=repo_root, snapshot=snapshot)
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
    inputs = _operation_input_provenance(
        repo_root,
        args.source,
        snapshot,
        config_path=config_path,
        lock_path=lock_path,
    )
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
        diagnostic_base_ref=args.base_ref,
        lock_path=args.lock,
        lock_provenance=inputs["lock"],
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
    parser = build_parser(version=_get_version(), epilog=__doc__)

    args = parser.parse_args()

    # Honour the BOUNDVER_ALLOW_CUSTOM_PROVIDERS env var as a fallback so
    # automation pipelines don't need to thread the flag through every call site.
    if os.environ.get("BOUNDVER_ALLOW_CUSTOM_PROVIDERS", "").strip() in {
        "1",
        "true",
        "yes",
    }:
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
        print("ERROR: Not inside a git repository.", file=sys.stderr)
        print(file=sys.stderr)
        print(
            "boundver requires git history to compute fingerprints.",
            file=sys.stderr,
        )
        print("Common fixes:", file=sys.stderr)
        print(
            "  - In CI: set fetch-depth: 0 (GitHub Actions) or GIT_DEPTH: 0 (GitLab)",
            file=sys.stderr,
        )
        print(
            "  - In Docker: copy .git into the build context or mount it",
            file=sys.stderr,
        )
        print(
            "  - Locally: run from within a git-initialized directory (git init)",
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
