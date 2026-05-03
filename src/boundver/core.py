#!/usr/bin/env python3
"""
boundary-lock: Semantic version manifest with faceted fingerprints.

Generates a lockfile where each component has:
    - exact fingerprint     (did anything change?)
    - behavior fingerprint  (did the behavioral contract change?)
    - boundary fingerprint  (did the public boundary change?)
    - compat fingerprint    (did the compatibility family change?)

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
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

# Sub-module imports — each group lives in a focused module.
from ._git import (
    _git_run,
    _head_entries_for_path,
    _is_ignored,
    _list_files_for_source,
    _to_posix,
    changed_components_since_ref,
    git_latest_tag,
    git_root,
    list_head_files,
    _git_cat_blob,
    _git_batch_cat,
)
from ._hashing import (
    MAX_HASH_FILE_BYTES,
    MAX_HASH_FILES,
    _content_only_digest,
    _enforce_content_size,
    _enforce_hash_guardrails,
    _is_within,
    _read_path_content,
    _short,
    boundary_provider_name,
    canonical_json,
    sha256_hex,
    source_tree_digest,
)
from ._config import (
    config_warnings,
    _is_str_list,
    _load_config_schema,
    _schema_engine_errors,
    _schema_required_fields,
    discover_components,
    find_config_file,
    load_config_file,
    validate_config,
)
from ._lockfile import (
    LOCKFILE_SCHEMA,
    LOCKFILE_SCHEMA_URL,
    MigrationError,
    _lockfile_schema_issues,
    _lockfile_structure_issues,
    _recompute_slice_entry,
    generate_lockfile,
    generate_lockfile_for_components,
    migrate_lockfile,
    verify_lockfile,
)
from ._diff import _summarize_change, diff_lockfiles
from ._output import (
    _bold,
    _green,
    _is_tty,
    _log,
    _parse_components_arg,
    _print_json,
    _red,
    _yellow,
    explain_component_changes,
    print_diff,
    print_status,
    why_component,
)
from ._completions import (
    _BASH_COMPLETION,
    _COMPLETION_SCRIPTS,
    _FISH_COMPLETION,
    _ZSH_COMPLETION,
)
from .versions import (
    _extract_json_field,
    _extract_toml_field,
    _extract_yaml_field,
    extract_version,
    parse_semver,
    yaml,
)


def main():
    parser = argparse.ArgumentParser(
        description="boundary-lock: semantic version manifest with faceted fingerprints",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("--quiet", action="store_true", help="Reduce non-error human-readable output")
    verbosity.add_argument("--verbose", action="store_true", help="Print additional progress diagnostics")
    sub = parser.add_subparsers(dest="command")

    # generate
    gen = sub.add_parser("generate", help="Generate or update the lockfile")
    gen.add_argument("--config", default="boundary.config.json", help="Config file path")
    gen.add_argument("--out", default="boundary.lock.json", help="Output lockfile path")
    gen.add_argument("--source", choices=["head", "index", "working-tree"], default="head", help="Fingerprint source")
    gen.add_argument("--allow-partial", action="store_true", help="Allow missing boundary/compat digests in slices")
    gen.add_argument("--dry-run", action="store_true", help="Compute lockfile and print status without writing output")
    gen.add_argument("--components", default="", help="Comma-separated component names to regenerate")
    gen.add_argument("--format", choices=["json", "text"], default="text", help="Output format")
    gen.add_argument(
        "--allow-custom-providers", action="store_true",
        help="Allow loading external provider modules declared in the config 'providers' key",
    )

    # verify
    ver = sub.add_parser(
        "verify",
        help="Check lockfile matches current repo state",
        description=(
            "Verify the lockfile is up to date with the current repo state.\n\n"
            "Exit codes:\n"
            "  0  Lockfile matches current repo state\n"
            "  1  Lockfile is out of date (fingerprint mismatches found)\n"
            "  2  Usage error (unknown component name, config missing, etc.)\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ver.add_argument("--config", default="boundary.config.json")
    ver.add_argument("--lock", default="boundary.lock.json")
    ver.add_argument("--source", choices=["head", "index", "working-tree"], default="head",
                     help="Fingerprint source to compare against locked values")
    ver.add_argument("--components", default="", help="Comma-separated component names to verify")
    ver.add_argument("--changed-from", default="", help="Auto-select changed components since git ref")
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

    # validate-config (and check-config alias)
    vc = sub.add_parser("validate-config", help="Validate config for strict boundary rules")
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
    init = sub.add_parser("init", help="Create a starter boundary.config.json")
    init.add_argument("--out", default="boundary.config.json", help="Output config file path")
    init.add_argument("--force", action="store_true", help="Overwrite existing file")
    init.add_argument("--discover", action="store_true", help="Auto-discover components from common manifests")

    # status
    st = sub.add_parser("status", help="Show lockfile summary and warnings")
    st.add_argument("--config", default="boundary.config.json")
    st.add_argument("--lock", default="boundary.lock.json")
    st.add_argument("--source", choices=["head", "index", "working-tree"], default="head")
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
    ex.add_argument("--source", choices=["head", "index", "working-tree"], default="head",
                    help="Source to diff against base-ref (default: head)")

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
    why.add_argument("--source", choices=["head", "index", "working-tree"], default="head",
                     help="Fingerprint source to compare against (default: head)")

    disc = sub.add_parser("discover", help="Print discovered components as JSON")
    disc.add_argument("--format", choices=["json", "text"], default="text", help="Output format")

    # migrate-lock
    ml = sub.add_parser(
        "migrate-lock",
        help="Upgrade a boundary.lock.json to the current schema",
        description=(
            "Read an existing boundary.lock.json, apply any pending schema migrations, "
            "and write the result back in-place.  If the lockfile is already at the "
            "current schema this is a no-op (exit 0).  Use --dry-run to preview."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ml.add_argument("--lock", default="boundary.lock.json",
                    help="Path to lockfile (default: boundary.lock.json)")
    ml.add_argument("--dry-run", action="store_true",
                    help="Print migrated JSON to stdout without writing the file")

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
        sys.exit(1)

    # completions doesn't need a git repo — handle before the git_root() call.
    if args.command == "completions":
        sys.stdout.write(_COMPLETION_SCRIPTS.get(args.shell, ""))
        return

    # migrate-lock also doesn't need a git repo.
    if args.command == "migrate-lock":
        lock_path = Path(args.lock)
        if not lock_path.is_absolute():
            lock_path = Path.cwd() / args.lock
        if not lock_path.exists():
            print(f"error: lockfile not found: {lock_path}", file=sys.stderr)
            sys.exit(1)
        try:
            old_lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"error: could not parse lockfile as JSON: {exc}", file=sys.stderr)
            sys.exit(1)
        try:
            migrated = migrate_lockfile(old_lock)
        except MigrationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        out = json.dumps(migrated, indent=2, sort_keys=False) + "\n"
        if args.dry_run:
            sys.stdout.write(out)
        else:
            lock_path.write_text(out, encoding="utf-8")
            if migrated.get("schema") == old_lock.get("schema"):
                _log("Already at current schema, no changes written.")
            else:
                _log(f"Migrated {lock_path} → schema {migrated['schema']}")
        return

    # diff compares two lockfiles and doesn't need a git repo.
    if args.command == "diff":
        for label, fpath in (("old", args.old), ("new", args.new)):
            if ".." in Path(fpath).parts:
                print(
                    f"ERROR: {label} lockfile path '{fpath}' contains '..' traversal",
                    file=sys.stderr,
                )
                sys.exit(2)
            if not Path(fpath).exists():
                print(f"ERROR: {label} lockfile not found: {fpath}", file=sys.stderr)
                sys.exit(2)
        try:
            old = json.loads(Path(args.old).read_text())
            new = json.loads(Path(args.new).read_text())
        except json.JSONDecodeError as exc:
            print(f"ERROR: Failed to parse lockfile: {exc}", file=sys.stderr)
            sys.exit(2)
        result = diff_lockfiles(old, new)
        if args.format == "json":
            _print_json(result)
        else:
            print_diff(result)
        return

    try:
        repo_root = git_root()
    except subprocess.CalledProcessError:
        print("ERROR: Not inside a git repository.", file=sys.stderr)
        sys.exit(1)

    def _resolve_allow_custom(config: dict) -> bool:
        """Merge CLI flag, env var, and config-level allow_custom_providers."""
        if getattr(args, "allow_custom_providers", False):
            return True
        return bool(config.get("allow_custom_providers", False))

    if args.command == "generate":
        config_path = find_config_file(repo_root, args.config)
        if not config_path.exists():
            print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
            sys.exit(1)
        try:
            config = load_config_file(config_path)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        allow_custom = _resolve_allow_custom(config)
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
                )
            else:
                lockfile = generate_lockfile(
                    config, repo_root, source=args.source, strict=(not args.allow_partial),
                    allow_custom_providers=allow_custom,
                )
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            print("Run: boundver validate-config", file=sys.stderr)
            sys.exit(1)
        out_path = repo_root / args.out
        if args.dry_run:
            _log(f"Dry run: lockfile not written ({out_path})", quiet=(args.quiet or args.format == "json"))
        else:
            out_path.write_text(json.dumps(lockfile, indent=2) + "\n")
            _log(f"Generated {out_path}", quiet=(args.quiet or args.format == "json"))
        if args.verbose and not args.quiet and args.format != "json":
            print(f"Generation source={args.source} strict={not args.allow_partial}")
        if args.format == "json":
            _print_json(lockfile)
        elif not args.quiet:
            print_status(lockfile)

    elif args.command == "verify":
        try:
            config = load_config_file(find_config_file(repo_root, args.config))
        except (FileNotFoundError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        allow_custom = _resolve_allow_custom(config)
        lock_path = repo_root / args.lock
        if not lock_path.exists():
            print(f"ERROR: Lockfile not found: {lock_path} — run 'boundver generate' first.", file=sys.stderr)
            sys.exit(2)
        try:
            lockfile = json.loads(lock_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"ERROR: Lockfile is not valid JSON: {exc}", file=sys.stderr)
            sys.exit(2)
        components_filter = _parse_components_arg(args.components)
        if args.changed_from:
            auto = changed_components_since_ref(config, repo_root, args.changed_from)
            if components_filter:
                auto_set = set(auto)
                components_filter = [c for c in components_filter if c in auto_set]
            else:
                components_filter = auto
        unknown = [n for n in components_filter if n not in config.get("components", {})]
        if unknown:
            print(f"ERROR: unknown --components entries: {', '.join(unknown)}", file=sys.stderr)
            sys.exit(2)
        issues = verify_lockfile(
            config, lockfile, repo_root, source=args.source, components_filter=components_filter,
            allow_custom_providers=allow_custom,
        )
        if args.format == "json":
            _print_json({"ok": len(issues) == 0, "issues": issues, "components_filter": components_filter})
        if issues:
            if args.format != "json" and not args.quiet:
                print(_red(f"LOCKFILE OUT OF DATE ({len(issues)} issues):") + "\n")
                for issue in issues:
                    print(f"  {issue}")
            sys.exit(1)
        else:
            if args.verbose and not args.quiet:
                print(_green(f"Verified source={args.source} with 0 issues."))
            elif args.format != "json" and not args.quiet:
                print(_green("Lockfile is up to date."))

    elif args.command == "slice":
        lock_path = repo_root / args.lock
        if not lock_path.exists():
            print(f"ERROR: Lockfile not found: {lock_path} — run 'boundver generate' first.", file=sys.stderr)
            sys.exit(2)
        lockfile = json.loads(lock_path.read_text())
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

    elif args.command in ("validate-config", "check-config"):
        config_path = find_config_file(repo_root, args.config)
        if not config_path.exists():
            print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
            sys.exit(1)
        try:
            config = load_config_file(config_path)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        errors = validate_config(config, repo_root)
        warnings = config_warnings(config, repo_root)
        if errors:
            print(_red(f"CONFIG INVALID ({len(errors)} issues):"))
            for err in errors:
                print(f"  - {err}")
            sys.exit(1)
        if warnings:
            print(_yellow(f"CONFIG WARNINGS ({len(warnings)}):"))
            for warning in warnings:
                print(f"  - {warning}")
        print(_green("Config is valid."))

    elif args.command == "init":
        config_path = repo_root / args.out
        if config_path.exists() and not args.force:
            print(f"ERROR: Config already exists: {config_path}", file=sys.stderr)
            sys.exit(1)
        discovered = discover_components(repo_root) if args.discover else {}
        starter = {
            "$schema": "https://raw.githubusercontent.com/yzm1/boundver/main/boundary.config.schema.json",
            "project": repo_root.name,
            "defaults": {"compat_mode": "major"},
            "components": discovered or {
                "example-component": {
                    "path": "src",
                    "version_source": None,
                    "boundary": {"provider": "implicit", "paths": []},
                }
            },
            "slices": {
                "default": {
                    "description": "Default exact slice",
                    "mode": "exact",
                    "components": sorted((discovered or {"example-component": {}}).keys()),
                }
            },
        }
        config_path.write_text(json.dumps(starter, indent=2) + "\n")
        print(f"Created starter config: {config_path}")

    elif args.command == "discover":
        discovered = discover_components(repo_root)
        payload = {"count": len(discovered), "components": discovered}
        if args.format == "json":
            _print_json(payload)
        else:
            print(f"Discovered {len(discovered)} components:")
            for name, comp in discovered.items():
                print(f"  - {name}: {comp['path']}")

    elif args.command == "status":
        lock_path = repo_root / args.lock
        if lock_path.exists():
            try:
                lockfile = json.loads(lock_path.read_text())
            except json.JSONDecodeError as exc:
                print(f"ERROR: Lockfile is not valid JSON: {exc}", file=sys.stderr)
                sys.exit(2)
            status_payload = {"lockfile": lockfile, "issues": [], "warnings": []}
            if args.format != "json" and not args.quiet:
                print_status(lockfile)
            # Collect component warnings
            has_warnings = False
            for name, comp in lockfile.get("components", {}).items():
                for w in comp.get("warnings", []):
                    status_payload["warnings"].append(f"{name}: {w}")
                    has_warnings = True
                for e in comp.get("boundary_errors", []):
                    status_payload["warnings"].append(f"{name}: boundary {comp.get('boundary_status', 'unknown')} - {e}")
                    has_warnings = True
            # Also verify if config exists
            config_path = find_config_file(repo_root, args.config)
            if config_path.exists():
                try:
                    config = load_config_file(config_path)
                except ValueError as exc:
                    print(f"WARNING: Config parse error: {exc}", file=sys.stderr)
                    config = None
                if config is not None:
                    allow_custom = _resolve_allow_custom(config)
                    issues = verify_lockfile(config, lockfile, repo_root, source=args.source,
                                             allow_custom_providers=allow_custom)
                    status_payload["issues"] = issues
                    if issues:
                        if not args.quiet and args.format != "json":
                            print(f"\n  DRIFT DETECTED ({len(issues)} issues):")
                            for issue in issues:
                                print(f"    {issue}")
            if args.format == "json":
                _print_json(status_payload)
            # --strict: exit non-zero if any drift or warnings
            if getattr(args, "strict", False) and (status_payload["issues"] or has_warnings):
                sys.exit(1)
        else:
            print(f"No lockfile found at {lock_path}. Run 'generate' first.")
            sys.exit(1)

    elif args.command == "explain":
        try:
            config = load_config_file(find_config_file(repo_root, args.config))
        except (FileNotFoundError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        rc = explain_component_changes(config, repo_root, args.component, base_ref=args.base_ref, source=args.source)
        if rc != 0:
            sys.exit(rc)

    elif args.command == "why":
        config_path = find_config_file(repo_root, args.config)
        lock_path = repo_root / args.lock
        if not config_path.exists():
            print(f"ERROR: Config file not found: {config_path}", file=sys.stderr)
            sys.exit(2)
        if not lock_path.exists():
            print(f"ERROR: Lockfile not found: {lock_path} — run 'boundver generate' first.", file=sys.stderr)
            sys.exit(2)
        try:
            config = load_config_file(config_path)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        lockfile = json.loads(lock_path.read_text())
        rc = why_component(config, lockfile, repo_root, args.component, source=args.source)
        if rc != 0:
            sys.exit(rc)


if __name__ == "__main__":
    main()
