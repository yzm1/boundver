"""Read-only comparison with an operator-asserted local deployment reference."""

from pathlib import Path
import subprocess

from ._config import find_config_file, load_config_file, validate_config
from ._git import _capture_git_ref_snapshot, _capture_git_source_snapshot
from ._lockfile import (
    LOCKFILE_SCHEMA, SEMANTIC_CONFIG_VERSION, _generation_errors,
    _lockfile_schema_issues, _lockfile_structure_issues,
    generate_lockfile, load_lockfile_file, semantic_config_digest, verify_lockfile,
)
from ._review import _config_lock_consistency_issues
from ._utils import BoundverError, ConfigError, FACETS, SOURCE_MODE_SET, _DiagnosticTruncationMarker, _bounded_diagnostic_text, _bounded_exception_text, _bounded_json_dumps


DEPLOYMENT_SCHEMA = "boundver-deployment-comparison/v1"
MAX_DEPLOYMENT_RESULT_BYTES = 4 * 1024 * 1024
MAX_DEPLOYMENT_ROWS = 10_000
MAX_DEPLOYMENT_ISSUES = 100
MAX_DEPLOYMENT_ISSUE_CHARS = 1024
MAX_DEPLOYMENT_ENVIRONMENT_CHARS = 128


def _selector(value):
    if not isinstance(value, str) or not value or len(value) > 16_384:
        raise ConfigError("Deployment input selector must be a bounded relative file path")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or any(char in normalized for char in ":*?[]{}\0"):
        raise ConfigError("Deployment input selector must be a literal repository-relative file path")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ConfigError("Deployment input selector cannot traverse directories")
    return normalized


def _issue(result, messages):
    for message in messages:
        result["issues"]["truncated"] |= isinstance(message, _DiagnosticTruncationMarker)
        if len(result["issues"]["items"]) >= MAX_DEPLOYMENT_ISSUES:
            result["issues"]["omitted"] += 1
            result["issues"]["truncated"] = True
            continue
        text = str(message)
        rendered = _bounded_diagnostic_text(text, max_chars=MAX_DEPLOYMENT_ISSUE_CHARS)
        # UTF-8 backslash replacement can expand lone surrogate characters.
        prefix = text[:MAX_DEPLOYMENT_ISSUE_CHARS + 1]
        expanded = prefix.encode("utf-8", errors="backslashreplace").decode("utf-8")
        result["issues"]["truncated"] |= len(expanded) > MAX_DEPLOYMENT_ISSUE_CHARS
        result["issues"]["items"].append(rendered)


def compare_deployment(
    repo_root: Path, environment: str, reference_ref: str, *, source="head",
    config_hint="boundary.config.json", reference_config_hint=None,
    reference_lock_hint="boundary.lock.json", facets=None, limit=1000,
    allow_custom_providers=False,
) -> dict:
    """Compare declared contracts, not actual cloud deployments or a current lock."""
    if not isinstance(environment, str) or not environment or environment != environment.strip() or len(environment) > MAX_DEPLOYMENT_ENVIRONMENT_CHARS or not environment.isprintable():
        raise ConfigError("Environment label must be trimmed printable text of 1..128 characters")
    if not isinstance(reference_ref, str) or not reference_ref or len(reference_ref) > 16_384 or not reference_ref.isprintable():
        raise ConfigError("An explicit bounded printable deployment ref is required")
    if not isinstance(source, str) or source not in SOURCE_MODE_SET or type(limit) is not int or not 0 <= limit <= MAX_DEPLOYMENT_ROWS:
        raise ConfigError("Invalid deployment comparison source or presentation limit")
    if facets is not None and (not isinstance(facets, list) or not facets or any(facet not in FACETS for facet in facets)):
        raise ConfigError("Deployment facets must be a nonempty selection of known facets")
    config_hint = _selector(config_hint)
    reference_config_hint = _selector(config_hint if reference_config_hint is None else reference_config_hint)
    reference_lock_hint = _selector(reference_lock_hint)
    result = {
        "schema": DEPLOYMENT_SCHEMA, "environment": environment,
        "evidence": {"kind": "operator-asserted-local-ref", "attested": False},
        "current": {"mode": source, "commit": None, "tree": None, "config": None, "config_digest": None},
        "reference": {"ref": reference_ref, "commit": None, "tree": None, "config": None,
                      "lock": reference_lock_hint, "lock_schema": None, "config_contract": None, "verified": False},
        "current_lock_verification": "not-performed", "complete": False, "ok": False,
        "exit_code": 2, "facets": [facet for facet in FACETS if facets is None or facet in facets],
        "counts": None, "changes": [],
        "presentation": {"limit": limit, "shown": 0, "omitted": 0, "truncated": False},
        "issues": {"items": [], "omitted": 0, "truncated": False},
    }
    endpoint = "reference"
    try:
        reference = _capture_git_ref_snapshot(repo_root, reference_ref, label="deployment reference")
        result["reference"].update(commit=reference.head_oid, tree=reference.tree_oid)
        path = find_config_file(repo_root, reference_config_hint, snapshot=reference)
        result["reference"]["config"] = path.relative_to(repo_root).as_posix()
        old_config = load_config_file(path, repo_root=repo_root, snapshot=reference)
        old_lock = load_lockfile_file(repo_root / reference_lock_hint, repo_root=repo_root, snapshot=reference)
        for key, field in (("schema", "lock_schema"), ("config_contract", "config_contract")):
            value = old_lock.get(key)
            if isinstance(value, str) and len(value) <= 128:
                result["reference"][field] = value
        if old_lock.get("schema") != LOCKFILE_SCHEMA or old_lock.get("config_contract") != SEMANTIC_CONFIG_VERSION:
            raise ConfigError("Unsupported reference digest contract; inspect the historical upgrade or choose a compatible verified checkpoint. No reference is rewritten.")
        problems = [
            *validate_config(old_config, repo_root, source="head", snapshot=reference,
                             allow_custom_providers=allow_custom_providers, validate_provider_runtime=False),
            *_lockfile_schema_issues(old_lock), *_lockfile_structure_issues(old_lock),
        ]
        if problems:
            _issue(result, ["Reference declarations or lock structure are invalid", *problems])
            return result
        problems = [*_config_lock_consistency_issues(old_config, old_lock), *_generation_errors(old_lock)]
        observations = []
        if not problems:
            problems = verify_lockfile(old_config, old_lock, repo_root, source="head", snapshot=reference,
                                       allow_custom_providers=allow_custom_providers, facets=[], observations=observations)
        if problems or observations:
            _issue(result, ["Reference is not a verified reconciled checkpoint; choose an existing verified checkpoint, do not rewrite this ref.", *problems, *observations])
            return result
        result["reference"]["verified"] = True
        endpoint = "current"
        current = _capture_git_source_snapshot(repo_root, source) if source != "working-tree" else None
        if current is not None:
            result["current"].update(commit=current.head_oid, tree=current.tree_oid)
        path = find_config_file(repo_root, config_hint, snapshot=current)
        result["current"]["config"] = path.relative_to(repo_root).as_posix()
        config = load_config_file(path, repo_root=repo_root, snapshot=current)
        problems = validate_config(config, repo_root, source=source, snapshot=current,
                                   allow_custom_providers=allow_custom_providers, require_slice_facets=True)
        if problems:
            _issue(result, ["Current declarations are invalid", *problems])
            return result
        selected = facets if facets is not None else config.get("defaults", {}).get("verify_facets", list(FACETS))
        result["facets"] = [facet for facet in FACETS if facet in (selected or FACETS)]
        result["current"]["config_digest"] = semantic_config_digest(config)
        if semantic_config_digest(old_config) != result["current"]["config_digest"]:
            raise ConfigError("Semantic config/graph differs from the reference; declaration reconciliation is unsupported in v1. Review the declarations separately; no integrity acknowledgement is applied.")
        new_lock = generate_lockfile(config, repo_root, source=source, snapshot=current, allow_custom_providers=allow_custom_providers)
        counts = {"components": len(config["components"]), "changed": 0, "unchanged": 0, "facets": dict.fromkeys(FACETS, 0)}
        exit_code = 0
        codes = {"exact": 1, "behavior": 3, "boundary": 4, "compat": 5}
        for name in sorted(config["components"]):
            before = old_lock["components"][name]["fingerprints"]
            after = new_lock["components"][name]["fingerprints"]
            changed = [facet for facet in FACETS if before[facet] != after[facet]]
            counts["changed" if changed else "unchanged"] += 1
            for facet in changed:
                counts["facets"][facet] += 1
                if facet in result["facets"]:
                    exit_code = max(exit_code, codes[facet])
            if changed and len(result["changes"]) < limit:
                result["changes"].append({"component": name, "facets": changed, "before": before, "after": after})
        shown = len(result["changes"])
        result.update(complete=True, ok=exit_code == 0, exit_code=exit_code, counts=counts)
        result["presentation"].update(shown=shown, omitted=counts["changed"] - shown, truncated=shown < counts["changed"])
    except (BoundverError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        _issue(result, [f"{endpoint} endpoint: {_bounded_exception_text(exc, max_chars=MAX_DEPLOYMENT_ISSUE_CHARS)}"])
        if endpoint == "reference":
            _issue(result, ["Choose an existing local verified checkpoint with the required objects and paths. Obtain missing history separately; comparison never fetches or rewrites the reference."])
    try:
        # Match the public JSON writer, including indentation and its newline.
        # A compact-only check could accept rows that cannot actually be emitted.
        _bounded_json_dumps(result, indent=2, sort_keys=True, allow_nan=False,
                            max_bytes=max(0, MAX_DEPLOYMENT_RESULT_BYTES - 1))
    except ValueError:
        result.update(complete=False, ok=False, exit_code=2, counts=None, changes=[])
        result["presentation"].update(shown=0, omitted=0, truncated=False)
        _issue(result, ["Result exceeds its output budget; use --summary or a lower --limit."])
    return result
