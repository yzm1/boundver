"""Declarative generated-artifact freshness receipts."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Mapping, Optional, Protocol, Sequence, Set, Tuple

from ._git import GitSourceSnapshot
from ._hashing import (
    HASH_DOMAIN_DERIVATION_INPUTS,
    HASH_DOMAIN_DERIVATION_OUTPUTS,
    source_paths_digest,
)
from ._structured_data import strict_json_loads
from ._utils import (
    BoundedDiagnosticList,
    ConfigError,
    GuardrailError,
    _PathGlobOperation,
    _bounded_diagnostic_text,
    _bounded_json_dumps,
    _is_glob,
    _normalize_declared_path,
)


DERIVATION_EVIDENCE_SCHEMA = "boundver-derivation/v1"
MAX_DERIVATION_EVIDENCE_BYTES = 64 * 1024
MAX_DERIVATION_EVIDENCE_FILES = 50_000


class DerivationSource(Protocol):
    """The source-view operations needed by freshness verification."""

    repo_root: Path
    source: str
    snapshot: Optional[GitSourceSnapshot]

    def list_files(self, prefix: str) -> List[str]: ...

    def read_file_limited(self, repo_rel: str, max_bytes: int) -> bytes: ...

    def read_blob_limited(self, oid: str, max_bytes: int) -> bytes: ...


def _display_name(name: object) -> str:
    return _bounded_diagnostic_text(name)


def _selector_matches(
    path: str,
    selector: str,
    operation: _PathGlobOperation,
) -> bool:
    normalized = _normalize_declared_path(selector)
    if _is_glob(normalized):
        return operation.matches(path, normalized)
    return path == normalized or path.startswith(normalized.rstrip("/") + "/")


def _select_paths(
    all_files: Sequence[str],
    selectors: object,
    *,
    name: str,
    field: str,
    operation: _PathGlobOperation,
    errors: BoundedDiagnosticList,
) -> List[str]:
    if (
        not isinstance(selectors, list)
        or not selectors
        or not all(isinstance(selector, str) for selector in selectors)
    ):
        errors.append(
            f"Derivation '{_display_name(name)}' {field} must be a non-empty "
            "array of strings"
        )
        return []
    selected: Set[str] = set()
    for selector in sorted(selectors):
        try:
            matches = [
                path
                for path in all_files
                if _selector_matches(path, selector, operation)
            ]
        except (GuardrailError, ValueError) as exc:
            errors.append(
                f"Derivation '{_display_name(name)}' {field} selector could not "
                f"be evaluated: {_bounded_diagnostic_text(str(exc))}"
            )
            continue
        if not matches:
            errors.append(
                f"Derivation '{_display_name(name)}' {field} selector matched no "
                f"tracked file in the selected source: "
                f"{_bounded_diagnostic_text(selector)}"
            )
            continue
        selected.update(matches)
    return sorted(selected)


def _configured_boundary_files(
    config: Mapping[str, object],
    all_files: Sequence[str],
    operation: _PathGlobOperation,
) -> Set[str]:
    selected: Set[str] = set()
    components = config.get("components", {})
    if not isinstance(components, dict):
        return selected
    for component in components.values():
        if not isinstance(component, dict):
            continue
        component_path = component.get("path")
        boundary = component.get("boundary")
        if not isinstance(component_path, str) or not isinstance(boundary, dict):
            continue
        selectors = boundary.get("paths", [])
        if not isinstance(selectors, list) or not all(
            isinstance(selector, str) for selector in selectors
        ):
            continue
        try:
            root = _normalize_declared_path(component_path).rstrip("/")
        except ValueError:
            continue
        prefix = root + "/"
        for path in all_files:
            if not path.startswith(prefix):
                continue
            relative = path[len(prefix) :]
            for selector in selectors:
                try:
                    if _selector_matches(relative, selector, operation):
                        selected.add(path)
                        break
                except GuardrailError:
                    raise
                except ValueError:
                    # Static config validation reports malformed declarations.
                    break
    return selected


def _expected_rows(
    config: Mapping[str, object],
    source_view: DerivationSource,
) -> Tuple[Dict[str, Tuple[str, dict]], Set[str], List[str]]:
    derivations = config.get("derivations", {})
    if derivations in (None, {}):
        return {}, set(), []
    errors = BoundedDiagnosticList()
    if not isinstance(derivations, dict):
        return {}, set(), ["Field 'derivations' must be an object"]
    try:
        all_files = sorted(source_view.list_files("."))
    except (GuardrailError, OSError, ValueError) as exc:
        return {}, set(), [
            "Generated-artifact freshness could not list the selected source: "
            f"{_bounded_diagnostic_text(str(exc))}"
        ]
    all_file_set = set(all_files)
    operation = _PathGlobOperation("Generated-artifact freshness")
    try:
        boundary_files = _configured_boundary_files(config, all_files, operation)
    except GuardrailError as exc:
        return {}, all_file_set, [
            "Generated-artifact boundary linkage failed closed: "
            f"{_bounded_diagnostic_text(str(exc))}"
        ]

    rows: Dict[str, Tuple[str, dict]] = {}
    output_owners: Dict[str, str] = {}
    evidence_owners: Dict[str, str] = {}
    names = [name for name in derivations if isinstance(name, str)]
    if len(names) != len(derivations):
        errors.append("Derivation names must be strings")
    for name in sorted(names):
        if errors.truncated:
            break
        definition = derivations[name]
        if not isinstance(definition, dict):
            errors.append(f"Derivation '{_display_name(name)}' must be an object")
            continue
        evidence = definition.get("evidence")
        generator = definition.get("generator")
        if not isinstance(evidence, str):
            errors.append(
                f"Derivation '{_display_name(name)}' evidence must be a "
                "repository-relative file path"
            )
            continue
        try:
            evidence = _normalize_declared_path(evidence)
        except ValueError as exc:
            errors.append(
                f"Derivation '{_display_name(name)}' evidence path is invalid: "
                f"{_bounded_diagnostic_text(str(exc))}"
            )
            continue
        if _is_glob(evidence):
            errors.append(
                f"Derivation '{_display_name(name)}' evidence must be a literal path"
            )
            continue
        if not isinstance(generator, str) or not generator:
            errors.append(
                f"Derivation '{_display_name(name)}' generator must be a "
                "non-empty string"
            )
            continue
        inputs = _select_paths(
            all_files,
            definition.get("inputs"),
            name=name,
            field="inputs",
            operation=operation,
            errors=errors,
        )
        outputs = _select_paths(
            all_files,
            definition.get("outputs"),
            name=name,
            field="outputs",
            operation=operation,
            errors=errors,
        )
        if not inputs or not outputs:
            continue
        overlap = sorted(set(inputs) & set(outputs))
        if overlap:
            errors.append(
                f"Derivation '{_display_name(name)}' inputs and outputs overlap at "
                f"{_bounded_diagnostic_text(overlap[0])}"
            )
            continue
        if evidence in set(inputs) | set(outputs):
            errors.append(
                f"Derivation '{_display_name(name)}' evidence cannot also be an "
                "input or output"
            )
            continue
        uncovered_outputs = sorted(set(outputs) - boundary_files)
        if uncovered_outputs:
            errors.append(
                f"Derivation '{_display_name(name)}' output is not selected by any "
                "component boundary declaration: "
                f"{_bounded_diagnostic_text(uncovered_outputs[0])}"
            )
            continue
        previous_evidence = evidence_owners.get(evidence)
        if previous_evidence is not None:
            errors.append(
                f"Derivations '{_display_name(previous_evidence)}' and "
                f"'{_display_name(name)}' use the same evidence path: "
                f"{_bounded_diagnostic_text(evidence)}"
            )
            continue
        evidence_owners[evidence] = name
        ambiguous_output = next(
            (path for path in outputs if path in output_owners),
            None,
        )
        if ambiguous_output is not None:
            errors.append(
                f"Derivation output is claimed by both "
                f"'{_display_name(output_owners[ambiguous_output])}' and "
                f"'{_display_name(name)}': "
                f"{_bounded_diagnostic_text(ambiguous_output)}"
            )
            continue
        for output in outputs:
            output_owners[output] = name
        try:
            input_digest = source_paths_digest(
                source_view.repo_root,
                inputs,
                source=source_view.source,
                domain=HASH_DOMAIN_DERIVATION_INPUTS,
                snapshot=source_view.snapshot,
                read_blob_fn=source_view.read_blob_limited,
            )
            output_digest = source_paths_digest(
                source_view.repo_root,
                outputs,
                source=source_view.source,
                domain=HASH_DOMAIN_DERIVATION_OUTPUTS,
                snapshot=source_view.snapshot,
                read_blob_fn=source_view.read_blob_limited,
            )
        except (GuardrailError, OSError, ValueError) as exc:
            errors.append(
                f"Derivation '{_display_name(name)}' source hashing failed: "
                f"{_bounded_diagnostic_text(str(exc))}"
            )
            continue
        rows[name] = (
            evidence,
            {
                "schema": DERIVATION_EVIDENCE_SCHEMA,
                "derivation": name,
                "generator": generator,
                "inputs": {"files": len(inputs), "digest": input_digest},
                "outputs": {"files": len(outputs), "digest": output_digest},
            },
        )
    return rows, all_file_set, list(errors)


def _evidence_shape_issues(value: object) -> List[str]:
    issues: List[str] = []
    if not isinstance(value, dict):
        return ["root must be an object"]
    expected_fields = {"schema", "derivation", "generator", "inputs", "outputs"}
    if set(value) != expected_fields:
        issues.append("root fields do not match the derivation evidence contract")
    for field in ("schema", "derivation", "generator"):
        if not isinstance(value.get(field), str) or not value.get(field):
            issues.append(f"{field} must be a non-empty string")
    for field in ("inputs", "outputs"):
        item = value.get(field)
        if not isinstance(item, dict) or set(item) != {"files", "digest"}:
            issues.append(f"{field} must contain exactly files and digest")
            continue
        files = item.get("files")
        if (
            isinstance(files, bool)
            or not isinstance(files, int)
            or files < 1
            or files > MAX_DERIVATION_EVIDENCE_FILES
        ):
            issues.append(f"{field}.files must be a bounded positive integer")
        digest = item.get("digest")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            issues.append(f"{field}.digest must be a lowercase SHA-256 digest")
    return issues


def parse_derivation_evidence(data: bytes, path: str) -> dict:
    """Parse one bounded UTF-8 freshness receipt without accepting duplicates."""
    if len(data) > MAX_DERIVATION_EVIDENCE_BYTES:
        raise ConfigError(
            f"Derivation evidence exceeds the {MAX_DERIVATION_EVIDENCE_BYTES}-byte "
            f"limit: {_bounded_diagnostic_text(path)}"
        )
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"Derivation evidence is not valid UTF-8: "
            f"{_bounded_diagnostic_text(path)}"
        ) from exc
    try:
        value = strict_json_loads(text)
    except (ValueError, RecursionError, OverflowError) as exc:
        raise ConfigError(
            f"Derivation evidence is not valid strict JSON: "
            f"{_bounded_diagnostic_text(path)}"
        ) from exc
    shape_issues = _evidence_shape_issues(value)
    if shape_issues:
        raise ConfigError(
            f"Derivation evidence is malformed at {_bounded_diagnostic_text(path)}: "
            + "; ".join(shape_issues[:8])
        )
    return value


def verify_derivations(
    config: Mapping[str, object],
    source_view: DerivationSource,
) -> List[str]:
    """Return fail-closed freshness issues for every configured derivation."""
    rows, selected_files, preparation_issues = _expected_rows(config, source_view)
    issues = BoundedDiagnosticList(preparation_issues)
    if issues:
        return list(issues)
    for name in sorted(rows):
        evidence_path, expected = rows[name]
        if evidence_path not in selected_files:
            issues.append(
                f"Derivation '{_display_name(name)}' evidence is missing from the "
                f"selected {source_view.source} source: "
                f"{_bounded_diagnostic_text(evidence_path)}; run the trusted "
                "generator, record the derivation, and include the receipt"
            )
            continue
        try:
            raw = source_view.read_file_limited(
                evidence_path,
                MAX_DERIVATION_EVIDENCE_BYTES,
            )
            if getattr(raw, "git_mode", None) not in {"100644", "100755"}:
                raise ConfigError("evidence must be a regular Git file")
            actual = parse_derivation_evidence(raw, evidence_path)
        except (ConfigError, GuardrailError, OSError, ValueError) as exc:
            issues.append(
                f"Derivation '{_display_name(name)}' evidence cannot be used: "
                f"{_bounded_diagnostic_text(str(exc))}"
            )
            continue
        if actual["schema"] != expected["schema"]:
            issues.append(
                f"Derivation '{_display_name(name)}' evidence schema is unsupported; "
                "record it again with this Boundver version"
            )
        if actual["derivation"] != expected["derivation"]:
            issues.append(
                f"Derivation '{_display_name(name)}' evidence names a different "
                "derivation; record it again"
            )
        if actual["generator"] != expected["generator"]:
            issues.append(
                f"Derivation '{_display_name(name)}' generator identity changed; "
                "run the trusted generator and record fresh evidence"
            )
        if actual["inputs"] != expected["inputs"]:
            issues.append(
                f"Derivation '{_display_name(name)}' inputs are stale for the "
                f"selected {source_view.source} source; run the trusted generator "
                "and record fresh evidence"
            )
        if actual["outputs"] != expected["outputs"]:
            issues.append(
                f"Derivation '{_display_name(name)}' outputs changed after evidence "
                "was recorded; run the trusted generator and record fresh evidence"
            )
        if issues.truncated:
            break
    return list(issues)


def build_derivation_evidence(
    config: Mapping[str, object],
    name: str,
    source_view: DerivationSource,
) -> Tuple[str, dict]:
    """Build one receipt after an explicitly trusted generator has run."""
    rows, _selected_files, issues = _expected_rows(config, source_view)
    if issues:
        raise ConfigError("Cannot record derivation evidence:\n" + "\n".join(issues))
    if name not in rows:
        available = ", ".join(sorted(rows)) or "none"
        raise ConfigError(
            f"Unknown derivation '{_display_name(name)}'; configured: "
            f"{_bounded_diagnostic_text(available)}"
        )
    return rows[name]


def dump_derivation_evidence(value: dict) -> str:
    """Serialize one receipt within the read-side storage bound."""
    try:
        body = _bounded_json_dumps(
            value,
            indent=2,
            max_bytes=MAX_DERIVATION_EVIDENCE_BYTES - 1,
        )
    except GuardrailError as exc:
        raise ConfigError("Derivation evidence exceeds its storage limit") from exc
    return body + "\n"
