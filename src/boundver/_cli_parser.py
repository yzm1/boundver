"""Argument-parser construction for the public boundver CLI."""

import argparse
import sys

from ._output import _display_text
from ._utils import SOURCE_MODES


class _TerminalSafeArgumentParser(argparse.ArgumentParser):
    """Keep caller-controlled parse errors on one terminal-safe line."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {_display_text(message)}\n")


def build_parser(*, version: str, epilog: str) -> argparse.ArgumentParser:
    """Build the parser without executing or dispatching a command."""
    parser = _TerminalSafeArgumentParser(
        prog="boundver",
        description="Detect declared component, behavior, boundary, and compatibility drift",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version}"
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "--quiet", action="store_true", help="Reduce non-error human-readable output"
    )
    verbosity.add_argument(
        "--verbose", action="store_true", help="Print additional progress diagnostics"
    )
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
    gen.add_argument(
        "--config", default="boundary.config.json", help="Config file path"
    )
    gen.add_argument("--out", default="boundary.lock.json", help="Output lockfile path")
    gen.add_argument(
        "--source",
        choices=SOURCE_MODES,
        default="head",
        help="Snapshot to fingerprint (default: head): last commit, staged index, or tracked files on disk",
    )
    gen.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow intentional null facet inputs in slices (not provider or source errors)",
    )
    gen.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute lockfile and print status without writing output",
    )
    gen.add_argument(
        "--components", default="", help="Comma-separated component names to regenerate"
    )
    gen.add_argument(
        "--format", choices=["json", "text"], default="text", help="Output format"
    )
    gen.add_argument(
        "--allow-custom-providers",
        action="store_true",
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
    ver.add_argument(
        "--config",
        default="boundary.config.json",
        help="Config path inside the selected source snapshot",
    )
    ver.add_argument(
        "--lock",
        default="boundary.lock.json",
        help="Lock path inside the selected source snapshot",
    )
    ver.add_argument(
        "--source",
        choices=SOURCE_MODES,
        default="head",
        help="Snapshot to compare (default: head): last commit, staged index, or tracked files on disk",
    )
    ver.add_argument(
        "--components", default="", help="Comma-separated component names to verify"
    )
    ver.add_argument(
        "--changed-from",
        default="",
        help="Report components changed since a Git ref while verifying full lock integrity",
    )
    ver.add_argument(
        "--fail-fast",
        action="store_true",
        help="Report only the highest-severity mismatch",
    )
    ver.add_argument(
        "--facets",
        default="",
        help=(
            "Comma-separated CLI-wide gate override (default: component "
            "verify_facets, then config defaults, then all available facets)"
        ),
    )
    ver.add_argument(
        "--transitive",
        action="store_true",
        help="Report the transitive downstream consumer closure for boundary/compat drift",
    )
    ver.add_argument(
        "--update",
        action="store_true",
        help="After reporting drift, atomically regenerate the lockfile if computation succeeds",
    )
    baseline_group = ver.add_mutually_exclusive_group()
    baseline_group.add_argument(
        "--baseline",
        default="",
        metavar="PATH",
        help="Allow reviewed violation identities from a baseline while failing on new ones",
    )
    baseline_group.add_argument(
        "--write-baseline",
        default="",
        metavar="PATH",
        help="Explicitly capture current baselinable violations in a new JSON file",
    )
    baseline_group.add_argument(
        "--update-baseline",
        default="",
        metavar="PATH",
        help="Shrink an existing baseline by removing resolved identities; never adds violations",
    )
    ver.add_argument(
        "--format", choices=["json", "text"], default="text", help="Output format"
    )
    ver.add_argument(
        "--allow-custom-providers",
        action="store_true",
        help="Allow loading external provider modules declared in the config 'providers' key",
    )

    # diff
    dif = sub.add_parser("diff", help="Diff two lockfiles")
    dif.add_argument("old", help="Old lockfile")
    dif.add_argument("new", help="New lockfile")
    dif.add_argument(
        "--format", choices=["json", "text"], default="text", help="Output format"
    )

    # slice
    sl = sub.add_parser("slice", help="Show fingerprint for a specific slice")
    sl.add_argument("name", help="Slice name")
    sl.add_argument("--lock", default="boundary.lock.json")
    sl.add_argument(
        "--format", choices=["json", "text"], default="text", help="Output format"
    )

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
        "--allow-partial",
        action="store_true",
        help="Allow intentional null facet inputs in slices",
    )
    vc.add_argument(
        "--allow-custom-providers",
        action="store_true",
        help="Allow loading external provider modules declared in the config 'providers' key",
    )
    cc = sub.add_parser("check-config", help="Alias for validate-config")
    cc.add_argument("--config", default="boundary.config.json")
    cc.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow intentional null facet inputs in slices",
    )
    cc.add_argument(
        "--allow-custom-providers",
        action="store_true",
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
    init.add_argument(
        "--out", default="boundary.config.json", help="Output config file path"
    )
    init.add_argument("--force", action="store_true", help="Overwrite existing file")
    init.add_argument(
        "--discover",
        action="store_true",
        help="Auto-discover components from common manifests",
    )

    # add
    add = sub.add_parser("add", help="Add a component to the config")
    add.add_argument("name", help="Component name")
    add.add_argument("path", help="Component path relative to repo root")
    add.add_argument(
        "--provider", default="implicit", help="Boundary provider (default: implicit)"
    )
    add.add_argument("--paths", default="", help="Comma-separated boundary paths")
    add.add_argument(
        "--config", default="boundary.config.json", help="Config file path"
    )

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
    st.add_argument(
        "--source",
        choices=SOURCE_MODES,
        default="head",
        help="Snapshot to inspect (default: head)",
    )
    st.add_argument(
        "--format", choices=["json", "text"], default="text", help="Output format"
    )
    st.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any drift or warnings are detected (useful for CI)",
    )
    st.add_argument(
        "--allow-custom-providers",
        action="store_true",
        help="Allow loading external provider modules declared in the config 'providers' key",
    )

    # explain
    ex = sub.add_parser("explain", help="Explain changed files for a component")
    ex.add_argument("component", help="Component name from config")
    ex.add_argument("--config", default="boundary.config.json")
    ex.add_argument(
        "--lock",
        default="boundary.lock.json",
        help="Lockfile path used to infer the default diagnostic base",
    )
    ex.add_argument(
        "--base-ref",
        default=None,
        help=(
            "Git ref to diff against (default: commit that introduced the "
            "component's current lock entry for source=head; HEAD otherwise)"
        ),
    )
    ex.add_argument(
        "--source",
        choices=SOURCE_MODES,
        default="head",
        help="Source to diff against base-ref (default: head)",
    )
    ex.add_argument(
        "--allow-custom-providers",
        action="store_true",
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
    why.add_argument(
        "--base-ref",
        default=None,
        help=(
            "Git ref for changed-file diagnostics (default: commit that introduced "
            "the component's current lock entry for source=head; HEAD otherwise)"
        ),
    )
    why.add_argument(
        "--source",
        choices=SOURCE_MODES,
        default="head",
        help="Fingerprint source to compare against (default: head)",
    )
    why.add_argument(
        "--format", choices=["json", "text"], default="text", help="Output format"
    )
    why.add_argument(
        "--transitive",
        action="store_true",
        help="Report the transitive downstream consumer closure",
    )
    why.add_argument(
        "--allow-custom-providers",
        action="store_true",
        help="Allow loading external provider modules declared in the config 'providers' key",
    )

    disc = sub.add_parser("discover", help="Find Git-tracked component manifests")
    disc.add_argument(
        "--format", choices=["json", "text"], default="text", help="Output format"
    )
    disc.add_argument(
        "--diff-config",
        action="store_true",
        help="Compare discovered component paths with the current config",
    )
    disc.add_argument(
        "--config",
        default="boundary.config.json",
        help="Config file used by --diff-config",
    )
    disc.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Exclude a repository-relative path and everything below it; "
            "repeat for multiple paths"
        ),
    )

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
    ml.add_argument(
        "--lock",
        default="boundary.lock.json",
        help="Path to lockfile (default: boundary.lock.json)",
    )
    migrate_mode = ml.add_mutually_exclusive_group()
    migrate_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Print normalized JSON to stdout without writing the file",
    )
    migrate_mode.add_argument(
        "--explain",
        action="store_true",
        help=(
            "Analyze and classify every boundary/behavior selector under v0.10 "
            "and current semantics without writing"
        ),
    )
    ml.add_argument(
        "--config", default="boundary.config.json", help="Config used by --explain"
    )
    ml.add_argument(
        "--source",
        choices=SOURCE_MODES,
        default="head",
        help="Source analyzed by --explain (default: head)",
    )
    ml.add_argument(
        "--format",
        choices=["json", "text"],
        default="text",
        help="Analysis output format used by --explain",
    )

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
    comp.add_argument(
        "--shell", required=True, choices=["bash", "zsh", "fish"], help="Target shell"
    )
    return parser
