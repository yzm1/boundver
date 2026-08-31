"""Lockfile generation and verification for boundver."""

import json
import os
import posixpath
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Set, Union

from ._config import _json_value_issues, _snapshot_relative_path
from ._config_contract import git_tag_prefix_error
from ._consumer_graph import (
    affected_consumer_groups,
    affected_consumers,
    empty_explicit_slice_error,
    resolve_slice_components,
)
from ._structured_data import strict_json_loads

from ._git import (
    GitSourceSnapshot,
    _capture_git_source_snapshot,
    _git_cat_blob,
    _is_git_repository,
    _list_files_for_source,
    _snapshot_files,
    _to_posix,
    git_latest_tag,
)
from ._hashing import (
    HASH_DOMAIN_BEHAVIOR,
    MAX_HASH_FILE_BYTES,
    _ModeAwareBytes,
    _content_only_digest,
    _enforce_content_size,
    _hash_framed_entries,
    _read_bounded_path_bytes,
    _read_path_content,
    canonical_json,
    sha256_hex,
    source_tree_digest,
)
from ._lockfile_validation import (
    is_sha256_digest as _is_sha256_digest_impl,
    lockfile_schema_issues as _lockfile_schema_issues_impl,
    lockfile_structure_issues as _lockfile_structure_issues_impl,
)
from ._utils import (
    BoundedDiagnosticList,
    DIAGNOSTIC_TRUNCATION_SENTINEL,
    FACETS,
    FACET_SET,
    SOURCE_MODE_SET,
    _bounded_diagnostic_repr,
    _bounded_diagnostic_text,
    _bounded_json_dumps,
    _bounded_json_int,
    _available_component_facets,
    _issue_facet,
    _short,
    boundary_provider_name,
    ConfigError,
    GuardrailError,
    LockfileError,
    ProviderError,
    SourceMode,
)
from .providers import (
    PathHashProvider,
    ProviderContext,
    compute_boundary,
    create_registry,
    get_provider,
    load_custom_providers,
)
from .versions import MAX_VERSION_FILE_BYTES, extract_version, parse_semver

LOCKFILE_SCHEMA = "boundary-lock/v3"
# v0.13.0 is the immutable canonical publication of the v3 schema.  Keep this
# URL stable across digest-neutral package upgrades; changing the persisted
# annotation would otherwise dirty every regenerated lock despite identical
# schema, configuration, and component content.  A structural schema change
# must advance LOCKFILE_SCHEMA and select a new canonical publication.
LOCKFILE_SCHEMA_URL = "https://raw.githubusercontent.com/yzm1/boundver/v0.13.0/spec/boundary.lock.schema.json"
SEMANTIC_CONFIG_VERSION = "boundver-semantic-config/v2"
# Historical semantic contracts that retain the current boundary-lock/v3
# structure closely enough for a bounded, read-only comparison.  Mutation and
# verification paths continue to accept only ``SEMANTIC_CONFIG_VERSION``.
DIFFABLE_SEMANTIC_CONFIG_VERSIONS = frozenset(
    {"boundver-semantic-config/v1", SEMANTIC_CONFIG_VERSION}
)
MAX_LOCKFILE_BYTES = 10 * 1024 * 1024

# Persisted fields whose integrity matters independently of the four digests.
# Keep this list shared by verify/diff/why so none of those views can report a
# stale entry as current merely because its fingerprints happen to match.
COMPONENT_METADATA_FIELDS = (
    "version", "path", "boundary_provider", "boundary_provider_version",
    "boundary_status", "semver", "consumers", "external_consumers",
    "boundary_metadata",
    "version_errors", "exact_errors", "behavior_errors", "boundary_errors", "warnings",
    "vendored_copies", "vendored_digests", "vendored_errors",
)


def dump_lockfile(value: dict) -> str:
    """Render one lock under the same UTF-8 limit accepted by its loader."""
    if MAX_LOCKFILE_BYTES < 1:  # pragma: no cover - production invariant
        raise LockfileError("Lockfile storage limit must leave room for a newline")
    try:
        body = _bounded_json_dumps(
            value,
            indent=2,
            max_bytes=MAX_LOCKFILE_BYTES - 1,
        )
    except GuardrailError as exc:
        raise LockfileError(
            "Lockfile output exceeds the "
            f"{MAX_LOCKFILE_BYTES}-byte storage limit; no file was written. "
            "Reduce generated component or provider metadata before retrying."
        ) from exc
    return body + "\n"


def parse_lockfile_text(text: str, path_label: object = "lockfile") -> dict:
    """Parse lock JSON without silently accepting duplicate object keys."""
    try:
        value = strict_json_loads(text)
    except (ValueError, RecursionError, OverflowError) as exc:
        raise LockfileError(f"Lockfile is not valid JSON at {path_label}: {exc}") from exc
    if not isinstance(value, dict):
        raise LockfileError(
            f"Lockfile root must be an object, got {type(value).__name__}"
        )
    try:
        value_issues = _json_value_issues(value, path="lockfile")
    except RecursionError as exc:
        raise LockfileError(f"Lockfile is nested too deeply at {path_label}") from exc
    if value_issues:
        raise LockfileError(
            f"Lockfile contains values that cannot be represented as deterministic "
            f"JSON at {path_label}:\n" + "\n".join(value_issues)
        )
    return value


def parse_lockfile_bytes(data: bytes, path_label: object = "lockfile") -> dict:
    """Decode and parse a bounded UTF-8 lockfile."""
    if len(data) > MAX_LOCKFILE_BYTES:
        raise LockfileError(
            f"Lockfile exceeds the {MAX_LOCKFILE_BYTES}-byte limit at {path_label}"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LockfileError(
            f"Lockfile is not valid UTF-8 at {path_label}: {exc}"
        ) from exc
    return parse_lockfile_text(text, path_label)


def load_lockfile_file(
    path: Path,
    *,
    repo_root: Optional[Path] = None,
    snapshot: Optional[GitSourceSnapshot] = None,
) -> dict:
    """Load a lock from disk or one captured immutable Git source."""
    if snapshot is not None:
        if repo_root is None:
            raise LockfileError("repo_root is required for source-backed lock reads")
        label = _snapshot_relative_path(repo_root, path)
        entry = snapshot.entries.get(label)
        if entry is None:
            raise FileNotFoundError(
                f"Lockfile not found in captured {snapshot.source} source: {label}"
            )
        if entry.object_type != "blob" or entry.mode not in {"100644", "100755"}:
            raise LockfileError(
                f"Lockfile path must be a regular file in captured {snapshot.source} "
                f"source: {label} (mode={entry.mode}, type={entry.object_type})"
            )
        try:
            data = _git_cat_blob(
                repo_root,
                entry.oid,
                max_bytes=MAX_LOCKFILE_BYTES,
            )
        except GuardrailError as exc:
            raise LockfileError(
                f"Cannot read lockfile from captured {snapshot.source} source: "
                f"{label}: file too large or transport limit exceeded"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise LockfileError(
                f"Cannot read lockfile from captured {snapshot.source} source: "
                f"{label}"
            ) from exc
        return parse_lockfile_bytes(data, label)
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        data = _read_bounded_path_bytes(
            path,
            str(path),
            max_bytes=MAX_LOCKFILE_BYTES,
        )
    except GuardrailError as exc:
        raise LockfileError(
            f"Lockfile exceeds the {MAX_LOCKFILE_BYTES}-byte limit at {path}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise LockfileError(f"Cannot read lockfile {path}: {exc}") from exc
    return parse_lockfile_bytes(data, path)


def _normalize_source(source: Union[str, SourceMode]) -> str:
    value = source.value if isinstance(source, SourceMode) else source
    if value not in SOURCE_MODE_SET:
        raise ConfigError(
            "Unknown source mode "
            f"{_bounded_diagnostic_repr(value)}; expected head, index, or working-tree"
        )
    return value


def _normalized_semantic_path(value: object) -> object:
    """Normalize a validated config path without hiding semantic changes."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    return posixpath.normpath(stripped) if stripped else stripped


def _normalized_path_config(value: object) -> object:
    """Normalize path arrays while retaining provider-specific options."""
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    paths = normalized.get("paths")
    if isinstance(paths, list):
        normalized["paths"] = sorted(
            (_normalized_semantic_path(path) for path in paths),
            key=lambda item: canonical_json(item),
        )
    return normalized


def _semantic_config(config: dict) -> dict:
    """Return the canonical configuration that affects lock semantics.

    Schema URLs and object insertion order are presentation details. Lists used
    as sets are sorted. Custom-provider declaration order is retained
    conservatively because import-time interactions are environment-defined.
    """
    raw_defaults = config.get("defaults", {})
    defaults = raw_defaults if isinstance(raw_defaults, dict) else {}
    verify_facets = defaults.get("verify_facets")
    if isinstance(verify_facets, list):
        verify_facets = sorted(verify_facets, key=lambda item: canonical_json(item))
    semantic: dict = {
        "project": config.get("project", "unknown"),
        "providers": config.get("providers", []),
        "defaults": {
            **{
                key: value
                for key, value in defaults.items()
                if key not in {"compat_mode", "verify_facets"}
            },
            "compat_mode": defaults.get("compat_mode", "major"),
            "verify_facets": verify_facets,
        },
        "components": {},
        "slices": {},
    }

    components = config.get("components", {})
    if isinstance(components, dict):
        for name, raw_component in components.items():
            if not isinstance(raw_component, dict):
                semantic["components"][name] = raw_component
                continue
            component = {
                key: value
                for key, value in raw_component.items()
                if key not in {
                    "path", "ecosystem", "note", "boundary", "behavior", "version_source",
                    "vendored_copies", "consumers", "external_consumers",
                    "verify_facets",
                }
            }
            component["path"] = _normalized_semantic_path(
                raw_component.get("path")
            )
            normalized_boundary = _normalized_path_config(
                raw_component.get("boundary", {})
            )
            if isinstance(normalized_boundary, dict):
                # Human-facing annotations describe a declaration but do not
                # change provider selection, options, or generated lock data.
                normalized_boundary.pop("note", None)
            component["boundary"] = normalized_boundary
            component["behavior"] = _normalized_path_config(
                raw_component.get("behavior")
            )
            component["version_source"] = raw_component.get("version_source")
            vendored = raw_component.get("vendored_copies", [])
            if isinstance(vendored, list):
                vendored = sorted(
                    (_normalized_semantic_path(path) for path in vendored),
                    key=lambda item: canonical_json(item),
                )
            component["vendored_copies"] = vendored
            consumers = raw_component.get("consumers", [])
            if isinstance(consumers, list):
                consumers = sorted(consumers, key=lambda item: canonical_json(item))
            component["consumers"] = consumers
            external_consumers = raw_component.get("external_consumers", [])
            if isinstance(external_consumers, list):
                external_consumers = sorted(
                    external_consumers, key=lambda item: canonical_json(item)
                )
            component["external_consumers"] = external_consumers
            component_verify_facets = raw_component.get("verify_facets")
            if isinstance(component_verify_facets, list):
                component_verify_facets = sorted(
                    component_verify_facets,
                    key=lambda item: canonical_json(item),
                )
            if component_verify_facets is not None:
                component["verify_facets"] = component_verify_facets
            semantic["components"][name] = component

    slices = config.get("slices", {})
    if isinstance(slices, dict):
        for name, raw_slice in slices.items():
            if not isinstance(raw_slice, dict):
                semantic["slices"][name] = raw_slice
                continue
            slice_value = dict(raw_slice)
            slice_value.setdefault("description", "")
            slice_value.setdefault("mode", "exact")
            members = slice_value.get("components")
            if isinstance(members, list):
                slice_value["components"] = sorted(
                    members, key=lambda item: canonical_json(item)
                )
            semantic["slices"][name] = slice_value
    return semantic


def semantic_config_digest(config: dict) -> str:
    """Digest all configuration inputs that affect generated/verified locks."""
    try:
        value_issues = _json_value_issues(config)
    except RecursionError as exc:
        raise ConfigError("Config is nested too deeply to hash safely") from exc
    if value_issues:
        raise ConfigError(
            "Config cannot be hashed as deterministic JSON:\n"
            + "\n".join(value_issues)
        )
    try:
        payload = (
            f"{SEMANTIC_CONFIG_VERSION}\n"
            f"{canonical_json(_semantic_config(config))}"
        )
    except (RecursionError, UnicodeEncodeError, ValueError, TypeError) as exc:
        raise ConfigError(
            "Config cannot be hashed as deterministic JSON: "
            f"{exc}"
        ) from exc
    return sha256_hex(payload)


def generate_lockfile(
    config: dict, repo_root: Path, source: Union[str, SourceMode] = "head", strict: bool = True,
    allow_custom_providers: bool = False,
    snapshot: Optional[GitSourceSnapshot] = None,
) -> dict:
    """Generate the full lockfile from config + repo state."""
    source = _normalize_source(source)
    if not isinstance(config, dict):
        raise ConfigError("Config root must be an object")
    components_config = config.get("components")
    if not isinstance(components_config, dict) or not components_config:
        raise ConfigError("Config must define at least one component")
    tag_prefixes = []
    for component_name, component in components_config.items():
        if not isinstance(component, dict):
            continue
        version_source = component.get("version_source")
        if not isinstance(version_source, dict) or "git_tag_prefix" not in version_source:
            continue
        prefix = version_source.get("git_tag_prefix")
        prefix_error = git_tag_prefix_error(prefix)
        if prefix_error is not None:
            raise ConfigError(
                f"Component '{component_name}' version_source.git_tag_prefix "
                f"{prefix_error}"
            )
        tag_prefixes.append(prefix)
    slices_config = config.get("slices", {})
    if not isinstance(slices_config, dict):
        raise ConfigError("Config field 'slices' must be an object")
    for slice_name in sorted(slices_config):
        empty_slice_error = empty_explicit_slice_error(
            slice_name,
            slices_config[slice_name],
        )
        if empty_slice_error is not None:
            raise ConfigError(empty_slice_error)
    registry = create_registry()
    provider_errors = load_custom_providers(
        config.get("providers", []), allow_custom=allow_custom_providers,
        registry=registry,
    )
    if provider_errors:
        raise ProviderError("Custom provider loading failed:\n" + "\n".join(provider_errors))
    defaults = config.get("defaults", {})

    lockfile: dict = {
        "$schema": LOCKFILE_SCHEMA_URL,
        "schema": LOCKFILE_SCHEMA,
        "config_contract": SEMANTIC_CONFIG_VERSION,
        "config_digest": semantic_config_digest(config),
        "project": config.get("project", "unknown"),
        "components": {},
        "slices": {},
    }

    try:
        accessor = _SourceAccessor(repo_root, source, snapshot=snapshot)
    except ValueError as exc:
        raise ConfigError(f"Cannot capture {source} source: {exc}") from exc
    accessor.prime_latest_tags(tag_prefixes)

    # --- Components ---
    generation_errors = BoundedDiagnosticList()
    for name, comp in components_config.items():
        component_entry = _compute_component_entry(
            name, comp, repo_root, source, defaults, accessor, registry,
        )
        lockfile["components"][name] = component_entry
        generation_errors.extend(
            _generation_errors({"components": {name: component_entry}})
        )
        if generation_errors.truncated:
            break

    # ``strict=False`` (the CLI's ``--allow-partial``) relaxes only slice
    # requirements for intentional null facets.  It must never bless a
    # provider/computation failure into a lockfile that verify will reject.
    if generation_errors:
        raise ConfigError(
            "Lockfile generation failed:\n" + "\n".join(generation_errors)
        )

    # --- Slices ---
    for slice_name, slice_def in slices_config.items():
        lockfile["slices"][slice_name] = _recompute_slice_entry(
            slice_name, slice_def, lockfile["components"], strict=strict
        )

    return lockfile


# ---------------------------------------------------------------------------
# Source accessor — centralizes git-aware I/O callbacks
# ---------------------------------------------------------------------------

class _SourceAccessor:
    """Provides read_file, list_files, and version_read_file for a given source mode."""

    def __init__(
        self,
        repo_root: Path,
        source: str,
        snapshot: Optional[GitSourceSnapshot] = None,
    ):
        self.repo_root = repo_root
        self.source = _normalize_source(source)
        self._latest_tags: Dict[str, Optional[str]] = {}
        if snapshot is not None and snapshot.source != self.source:
            raise ValueError(
                f"Captured source mismatch: snapshot={snapshot.source!r}, "
                f"source={self.source!r}"
            )
        self.snapshot: Optional[GitSourceSnapshot] = snapshot
        if self.source in {"head", "index"}:
            if self.snapshot is None:
                self.snapshot = _capture_git_source_snapshot(repo_root, self.source)
            self.head_oid = self.snapshot.head_oid
        else:
            if self.snapshot is not None:
                raise ValueError(
                    "working-tree source does not accept an immutable Git snapshot"
                )
            try:
                tracking_snapshot = _capture_git_source_snapshot(repo_root, "index")
            except ValueError:
                if _is_git_repository(repo_root):
                    raise
                tracking_snapshot = None
                # The repository probe above already established that this is
                # the documented non-Git filesystem fallback.  Re-probing HEAD
                # would turn Git's expected non-repository rc=128 into an
                # operational error under the strict resolver.
                self.head_oid = None
            else:
                self.head_oid = tracking_snapshot.head_oid
            # Preserve the documented unborn/non-Git filesystem fallback when
            # there is no captured tracked state at all.
            if tracking_snapshot is not None and (
                tracking_snapshot.entries or tracking_snapshot.head_oid is not None
            ):
                self.snapshot = tracking_snapshot

    def _captured_entry(self, repo_rel: str):
        if self.snapshot is None:
            return None
        entry = self.snapshot.entries.get(repo_rel)
        if entry is None:
            raise ValueError(
                f"Path is absent from captured {self.source} tree: {repo_rel}"
            )
        if entry.object_type != "blob":
            raise ValueError(
                f"Expected Git blob at {repo_rel}, got {entry.object_type} "
                f"mode {entry.mode}"
            )
        return entry

    def read_file(self, repo_rel: str) -> bytes:
        """Read file bytes carrying canonical Git mode/type metadata."""
        return self.read_file_limited(repo_rel, MAX_HASH_FILE_BYTES)

    def read_file_limited(self, repo_rel: str, max_bytes: int) -> bytes:
        """Read source bytes without exceeding a caller-supplied hard limit."""
        if max_bytes < 0:
            raise ValueError("File byte limit must be non-negative")
        effective_limit = min(max_bytes, MAX_HASH_FILE_BYTES)
        src = self.source
        if src in {"head", "index"}:
            entry = self._captured_entry(repo_rel)
            data = _git_cat_blob(
                self.repo_root,
                entry.oid,
                max_bytes=effective_limit,
            )
            mode, object_type = entry.mode, entry.object_type
        else:
            full = self.repo_root / repo_rel
            tracked_entry = (
                self.snapshot.entries.get(repo_rel)
                if self.snapshot is not None
                else None
            )
            data = _read_path_content(
                self.repo_root,
                full,
                "working-tree",
                max_bytes=effective_limit,
                tracked_entry=tracked_entry,
                core_filemode=(
                    self.snapshot.filemode if self.snapshot is not None else True
                ),
                normalize=False,
            )
            mode = data.git_mode
            object_type = data.git_object_type
        _enforce_content_size(data, repo_rel)
        return _ModeAwareBytes(data, mode, object_type)

    def list_files(self, prefix: str) -> List[str]:
        """List files under a prefix."""
        if self.snapshot is not None:
            files = _snapshot_files(self.snapshot, prefix)
            if self.source == "working-tree":
                files = [
                    rel
                    for rel in files
                    if (self.repo_root / rel).exists()
                    or (self.repo_root / rel).is_symlink()
                ]
            return files
        return _list_files_for_source(self.repo_root, prefix, self.source)

    def latest_tag(self, repo_root: Path, prefix: str) -> Optional[str]:
        """Resolve tags against the HEAD commit captured for this operation."""
        if prefix not in self._latest_tags:
            self._latest_tags[prefix] = git_latest_tag(
                repo_root, prefix, ref=self.head_oid or "HEAD"
            )
        return self._latest_tags[prefix]

    def prime_latest_tags(self, prefixes: List[str]) -> None:
        """Resolve every configured tag prefix before component iteration."""
        for prefix in sorted(set(prefixes)):
            self.latest_tag(self.repo_root, prefix)

    def version_read_file(self, repo_rel: str) -> bytes:
        """Read file content for version extraction."""
        if self.source == "working-tree":
            if self.snapshot is not None and repo_rel not in self.snapshot.entries:
                raise ConfigError(
                    f"Version source is not tracked in the captured index: {repo_rel}"
                )
            fpath = self.repo_root / repo_rel
            if fpath.is_symlink():
                raise ConfigError(
                    f"Version source must not be a symlink: {repo_rel}"
                )
            try:
                fpath.resolve().relative_to(self.repo_root.resolve())
            except (ValueError, OSError) as exc:
                raise ConfigError(
                    f"Version source escapes repository: {repo_rel}"
                ) from exc
        data = self.read_file_limited(repo_rel, MAX_VERSION_FILE_BYTES)
        if getattr(data, "git_mode", None) == "120000":
            raise ConfigError(f"Version source must not be a symlink: {repo_rel}")
        return data


# ---------------------------------------------------------------------------
# Per-component fingerprint computation
# ---------------------------------------------------------------------------

def _compute_component_entry(
    name: str,
    comp: dict,
    repo_root: Path,
    source: str,
    defaults: dict,
    accessor: "_SourceAccessor",
    registry: Optional[dict] = None,
) -> dict:
    """Compute the lockfile entry for a single component."""
    raw_path = comp.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ConfigError(
            f"Component '{_bounded_diagnostic_text(name)}' has invalid or "
            "missing 'path'"
        )
    comp_path = _to_posix(os.path.normpath(raw_path.strip()))
    comp_path_display = _bounded_diagnostic_text(comp_path)
    version = extract_version(
        repo_root, comp_path, comp.get("version_source"), accessor.latest_tag,
        read_file_fn=accessor.version_read_file,
    )
    compat, api_ver, exact_ver = parse_semver(version)
    version_errors: List[str] = []
    if comp.get("version_source") is not None:
        if version is None:
            version_errors.append("Configured version source did not produce a version")
        elif compat is None:
            version_errors.append(
                "Configured version is not valid SemVer: "
                f"{_bounded_diagnostic_repr(version)}"
            )
    compat_mode = defaults.get("compat_mode", "major")

    # Exact fingerprint: git tree hash of the whole component directory
    exact_errors: List[str] = []
    try:
        exact_digest = source_tree_digest(
            repo_root, comp_path, source=source, snapshot=accessor.snapshot
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        exact_digest = None
        exact_errors.append(
            f"Exact digest failed: {_bounded_diagnostic_text(str(exc))}"
        )

    if exact_digest is None and not exact_errors:
        if source in ("head", "index"):
            exact_errors.append(
                f"No files found for '{comp_path_display}' at {source.upper()}. "
                f"Have you committed this path? Try --source working-tree"
            )
        else:
            exact_errors.append(
                f"No files found for '{comp_path_display}' on disk"
            )

    # API fingerprint: resolve via registered provider
    boundary = comp.get("boundary", {})
    bp_name = boundary_provider_name(boundary)
    provider = get_provider(bp_name, registry=registry)
    provider_metadata = None
    provider_version = None
    if provider is None:
        api_digest = None
        boundary_status = "error"
        boundary_errors: List[str] = [
            "Unknown boundary provider: " f"{_bounded_diagnostic_repr(bp_name)}"
        ]
    else:
        try:
            provider_version = provider.version
        except Exception as exc:
            api_digest = None
            boundary_status = "error"
            boundary_errors = [
                "Boundary provider version could not be read: "
                f"{exc.__class__.__name__}"
            ]
            provider_metadata = None
            provider = None
    if provider is not None:
        ctx = ProviderContext(
            repo_root=repo_root,
            component_path=comp_path,
            boundary_cfg=boundary,
            source=source,
            read_file=accessor.read_file,
            read_file_limited=accessor.read_file_limited,
            list_files=accessor.list_files,
        )
        try:
            api_digest, boundary_status, boundary_errors, provider_metadata = compute_boundary(
                provider, ctx, include_metadata=True
            )
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            api_digest = None
            boundary_status = "error"
            boundary_errors = [
                "Boundary digest failed: "
                f"{_bounded_diagnostic_text(str(exc))}"
            ]
            provider_metadata = None
    boundary_errors = list(BoundedDiagnosticList(boundary_errors))

    # Behavior fingerprint: broader contract files (superset of boundary)
    behavior_cfg = comp.get("behavior")
    behavior_digest = None
    if behavior_cfg and behavior_cfg.get("paths"):
        behavior_provider = PathHashProvider()
        behavior_provider.name = "behavior"
        behavior_ctx = ProviderContext(
            repo_root=repo_root,
            component_path=comp_path,
            boundary_cfg=behavior_cfg,
            source=source,
            read_file=accessor.read_file,
            read_file_limited=accessor.read_file_limited,
            list_files=accessor.list_files,
        )
        try:
            behavior_digest, _bstatus, _berrs = compute_boundary(behavior_provider, behavior_ctx)
            behavior_errors = list(_berrs) if _bstatus == "error" else []
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            behavior_digest = None
            behavior_errors = [
                "Behavior digest failed: "
                f"{_bounded_diagnostic_text(str(exc))}"
            ]
    else:
        behavior_errors = []
    behavior_errors = list(BoundedDiagnosticList(behavior_errors))

    # A behavior contract is a cryptographic superset of the boundary contract.
    # It cannot remain unchanged when its boundary changes, even when the two
    # providers select disjoint labels by mistake.
    if behavior_digest is not None:
        boundary_identity = (
            api_digest if api_digest is not None else f"none:{boundary_status}"
        )
        behavior_digest = _hash_framed_entries(
            [
                ("behavior", behavior_digest.encode("ascii")),
                ("boundary", boundary_identity.encode("ascii")),
            ],
            domain=HASH_DOMAIN_BEHAVIOR,
        )

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
        "boundary_provider": bp_name,
        "boundary_provider_version": provider_version,
        "boundary_status": boundary_status,
        "consumers": sorted(set(comp.get("consumers", []))),
        "external_consumers": sorted(set(comp.get("external_consumers", []))),
        "fingerprints": {
            "exact": exact_digest,
            "behavior": behavior_digest,
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
    if provider_metadata is not None:
        entry["boundary_metadata"] = provider_metadata
    if version_errors:
        entry["version_errors"] = version_errors
    if exact_errors:
        entry["exact_errors"] = exact_errors
    if behavior_errors:
        entry["behavior_errors"] = behavior_errors

    # Flag vendored copies
    if "vendored_copies" in comp:
        entry["vendored_copies"] = comp["vendored_copies"]
        vendored_errors = BoundedDiagnosticList()
        source_content_hash = _content_only_digest(
            repo_root, comp_path, source=source, snapshot=accessor.snapshot
        )
        if source_content_hash is None:
            vendored_errors.append(
                "Vendored source path "
                f"'{comp_path_display}' has no files at {source}"
            )
        for vc in comp["vendored_copies"]:
            if vendored_errors.truncated:
                break
            vc_content_hash = _content_only_digest(
                repo_root,
                vc.rstrip("/"),
                source=source,
                snapshot=accessor.snapshot,
            )
            if vc_content_hash is None:
                vendored_errors.append(
                    "Vendored copy at "
                    f"'{_bounded_diagnostic_text(vc)}' has no files at {source}"
                )
                continue
            entry.setdefault("vendored_digests", {})[vc] = vc_content_hash
            if source_content_hash is not None and vc_content_hash != source_content_hash:
                vendored_errors.append(
                    "Vendored copy at "
                    f"'{_bounded_diagnostic_text(vc)}' differs from source "
                    f"(source={source_content_hash}, copy={vc_content_hash})"
                )
        if vendored_errors:
            entry["vendored_errors"] = list(vendored_errors)

    return entry


def _generation_errors(lockfile: dict) -> List[str]:
    """Return fingerprint computation failures that make a lock unsafe to bless."""
    errors = BoundedDiagnosticList()

    def append_messages(name: str, messages: List[str]) -> None:
        for message in messages:
            if message == DIAGNOSTIC_TRUNCATION_SENTINEL:
                errors.append(DIAGNOSTIC_TRUNCATION_SENTINEL)
            else:
                errors.append(
                    f"{name}: {_bounded_diagnostic_text(message)}"
                )
            if errors.truncated:
                break

    for name, entry in lockfile.get("components", {}).items():
        if errors.truncated:
            break
        name = _bounded_diagnostic_text(name)
        append_messages(name, entry.get("version_errors", []))
        append_messages(name, entry.get("exact_errors", []))
        append_messages(name, entry.get("behavior_errors", []))
        append_messages(name, entry.get("vendored_errors", []))
        boundary_status = entry.get("boundary_status")
        boundary_provider = entry.get("boundary_provider")
        if boundary_status == "error":
            messages = entry.get("boundary_errors", []) or ["Boundary computation failed"]
            append_messages(name, messages)
        elif boundary_status == "partial" and boundary_provider != "implicit":
            # ``partial`` is an intentional built-in state only for the
            # implicit provider, which publishes no separate boundary.  A
            # custom provider's partial output is incomplete executable-policy
            # output and must not be blessed merely because it supplied some
            # hashable entries.
            messages = entry.get("boundary_errors", []) or [
                "Boundary provider returned an incomplete partial result"
            ]
            append_messages(name, messages)
    return list(errors)


def _diagnostic_list_preview(values: List[str], *, limit: int = 8) -> str:
    """Render a deterministic bounded preview of a pre-ordered string list."""
    rendered = ", ".join(
        _bounded_diagnostic_text(value) for value in values[:limit]
    )
    if len(values) > limit:
        rendered += f", +{len(values) - limit} more"
    return rendered


def _recompute_slice_entry(
    slice_name: str,
    slice_def: dict,
    components_map: Dict[str, dict],
    strict: bool = True,
    graph_components: Optional[Dict[str, dict]] = None,
) -> dict:
    empty_slice_error = empty_explicit_slice_error(slice_name, slice_def)
    if empty_slice_error is not None:
        raise ConfigError(empty_slice_error)
    mode = slice_def.get("mode", "exact")
    component_names = resolve_slice_components(
        slice_def,
        graph_components if graph_components is not None else components_map,
    )
    digest_parts: Dict[str, Optional[str]] = {}
    for cname in sorted(component_names):
        comp_entry = components_map.get(cname)
        if comp_entry is None:
            digest_parts[cname] = None
            continue
        fp = comp_entry.get("fingerprints", {})
        if mode == "exact":
            digest_parts[cname] = fp.get("exact")
        elif mode == "behavior":
            if fp.get("behavior") is None and strict:
                raise ConfigError(f"Slice '{slice_name}' requires behavior digest for component '{cname}'")
            digest_parts[cname] = fp.get("behavior")
        elif mode == "boundary":
            if fp.get("boundary") is None and strict:
                raise ConfigError(f"Slice '{slice_name}' requires boundary digest for component '{cname}'")
            digest_parts[cname] = fp.get("boundary")
        elif mode == "compat":
            if fp.get("compat") is None and strict:
                raise ConfigError(f"Slice '{slice_name}' requires compat digest for component '{cname}'")
            digest_parts[cname] = fp.get("compat")
        else:
            raise ConfigError(f"Unknown slice mode: {mode}")
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
    snapshot: Optional[GitSourceSnapshot] = None,
    existing_lockfile: Optional[dict] = None,
    running_version: Optional[str] = None,
) -> dict:
    """Generate/update lockfile only for selected components and impacted slices."""
    source = _normalize_source(source)
    if snapshot is not None and snapshot.source != source:
        raise ConfigError(
            f"Captured source mismatch: snapshot={snapshot.source!r}, source={source!r}"
        )
    if source in {"head", "index"} and snapshot is None:
        try:
            snapshot = _capture_git_source_snapshot(repo_root, source)
        except ValueError as exc:
            raise ConfigError(f"Cannot capture {source} source: {exc}") from exc
    selected = sorted(set(selected_components))
    components_cfg = config.get("components", {})
    missing = [n for n in selected if n not in components_cfg]
    if missing:
        raise ConfigError(f"Unknown component(s): {', '.join(missing)}")

    if existing_lockfile is None:
        try:
            merged = load_lockfile_file(
                out_path,
                repo_root=repo_root,
                snapshot=snapshot if source in {"head", "index"} else None,
            )
        except FileNotFoundError as exc:
            raise ConfigError(
                f"Cannot generate a component subset without an existing "
                f"{LOCKFILE_SCHEMA} lockfile in the selected {source} source. "
                "Run a full `boundver generate` first."
            ) from exc
        except LockfileError as exc:
            raise ConfigError(f"Cannot read existing lockfile at {out_path}: {exc}") from exc
    else:
        merged = existing_lockfile
    # Work on a detached JSON tree so callers' parsed source lock is never
    # mutated while composing a working-tree output.
    try:
        merged = json.loads(
            _bounded_json_dumps(merged, allow_nan=False),
            parse_int=_bounded_json_int,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ConfigError(f"Existing lockfile cannot be copied safely: {exc}") from exc
    if not isinstance(merged, dict):
        raise ConfigError(
            f"Existing lockfile at {out_path} must contain a JSON object"
        )
    if merged.get("schema") != LOCKFILE_SCHEMA:
        raise ConfigError(
            "Cannot partially update schema "
            f"{_bounded_diagnostic_repr(merged.get('schema'))}; "
            f"run a full `boundver generate` to create {LOCKFILE_SCHEMA}."
        )
    structure_issues = _lockfile_structure_issues(
        merged,
        running_version=running_version,
    )
    if structure_issues:
        raise ConfigError(
            "Cannot partially update the selected lockfile:\n"
            + "\n".join(structure_issues)
        )
    if merged.get("project") != config.get("project", "unknown"):
        raise ConfigError(
            "Cannot partially update after the project name changed. "
            "Run a full `boundver generate`."
        )

    # Recompute every component before merging. This makes component-scoped
    # generation an output-selection convenience, not a way to preserve stale
    # digests after config/default/provider changes.
    current_lock = generate_lockfile(
        config, repo_root, source=source, strict=strict,
        allow_custom_providers=allow_custom_providers,
        snapshot=snapshot,
    )
    for name in set(components_cfg) - set(selected):
        if merged.get("components", {}).get(name) != current_lock["components"][name]:
            raise ConfigError(
                f"Cannot partially generate because unselected component '{name}' "
                "is stale. Run a full `boundver generate`."
            )
    merged["$schema"] = LOCKFILE_SCHEMA_URL
    merged["schema"] = LOCKFILE_SCHEMA
    merged["config_contract"] = SEMANTIC_CONFIG_VERSION
    merged["config_digest"] = current_lock["config_digest"]
    merged["project"] = config.get("project", "unknown")
    if not isinstance(merged.get("components"), dict):
        merged["components"] = {}
    if not isinstance(merged.get("slices"), dict):
        merged["slices"] = {}

    for name in selected:
        merged["components"][name] = current_lock["components"][name]

    # A partial refresh must still reconcile config removals and every slice.
    configured_names = set(components_cfg)
    merged["components"] = {
        name: entry
        for name, entry in merged["components"].items()
        if name in configured_names
    }
    missing_entries = sorted(configured_names - set(merged["components"]))
    if missing_entries:
        raise ConfigError(
            "Cannot partially generate because the existing lockfile has no entry for: "
            + _diagnostic_list_preview(missing_entries)
            + ". Run a full `boundver generate`."
        )

    merged["slices"] = {}
    for sname, sdef in config.get("slices", {}).items():
        merged["slices"][sname] = _recompute_slice_entry(
            sname, sdef, merged["components"], strict=strict
        )

    # Keep component-scoped generation symmetric with full generation:
    # ``strict=False`` permits intentional null slice inputs, not digest
    # computation failures.
    errors = _generation_errors(merged)
    if errors:
        raise ConfigError("Lockfile generation failed:\n" + "\n".join(errors))

    return merged


def _lockfile_schema_issues(lockfile: dict) -> List[str]:
    """Compatibility wrapper for the lockfile validation subsystem."""
    return _lockfile_schema_issues_impl(lockfile, LOCKFILE_SCHEMA)


def _is_sha256_digest(value: object) -> bool:
    """Compatibility wrapper for canonical digest validation."""
    return _is_sha256_digest_impl(value)


def _lockfile_structure_issues(
    lockfile: dict,
    *,
    allowed_config_contracts: Optional[Set[str]] = None,
    running_version: Optional[str] = None,
) -> List[str]:
    """Compatibility wrapper for complete structural lock validation."""
    return _lockfile_structure_issues_impl(
        lockfile,
        semantic_config_version=SEMANTIC_CONFIG_VERSION,
        facets=FACETS,
        component_metadata_fields=COMPONENT_METADATA_FIELDS,
        expected_schema=LOCKFILE_SCHEMA,
        allowed_config_contracts=allowed_config_contracts,
        running_version=running_version,
    )


def verify_lockfile(
    config: dict,
    lockfile: dict,
    repo_root: Path,
    source: Union[str, SourceMode] = "head",
    components_filter: Optional[List[str]] = None,
    allow_custom_providers: bool = False,
    fail_fast: bool = False,
    facets: Optional[List[str]] = None,
    observations: Optional[List[str]] = None,
    snapshot: Optional[GitSourceSnapshot] = None,
    transitive_consumers: bool = False,
    consumer_impact: Optional[List[dict]] = None,
) -> List[str]:
    """Check if the lockfile matches current repo state. Returns list of mismatches.

    When *fail_fast* is True, all selected components are still evaluated so the
    process can return the globally highest-severity exit condition; only the
    returned mismatch report is limited to one item.
    """
    source = _normalize_source(source)
    # Compute all selected entries even when only one issue is requested. This
    # is necessary to preserve the global highest-severity exit-code contract
    # across components; ``fail_fast`` limits the returned report, not safety
    # evaluation.
    limit_report = fail_fast
    fail_fast = False

    # Load custom providers once up front.
    if not isinstance(config, dict):
        return ["Config root must be an object"]
    if not isinstance(lockfile, dict):
        return ["LOCKFILE malformed: root must be an object"]
    if not isinstance(config.get("components"), dict) or not config.get("components"):
        return ["Config malformed: components must be a non-empty object"]
    if not isinstance(config.get("slices", {}), dict):
        return ["Config malformed: slices must be an object"]
    for slice_name in sorted(config.get("slices", {})):
        empty_slice_error = empty_explicit_slice_error(
            slice_name,
            config["slices"][slice_name],
        )
        if empty_slice_error is not None:
            return [f"Config invalid: {empty_slice_error}"]
    registry = create_registry()
    provider_errors = load_custom_providers(
        config.get("providers", []), allow_custom=allow_custom_providers,
        registry=registry,
    )
    if provider_errors:
        return list(
            BoundedDiagnosticList(
                f"Custom provider loading failed: "
                f"{_bounded_diagnostic_text(error)}"
                for error in provider_errors
            )
        )

    issues = BoundedDiagnosticList(_lockfile_schema_issues(lockfile))
    issues.extend(_lockfile_structure_issues(lockfile))
    if issues:
        return list(issues)
    current_config_digest = semantic_config_digest(config)
    if lockfile.get("config_digest") != current_config_digest:
        issues.append(
            "METADATA MISMATCH config_digest: "
            f"lockfile={_bounded_diagnostic_repr(lockfile.get('config_digest'))} "
            f"current={_bounded_diagnostic_repr(current_config_digest)}"
        )
    if lockfile.get("project") != config.get("project", "unknown"):
        issues.append(
            "METADATA MISMATCH project: lockfile="
            f"{_bounded_diagnostic_repr(lockfile.get('project'))} current="
            f"{_bounded_diagnostic_repr(config.get('project', 'unknown'))}"
        )
        if fail_fast:
            return issues

    selected = set(components_filter or [])
    use_filter = len(selected) > 0
    supported_facets = FACET_SET
    explicit_gated_facets: Optional[Set[str]] = (
        set(facets) if facets is not None else None
    )
    raw_defaults = config.get("defaults", {})
    has_explicit_default_facets = (
        isinstance(raw_defaults, dict) and "verify_facets" in raw_defaults
    )
    raw_default_facets = raw_defaults.get("verify_facets", [])
    default_gated_facets: Set[str] = (
        set(raw_default_facets)
        if isinstance(raw_default_facets, list)
        and all(isinstance(item, str) for item in raw_default_facets)
        else set()
    )
    configured_gated_facets = set(default_gated_facets)
    if explicit_gated_facets is None:
        for component in config.get("components", {}).values():
            if isinstance(component, dict) and isinstance(
                component.get("verify_facets"), list
            ):
                configured_gated_facets.update(component["verify_facets"])
    else:
        configured_gated_facets = explicit_gated_facets
    unknown_facets = configured_gated_facets - supported_facets
    if unknown_facets:
        return [
            "Unknown verification facet(s): "
            + _diagnostic_list_preview(sorted(unknown_facets))
        ]
    non_gating = BoundedDiagnosticList(observations or [])

    def truncated_issue_result() -> List[str]:
        if observations is not None:
            observations[:] = list(non_gating)
        if limit_report:
            return [DIAGNOSTIC_TRUNCATION_SENTINEL]
        return list(issues)

    # Determine which components to check.
    all_components = config.get("components", {})
    unknown_components = selected - set(all_components)
    if unknown_components:
        unknown_component_names = sorted(unknown_components)
        return [
            "Unknown verification component(s): "
            + _diagnostic_list_preview(unknown_component_names)
        ]
    if use_filter:
        check_components = {n: all_components[n] for n in components_filter if n in all_components}
    else:
        check_components = all_components

    defaults = config.get("defaults", {})
    try:
        accessor = _SourceAccessor(repo_root, source, snapshot=snapshot)
    except ValueError as exc:
        return [
            f"Cannot capture {source} source: {_bounded_diagnostic_text(str(exc))}"
        ]

    # Per-component verification with optional early exit.
    computed_entries: Dict[str, dict] = {}
    for name, comp_cfg in check_components.items():
        if issues.truncated:
            return truncated_issue_result()
        display_name = _bounded_diagnostic_text(name)
        component_gated_facets = explicit_gated_facets
        configured_component_facets = comp_cfg.get("verify_facets")
        if component_gated_facets is None:
            if isinstance(configured_component_facets, list):
                component_gated_facets = set(configured_component_facets)
            elif has_explicit_default_facets:
                component_gated_facets = default_gated_facets
            else:
                component_gated_facets = _available_component_facets(comp_cfg)
        component_availability_is_required = (
            explicit_gated_facets is not None
            or isinstance(configured_component_facets, list)
            or has_explicit_default_facets
        )
        current_comp = _compute_component_entry(
            name, comp_cfg, repo_root, source, defaults, accessor, registry
        )
        computed_entries[name] = current_comp
        locked_comp = lockfile.get("components", {}).get(name)
        if locked_comp is None:
            issues.append(f"NEW component not in lockfile: {display_name}")
            if fail_fast:
                return issues
            continue
        current_errors = _generation_errors({"components": {name: current_comp}})
        locked_errors = _generation_errors({"components": {name: locked_comp}})
        for message in current_errors:
            issues.append(
                f"CURRENT DIGEST ERROR {_bounded_diagnostic_text(message)}"
            )
            if issues.truncated:
                break
            if fail_fast:
                return issues
        for message in locked_errors:
            issues.append(
                f"LOCKED DIGEST ERROR {_bounded_diagnostic_text(message)}"
            )
            if issues.truncated:
                break
            if fail_fast:
                return issues
        # Highest policy severity first keeps --fail-fast compatible with the
        # documented exit-code contract.
        for facet in ("compat", "boundary", "behavior", "exact"):
            cv = current_comp["fingerprints"].get(facet)
            locked_fps = locked_comp.get("fingerprints", {})
            lv = locked_fps.get(facet)
            if (
                component_availability_is_required
                and facet in component_gated_facets
                and (cv is None or lv is None)
            ):
                issues.append(
                    f"UNAVAILABLE FACET {display_name}.{facet}: selected gate requires "
                    "both locked and current digests"
                )
                if fail_fast:
                    return issues
                # A null/non-null pair is already explained by this controlled
                # policy error; do not also classify it as ordinary drift.
                continue
            if cv != lv:
                message = (
                    f"MISMATCH {display_name}.{facet}: "
                    f"lockfile={_short(lv)} current={_short(cv)}"
                )
                if facet in component_gated_facets:
                    issues.append(message)
                    if facet in {"boundary", "compat"}:
                        groups = affected_consumer_groups(
                            all_components,
                            name,
                            transitive=transitive_consumers,
                        )
                        if consumer_impact is not None:
                            existing = (
                                consumer_impact[-1]
                                if consumer_impact
                                and isinstance(consumer_impact[-1], dict)
                                and consumer_impact[-1].get("component") == name
                                else None
                            )
                            if existing is None:
                                consumer_impact.append(
                                    {
                                        "component": name,
                                        "facets": [facet],
                                        "components": groups["components"],
                                        "external_consumers": groups[
                                            "external_consumers"
                                        ],
                                        "transitive": transitive_consumers,
                                    }
                                )
                            elif facet not in existing["facets"]:
                                existing["facets"].append(facet)
                                existing["facets"].sort()
                        consumers = affected_consumers(
                            all_components,
                            name,
                            transitive=transitive_consumers,
                        )
                        if consumers:
                            qualifier = " (TRANSITIVE)" if transitive_consumers else ""
                            consumer_preview = _diagnostic_list_preview(consumers)
                            issues.append(
                                f"AFFECTED CONSUMERS{qualifier} {display_name}: "
                                f"{consumer_preview}"
                            )
                    if fail_fast:
                        return issues
                else:
                    non_gating.append(message)

        for field in COMPONENT_METADATA_FIELDS:
            if locked_comp.get(field) != current_comp.get(field):
                locked_value = _bounded_diagnostic_repr(locked_comp.get(field))
                current_value = _bounded_diagnostic_repr(current_comp.get(field))
                issues.append(
                    f"METADATA MISMATCH {display_name}.{field}: "
                    f"lockfile={locked_value} current={current_value}"
                )
                if fail_fast:
                    return issues

        # Check for vendored copy drift
        for warning in current_comp.get("warnings", []):
            issues.append(
                f"VENDORED DRIFT {display_name}: "
                f"{_bounded_diagnostic_text(warning)}"
            )
            if issues.truncated:
                break
            if fail_fast:
                return issues

    for name in lockfile.get("components", {}):
        if issues.truncated:
            return truncated_issue_result()
        if name not in check_components:
            if name in all_components:
                continue
            issues.append(
                "REMOVED component still in lockfile: "
                f"{_bounded_diagnostic_text(name)}"
            )
            if fail_fast:
                return issues

    # Check every slice affected by the selected components. This prevents a
    # component-filtered verify from silently ignoring its aggregate contract.
    slices_config = config.get("slices", {})
    if slices_config:
        slice_component_names = set()
        for sdef in slices_config.values():
            slice_component_names.update(
                resolve_slice_components(sdef, all_components)
            )
        for cname in sorted(slice_component_names):
            if cname not in computed_entries and cname in all_components:
                computed_entries[cname] = _compute_component_entry(
                    cname, all_components[cname], repo_root, source, defaults, accessor, registry
                )
        for sname, sdef in slices_config.items():
            if issues.truncated:
                return truncated_issue_result()
            resolved_slice_components = set(
                resolve_slice_components(sdef, all_components)
            )
            if use_filter and not (selected & resolved_slice_components):
                continue
            current_slice = _recompute_slice_entry(
                sname,
                sdef,
                computed_entries,
                strict=False,
                graph_components=all_components,
            )
            locked_slice = lockfile.get("slices", {}).get(sname)
            if locked_slice is None:
                issues.append(
                    "NEW slice not in lockfile: "
                    f"{_bounded_diagnostic_text(sname)}"
                )
                if fail_fast:
                    return issues
            else:
                slice_mode = sdef.get("mode", "exact")
                if explicit_gated_facets is not None:
                    slice_gated_facets = explicit_gated_facets
                    slice_availability_is_required = True
                else:
                    # A slice gate follows the effective policy of its
                    # members. One member selecting the slice mode is enough
                    # to require a usable aggregate for that mode.
                    slice_gated_facets = set()
                    slice_availability_is_required = False
                    for cname in resolved_slice_components:
                        member_cfg = all_components.get(cname, {})
                        member_facets = (
                            member_cfg.get("verify_facets")
                            if isinstance(member_cfg, dict)
                            else None
                        )
                        if isinstance(member_facets, list):
                            effective_member_facets = set(member_facets)
                        elif has_explicit_default_facets:
                            effective_member_facets = default_gated_facets
                        else:
                            effective_member_facets = (
                                _available_component_facets(member_cfg)
                                if isinstance(member_cfg, dict)
                                else set()
                            )
                        slice_gated_facets.update(effective_member_facets)
                        if (
                            isinstance(member_facets, list)
                            or has_explicit_default_facets
                        ):
                            slice_availability_is_required = True
                locked_digests = locked_slice.get("component_digests", {})
                current_digests = current_slice.get("component_digests", {})
                unavailable_members = sorted(
                    cname
                    for cname in resolved_slice_components
                    if (
                        cname in locked_digests
                        and locked_digests[cname] is None
                    )
                    or (
                        cname in current_digests
                        and current_digests[cname] is None
                    )
                )
                if (
                    slice_availability_is_required
                    and slice_mode in slice_gated_facets
                    and unavailable_members
                ):
                    issues.append(
                        f"UNAVAILABLE FACET {_bounded_diagnostic_text(sname)}."
                        f"{slice_mode}: slice members "
                        "lack locked or current digests: "
                        + _diagnostic_list_preview(unavailable_members)
                    )
                    if fail_fast:
                        return issues
                    continue
            if locked_slice is not None and locked_slice != current_slice:
                message = (
                    f"SLICE MISMATCH {_bounded_diagnostic_text(sname)}."
                    f"{sdef.get('mode', 'exact')}: "
                    f"lockfile={_short(locked_slice.get('fingerprint'))} "
                    f"current={_short(current_slice.get('fingerprint'))}"
                )
                if sdef.get("mode", "exact") in slice_gated_facets:
                    issues.append(message)
                    if fail_fast:
                        return issues
                else:
                    non_gating.append(message)
    for sname in lockfile.get("slices", {}):
        if issues.truncated:
            return truncated_issue_result()
        if sname not in slices_config:
            issues.append(
                f"REMOVED slice still in lockfile: "
                f"{_bounded_diagnostic_text(sname)}"
            )
            if fail_fast:
                return issues

    if observations is not None:
        observations[:] = list(non_gating)
    if limit_report and issues:
        if DIAGNOSTIC_TRUNCATION_SENTINEL in issues:
            return [DIAGNOSTIC_TRUNCATION_SENTINEL]
        safety_prefixes = (
            "Config root",
            "LOCKFILE",
            "Custom provider loading failed",
            "Unknown verification facet",
            "Unknown verification component",
            "CURRENT DIGEST ERROR",
            "LOCKED DIGEST ERROR",
            "UNAVAILABLE FACET",
        )
        safety_issues = [
            issue for issue in issues if issue.startswith(safety_prefixes)
        ]
        if safety_issues:
            # A usage/integrity failure is not ordinary drift and must never be
            # hidden by a numerically higher facet severity.  In particular,
            # callers must see unavailable selected facets before considering
            # --update safe.
            return [safety_issues[0]]

        def _severity(message: str) -> int:
            return {
                "exact": 1,
                "behavior": 3,
                "boundary": 4,
                "compat": 5,
            }.get(_issue_facet(message), 1)

        return [max(issues, key=_severity)]
    return list(issues)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

# All known schemas. Hash-bearing older locks are recognized only so their
# rejection can explain that repository content must be regenerated.
KNOWN_SCHEMAS = frozenset({"boundary-lock/v1", "boundary-lock/v2", "boundary-lock/v3"})


class MigrationError(ValueError):
    """Raised when a lockfile cannot be migrated to the current schema."""


def migrate_lockfile(lockfile: dict) -> dict:
    """Normalize a current lock and reject incompatible hash contracts.

    Returns a new dict; does not mutate the input.
    Raises ``MigrationError`` if the schema is absent, unrecognised, or needs
    repository content to regenerate its fingerprints.

    Hash contract v1/v2 lockfiles and v3 locks with an older semantic-config
    contract cannot be mechanically upgraded because their fingerprints must
    be recomputed from repository content.
    """
    schema = lockfile.get("schema")
    if schema is None:
        raise MigrationError(
            "Lockfile has no 'schema' field — cannot determine version to migrate from."
        )
    if not isinstance(schema, str) or schema not in KNOWN_SCHEMAS:
        raise MigrationError(
            "Unknown lockfile schema "
            f"{_bounded_diagnostic_repr(schema)}. "
            f"Supported: {', '.join(sorted(KNOWN_SCHEMAS))}. "
            "You may need to upgrade boundver."
        )
    if schema in {"boundary-lock/v1", "boundary-lock/v2"}:
        raise MigrationError(
            f"{schema} does not bind every file's Git mode/type and semantic "
            "configuration, so it cannot be migrated without repository content. "
            f"Run `boundver generate` to create a {LOCKFILE_SCHEMA} lockfile."
        )
    config_contract = lockfile.get("config_contract")
    if config_contract != SEMANTIC_CONFIG_VERSION:
        actual = config_contract if isinstance(config_contract, str) else "missing"
        raise MigrationError(
            f"{schema} uses semantic configuration contract "
            f"{_bounded_diagnostic_repr(actual)}, but this "
            f"release requires {SEMANTIC_CONFIG_VERSION!r}. Semantic digests "
            "cannot be relabelled or migrated without repository content. Run "
            f"`boundver generate` to create a current {LOCKFILE_SCHEMA} lockfile."
        )
    migrated = dict(lockfile)
    migrated.pop("generated_at", None)          # legacy field removed in v1 final
    migrated["schema"] = LOCKFILE_SCHEMA        # normalise to current constant
    migrated.setdefault("components", {})
    migrated.setdefault("slices", {})
    return migrated
