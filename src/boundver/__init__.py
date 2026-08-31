"""boundver package."""

from typing import List, Optional

__all__ = [
    "main",
    "generate",
    "verify",
    "diff",
    "load_config",
    "BoundverError",
    "ConfigError",
    "LockfileError",
    "ProviderError",
    "GuardrailError",
    "SourceMode",
    "create_registry",
    "analyze_component_drift",
    "analyze_explain_changes",
]

from importlib.metadata import PackageNotFoundError, version

from ._utils import BoundverError, ConfigError, GuardrailError, LockfileError, ProviderError, SourceMode
from .providers import create_registry
from ._output import analyze_component_drift, analyze_explain_changes

from .cli import main

try:
    __version__ = version("boundver")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+local"


def _load_validated_config_inputs(
    config_path: str,
    source: str,
    *,
    allow_custom_providers: bool,
    require_slice_facets: bool = False,
):
    """Return one shared, validated config view for the public API."""
    import subprocess

    from ._config import find_config_file, load_config_file, validate_config
    from ._git import git_root
    from .core import _capture_operation_snapshot

    try:
        repo_root = git_root()
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise ConfigError(
            "Cannot load config: the current directory is not inside a readable "
            "Git repository"
        ) from exc
    try:
        snapshot = _capture_operation_snapshot(repo_root, source)
        path = find_config_file(repo_root, config_path, snapshot=snapshot)
        config = load_config_file(
            path,
            repo_root=repo_root,
            snapshot=snapshot,
        )
    except ConfigError:
        raise
    except FileNotFoundError as exc:
        raise ConfigError(str(exc)) from exc
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise ConfigError(f"Cannot load config: {exc}") from exc

    config_errors = validate_config(
        config,
        repo_root,
        allow_custom_providers=allow_custom_providers,
        source=source,
        snapshot=snapshot,
        require_slice_facets=require_slice_facets,
    )
    if config_errors:
        raise ConfigError("Config is invalid:\n" + "\n".join(config_errors))
    return repo_root, snapshot, path, config


def load_config(
    config_path: str = "boundary.config.json",
    source: str = "working-tree",
) -> dict:
    """Load and validate one repository config without executing custom providers.

    ``source`` selects working-tree bytes by default or one immutable ``head``
    or ``index`` snapshot. ``ConfigError`` is raised for repository, source,
    missing-file, parse, and validation failures. A valid config does not need
    a ``$schema`` field because the packaged schema remains authoritative.
    """
    _, _, _, config = _load_validated_config_inputs(
        config_path,
        source,
        allow_custom_providers=False,
    )
    return config


def generate(
    config_path: str = "boundary.config.json",
    out_path: Optional[str] = "boundary.lock.json",
    source: str = "head",
    allow_custom_providers: bool = False,
) -> dict:
    """Generate a lockfile and return it as a dict.

    Also writes to *out_path* (relative to repo root). Pass ``out_path=None``
    to skip writing.
    """
    from ._lockfile import generate_lockfile
    from .core import (
        _ensure_lock_outside_components,
        _write_lockfile_atomic,
    )

    repo_root, snapshot, resolved_config_path, config = _load_validated_config_inputs(
        config_path,
        source,
        allow_custom_providers=allow_custom_providers,
        require_slice_facets=True,
    )
    if out_path is not None:
        dest = repo_root / out_path
        _ensure_lock_outside_components(
            repo_root,
            dest,
            config,
            config_path=resolved_config_path,
        )
    lockfile = generate_lockfile(
        config, repo_root, source=source, strict=True,
        allow_custom_providers=allow_custom_providers,
        snapshot=snapshot,
    )
    if out_path is not None:
        _write_lockfile_atomic(dest, lockfile)
    return lockfile


def verify(
    config_path: str = "boundary.config.json",
    lock_path: str = "boundary.lock.json",
    source: str = "head",
    components: Optional[List[str]] = None,
    allow_custom_providers: bool = False,
    facets: Optional[List[str]] = None,
    observations: Optional[List[str]] = None,
    fail_fast: bool = False,
    transitive_consumers: bool = False,
) -> List[str]:
    """Verify lockfile matches current repo state.

    Returns gated mismatch strings. Empty means the selected gate is current.
    When *facets* is omitted, ``defaults.verify_facets`` is honored just like
    the CLI. Pass a list as *observations* to collect drift outside that gate.
    """
    from ._lockfile import verify_lockfile
    from .core import (
        _ensure_lock_outside_components,
        _load_lockfile,
        _verify_lock_preflight_issues,
    )

    repo_root, snapshot, resolved_config_path, config = _load_validated_config_inputs(
        config_path,
        source,
        allow_custom_providers=allow_custom_providers,
    )
    resolved_lock_path = repo_root / lock_path
    _ensure_lock_outside_components(
        repo_root,
        resolved_lock_path,
        config,
        config_path=resolved_config_path,
    )
    lf = _load_lockfile(
        resolved_lock_path, repo_root=repo_root, snapshot=snapshot
    )
    preflight_issues = _verify_lock_preflight_issues(config, lf)
    if preflight_issues:
        return preflight_issues
    return verify_lockfile(
        config, lf, repo_root, source=source,
        components_filter=components,
        allow_custom_providers=allow_custom_providers,
        facets=facets,
        observations=observations,
        fail_fast=fail_fast,
        snapshot=snapshot,
        transitive_consumers=transitive_consumers,
    )


def diff(old_path: str, new_path: str) -> dict:
    """Diff two lockfiles and return structured result."""
    from pathlib import Path
    from ._diff import diff_lockfiles, require_compatible_lockfile_schemas
    from .core import _load_lockfile, _require_diffable_lockfile

    old = _load_lockfile(Path(old_path))
    new = _load_lockfile(Path(new_path))
    require_compatible_lockfile_schemas(old, new)
    _require_diffable_lockfile(old)
    _require_diffable_lockfile(new)
    return diff_lockfiles(old, new)
