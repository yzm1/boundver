"""CI routing plans and bounded summaries derived from one range review."""

from __future__ import annotations

import html
from pathlib import PurePosixPath
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from ._review import MAX_REVIEW_RESULT_BYTES, REVIEW_SCHEMA
from ._utils import ConfigError, GuardrailError, _bounded_json_dumps


PLAN_SCHEMA = "boundver-plan/v1"
PLAN_CLAIM = "routing-evidence-only"
MAX_PLAN_RESULT_BYTES = MAX_REVIEW_RESULT_BYTES
MAX_PLAN_SUMMARY_BYTES = 64 * 1024
MAX_PLAN_SUMMARY_ROWS = 50
MAX_PLAN_SUMMARY_FIELD_BYTES = 512


def _sorted_names(rows: Iterable[Mapping[str, object]]) -> List[str]:
    return sorted(
        {
            value
            for row in rows
            for value in (row.get("name"),)
            if isinstance(value, str)
        }
    )


def _impact_names(impacts: Sequence[Mapping[str, object]]) -> Tuple[List[str], List[str]]:
    components: Set[str] = set()
    external: Set[str] = set()
    for impact in impacts:
        for item in impact.get("components", []):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                components.add(item["name"])
        for item in impact.get("external_consumers", []):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                external.add(item["name"])
    return sorted(components), sorted(external)


def _source_path(component_path: object, document_label: object) -> Optional[str]:
    if not isinstance(component_path, str) or not isinstance(document_label, str):
        return None
    prefix = "canonical:"
    if not document_label.startswith(prefix):
        return None
    relative = PurePosixPath(document_label[len(prefix) :])
    root = PurePosixPath(component_path)
    if (
        relative.is_absolute()
        or root.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in (*root.parts, *relative.parts))
    ):
        return None
    return (root / relative).as_posix()


def _source_locations(structural: Mapping[str, object]) -> List[dict]:
    locations: Dict[Tuple[str, str, str, str], dict] = {}
    reports = structural.get("reports", [])
    if not isinstance(reports, list):
        return []
    for report in reports:
        if not isinstance(report, dict) or report.get("complete") is not True:
            continue
        component = report.get("component")
        inputs = report.get("inputs", {})
        documents = report.get("documents", [])
        if not isinstance(component, str) or not isinstance(inputs, dict):
            continue
        if not isinstance(documents, list):
            continue
        for document in documents:
            if not isinstance(document, dict):
                continue
            endpoint_name = "base" if document.get("status") == "removed" else "target"
            endpoint = inputs.get(endpoint_name, {})
            if not isinstance(endpoint, dict) or endpoint.get("present") is not True:
                continue
            path = _source_path(endpoint.get("component_path"), document.get("label"))
            commit = endpoint.get("commit")
            if path is None or not isinstance(commit, str):
                continue
            key = (component, path, endpoint_name, commit)
            locations[key] = {
                "component": component,
                "path": path,
                "endpoint": endpoint_name,
                "commit": commit,
                "kind": "structural-document",
            }
    return [locations[key] for key in sorted(locations)]


def build_review_plan(review: Mapping[str, object]) -> dict:
    """Project a complete review into a stable downstream-test routing contract."""
    if review.get("schema") != REVIEW_SCHEMA or review.get("complete") is not True:
        raise ConfigError(
            "A CI review plan requires one complete boundver-review/v1 result"
        )
    components = review.get("components", {})
    slices = review.get("slices", {})
    impacts = review.get("consumer_impact", [])
    slice_impacts = review.get("slice_impact", [])
    structural = review.get("structural_changes", {})
    if not all(
        (
            isinstance(components, dict),
            isinstance(slices, dict),
            isinstance(impacts, list),
            isinstance(slice_impacts, list),
            isinstance(structural, dict),
        )
    ):
        raise ConfigError("The complete review result has an invalid plan shape")

    changed_rows = []
    for component in components.get("changed", []):
        if not isinstance(component, dict):
            raise ConfigError("The complete review contains an invalid component row")
        facet_rows = []
        for transition in component.get("facets", []):
            if not isinstance(transition, dict):
                raise ConfigError("The complete review contains an invalid facet row")
            facet_rows.append(
                {
                    "facet": transition.get("facet"),
                    "selected": transition.get("selected"),
                }
            )
        metadata = component.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ConfigError("The complete review contains invalid component metadata")
        changed_rows.append(
            {
                "name": component.get("name"),
                "status": component.get("status"),
                "facets": facet_rows,
                "metadata": sorted(metadata),
            }
        )

    changed_slice_rows = []
    for item in slices.get("changed", []):
        if not isinstance(item, dict):
            raise ConfigError("The complete review contains an invalid slice row")
        changed_slice_rows.append(
            {"name": item.get("name"), "status": item.get("status")}
        )

    changed_components = _sorted_names(changed_rows)
    impacted_components, external_consumers = _impact_names(impacts)
    changed_slices = _sorted_names(changed_slice_rows)
    impacted_slices = _sorted_names(slice_impacts)
    test_components = sorted(set(changed_components) | set(impacted_components))
    test_slices = sorted(set(changed_slices) | set(impacted_slices))
    locations = _source_locations(structural)

    plan = {
        "schema": PLAN_SCHEMA,
        "complete": True,
        "claim": PLAN_CLAIM,
        "request": review.get("request"),
        "history": review.get("history"),
        "endpoints": review.get("endpoints"),
        "policy": review.get("policy"),
        "changed_components": changed_rows,
        "consumer_impact": impacts,
        "changed_slices": changed_slice_rows,
        "slice_impact": slice_impacts,
        "structural_changes": structural,
        "selection": {
            "changed_components": changed_components,
            "impacted_components": impacted_components,
            "external_consumers": external_consumers,
            "test_components": test_components,
            "changed_slices": changed_slices,
            "impacted_slices": impacted_slices,
            "test_slices": test_slices,
        },
        "source_locations": locations,
        "summary": {
            "changed_components": len(changed_components),
            "impacted_components": len(impacted_components),
            "external_consumers": len(external_consumers),
            "test_components": len(test_components),
            "changed_slices": len(changed_slices),
            "impacted_slices": len(impacted_slices),
            "test_slices": len(test_slices),
            "structural_reports": len(structural.get("reports", [])),
            "structural_complete": structural.get("complete") is True,
        },
    }
    try:
        _bounded_json_dumps(
            plan,
            sort_keys=True,
            max_bytes=MAX_PLAN_RESULT_BYTES,
        )
    except GuardrailError as exc:
        raise GuardrailError(
            "CI review plan exceeds the "
            f"{MAX_PLAN_RESULT_BYTES}-byte complete JSON limit. Reduce graph "
            "fan-out or use direct impact instead of --transitive. No partial "
            "plan was emitted."
        ) from exc
    return plan


def _display_text(value: object) -> str:
    rendered: List[str] = []
    for character in str(value):
        codepoint = ord(character)
        if character == "\n":
            rendered.append("\\n")
        elif character == "\r":
            rendered.append("\\r")
        elif character == "\t":
            rendered.append("\\t")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            rendered.append(f"\\x{codepoint:02x}")
        elif codepoint in {0x2028, 0x2029}:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(character)
    return "".join(rendered)


def _bounded_text(value: object, max_bytes: int) -> Tuple[str, bool]:
    text = _display_text(value)
    encoded = text.encode("utf-8", errors="backslashreplace")
    if len(encoded) <= max_bytes:
        return text, False
    marker = "..."
    budget = max_bytes - len(marker)
    kept: List[str] = []
    used = 0
    for character in text:
        size = len(character.encode("utf-8", errors="backslashreplace"))
        if used + size > budget:
            break
        kept.append(character)
        used += size
    return "".join(kept) + marker, True


def _code(value: object) -> Tuple[str, bool]:
    text, truncated = _bounded_text(value, MAX_PLAN_SUMMARY_FIELD_BYTES)
    return f"<code>{html.escape(text, quote=True)}</code>", truncated


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, list) else ()


def _summary_sections(
    plan: Mapping[str, object],
) -> List[Tuple[str, str, Sequence[object]]]:
    selection = plan.get("selection", {})
    if not isinstance(selection, dict):
        selection = {}
    return [
        (
            "### Changed components",
            "component",
            _sequence(plan.get("changed_components", [])),
        ),
        (
            "### Impacted components",
            "name",
            _sequence(selection.get("impacted_components", [])),
        ),
        (
            "### External consumers",
            "name",
            _sequence(selection.get("external_consumers", [])),
        ),
        (
            "### Changed slices",
            "name",
            _sequence(selection.get("changed_slices", [])),
        ),
        (
            "### Impacted slices",
            "name",
            _sequence(selection.get("impacted_slices", [])),
        ),
        (
            "### Exact structural source files",
            "source",
            _sequence(plan.get("source_locations", [])),
        ),
    ]


def _render_summary_row(kind: str, value: object) -> Tuple[str, bool]:
    if kind == "component" and isinstance(value, dict):
        name, bounded = _code(value.get("name"))
        facets = [
            item.get("facet")
            for item in _sequence(value.get("facets", []))
            if isinstance(item, dict) and isinstance(item.get("facet"), str)
        ]
        detail = ", ".join(facets) if facets else "metadata only"
        return f"- {name}: {html.escape(detail)}", bounded
    if kind == "source" and isinstance(value, dict):
        path, path_bounded = _code(value.get("path"))
        component, component_bounded = _code(value.get("component"))
        return f"- {path} ({component})", path_bounded or component_bounded
    rendered, bounded = _code(value)
    return f"- {rendered}", bounded


def render_review_plan_markdown(
    plan: Mapping[str, object],
    *,
    max_rows: int = MAX_PLAN_SUMMARY_ROWS,
    max_bytes: int = MAX_PLAN_SUMMARY_BYTES,
) -> str:
    """Render a bounded CI summary without weakening the complete plan artifact."""
    if plan.get("schema") != PLAN_SCHEMA or plan.get("complete") is not True:
        raise ConfigError("A CI summary requires one complete boundver-plan/v1 result")
    if max_rows < 0:
        raise ValueError("summary row limit must not be negative")
    if max_bytes < 2048:
        raise ValueError("summary byte limit must be at least 2048")
    endpoints = plan.get("endpoints", {})
    request = plan.get("request", {})
    policy = plan.get("policy", {})
    structural = plan.get("structural_changes", {})
    base = endpoints.get("base", {})
    target = endpoints.get("target", {})
    base_commit, base_bounded = _code(base.get("commit"))
    target_commit, target_bounded = _code(target.get("commit"))
    sections = _summary_sections(plan)
    field_truncated = base_bounded or target_bounded
    total_rows = sum(len(rows) for _title, _kind, rows in sections)

    lines = [
        "## Boundver range review plan",
        "",
        "**Routing evidence only; not a backward-compatibility verdict.**",
        "",
        f"- Plan contract: `{PLAN_SCHEMA}` (complete)",
        f"- Effective base: {base_commit}",
        f"- Target: {target_commit}",
        f"- Merge-base policy: {'enabled' if request.get('merge_base') else 'disabled'}",
        f"- Consumer impact: {html.escape(str(policy.get('impact', 'unknown')))}",
        (
            "- Structural evidence: complete"
            if structural.get("complete") is True
            else "- Structural evidence: incomplete; routing evidence remains complete"
        ),
    ]
    shown_rows = 0
    byte_truncated = False
    marker_reserve = 512
    stop = False
    for title, kind, rows in sections:
        if not rows:
            continue
        rendered_section: List[str] = ["", title, ""]
        section_rows = []
        for value in rows:
            if shown_rows >= max_rows:
                byte_truncated = True
                stop = True
                break
            row, bounded = _render_summary_row(kind, value)
            field_truncated = field_truncated or bounded
            candidate = "\n".join((*lines, *rendered_section, *section_rows, row))
            if len(candidate.encode("utf-8")) + marker_reserve > max_bytes:
                byte_truncated = True
                stop = True
                break
            section_rows.append(row)
            shown_rows += 1
        if section_rows:
            lines.extend((*rendered_section, *section_rows))
        if stop:
            byte_truncated = shown_rows < total_rows or field_truncated
            break

    presentation_truncated = (
        field_truncated or byte_truncated or shown_rows < total_rows
    )
    lines.extend(("", "---", ""))
    if presentation_truncated:
        details = (
            f"Presentation truncated: {shown_rows} of {total_rows} routing rows "
            "shown"
        )
        if field_truncated:
            details += "; one or more displayed fields were bounded"
        lines.append(
            details
            + ". The complete boundver-plan/v1 result remains in the result-file artifact."
        )
    else:
        lines.append(
            f"Presentation complete: all {total_rows} routing rows shown. "
            "The boundver-plan/v1 result is the machine-authoritative artifact."
        )
    rendered = "\n".join(lines) + "\n"
    if len(rendered.encode("utf-8")) > max_bytes:
        raise GuardrailError(
            "CI review summary cannot fit its bounded presentation contract"
        )
    return rendered
