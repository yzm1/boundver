#!/usr/bin/env python3
"""Export bounded composite-Action outputs from a boundver JSON result."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence, TextIO


# GitHub limits all job outputs to 1 MB, approximated as UTF-16. Keep each
# potentially user-sized boundver value at 64 KiB so this Action consumes less
# than one quarter of that budget and leaves room for the caller's own outputs.
MAX_VALUE_UTF16_BYTES = 64 * 1024
MAX_RESULT_BYTES = 64 * 1024 * 1024
TRUNCATION_MARKER = (
    "[boundver Action output truncated; inspect result-file or the step log]"
)
UNAVAILABLE_MARKER = (
    "[boundver Action result unavailable; inspect result-file and the step log]"
)
SIZED_OUTPUTS = ("issues", "observations", "consumer-impact")
PLAN_ARRAY_OUTPUTS = (
    "changed-components",
    "impacted-components",
    "external-consumers",
    "test-components",
    "changed-slices",
    "impacted-slices",
    "test-slices",
)
PLAN_SCHEMA = "boundver-plan/v1"
MAX_SOURCE_ANNOTATIONS = 10
SOURCE_COMPLETE = "complete"
SOURCE_OVERSIZED = "oversized"


def _utf16_size(value: str) -> int:
    return len(value.encode("utf-16-le", errors="surrogatepass"))


def _transport_text(value: object) -> str:
    rendered: list[str] = []
    for character in str(value):
        codepoint = ord(character)
        if character == "\n":
            rendered.append("\\n")
        elif character == "\r":
            rendered.append("\\r")
        elif character == "\t":
            rendered.append("\\t")
        elif character == "\b":
            rendered.append("\\b")
        elif character == "\f":
            rendered.append("\\f")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            rendered.append(f"\\x{codepoint:02x}")
        elif codepoint in {0x2028, 0x2029}:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(character)
    text = "".join(rendered)
    if text.startswith("::"):
        text = "\\x3a" + text[1:]
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _bounded_lines(value: object, limit: int) -> tuple[str, bool]:
    items = value if isinstance(value, list) else []
    lines = [_transport_text(item) for item in items]
    complete = "\n".join(lines)
    if _utf16_size(complete) <= limit:
        return complete, False

    kept: list[str] = []
    for line in lines:
        candidate = "\n".join((*kept, line, TRUNCATION_MARKER))
        if _utf16_size(candidate) > limit:
            break
        kept.append(line)
    return "\n".join((*kept, TRUNCATION_MARKER)), True


def _bounded_consumer_impact(value: object, limit: int) -> tuple[str, bool]:
    rows = value if isinstance(value, list) else []
    encoded = json.dumps(rows, separators=(",", ":"))
    if _utf16_size(encoded) <= limit:
        return encoded, False
    # A partial downstream closure is unsafe for CI fan-out. Return a valid
    # empty array and require callers to inspect ``truncated-outputs`` before
    # routing from this output; the full result remains in ``result-file``.
    return "[]", True


def _bounded_json_array(value: object, limit: int) -> tuple[str, bool]:
    rows = value if isinstance(value, list) else []
    encoded = json.dumps(rows, separators=(",", ":"))
    if _utf16_size(encoded) <= limit:
        return encoded, False
    return "[]", True


def _load_payload(path: Path, max_result_bytes: int) -> tuple[Mapping[str, object], str]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_result_bytes + 1)
    except OSError:
        return {}, "unreadable"
    if len(raw) > max_result_bytes:
        return {}, SOURCE_OVERSIZED
    if not raw:
        return {}, "empty"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return {}, "invalid-utf8"
    except json.JSONDecodeError:
        return {}, "invalid-json"
    if not isinstance(payload, dict):
        return {}, "invalid-shape"
    return payload, SOURCE_COMPLETE


def _fallback_payload(source_status: str) -> dict[str, object]:
    return {
        "ok": False,
        "issues": [UNAVAILABLE_MARKER],
        "observations": [],
        "consumer_impact": [],
        "action_transport": {
            "complete": False,
            "reason": source_status,
        },
    }


def _write_fallback(path: Path, payload: Mapping[str, object]) -> bool:
    try:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except OSError:
        return False
    return True


def _delimiter(name: str, value: str) -> str:
    digest = hashlib.sha256(f"{name}\0{value}".encode("utf-8")).hexdigest()
    candidate = f"BOUNDVER_OUTPUT_{digest}"
    occupied = set(value.splitlines())
    while candidate in occupied:
        candidate += "X"
    return candidate


def _append_output(handle: TextIO, name: str, value: str) -> None:
    if "\n" not in value and "\r" not in value:
        handle.write(f"{name}={value}\n")
        return
    delimiter = _delimiter(name, value)
    handle.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def _workflow_safe_text(value: object) -> str:
    text = str(value).encode("utf-8", errors="backslashreplace").decode("utf-8")
    rendered = []
    for character in text:
        codepoint = ord(character)
        if character in {"\r", "\n"}:
            rendered.append(character)
        elif character == "\t":
            rendered.append("\\t")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            rendered.append(f"\\x{codepoint:02x}")
        elif codepoint in {0x2028, 0x2029}:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(character)
    return "".join(rendered)


def _workflow_property(value: object) -> str:
    return (
        _workflow_safe_text(value)
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def _workflow_message(value: object) -> str:
    return (
        _workflow_safe_text(value)
        .replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def _emit_source_annotations(
    payload: Mapping[str, object],
    annotation_commit: Optional[str],
    *,
    max_annotations: int = MAX_SOURCE_ANNOTATIONS,
) -> bool:
    if not annotation_commit:
        return False
    endpoints = payload.get("endpoints", {})
    if (
        not isinstance(endpoints, dict)
        or not isinstance(endpoints.get("target"), dict)
        or endpoints["target"].get("commit") != annotation_commit
    ):
        return False
    locations = payload.get("source_locations", [])
    if not isinstance(locations, list):
        return False
    target_locations = [
        item
        for item in locations
        if isinstance(item, dict)
        and item.get("endpoint") == "target"
        and isinstance(item.get("path"), str)
        and isinstance(item.get("component"), str)
        and item.get("commit") == annotation_commit
    ]
    for item in target_locations[:max_annotations]:
        message = (
            f"{item['component']} has a structural boundary change at target "
            f"{annotation_commit}; inspect the complete review-plan artifact."
        )
        print(
            "::notice file="
            + _workflow_property(item["path"])
            + ",title=Boundver structural change::"
            + _workflow_message(message),
            file=sys.stderr,
        )
    return len(target_locations) > max_annotations


def export_outputs(
    result_path: Path,
    github_output: Path,
    *,
    operation: str = "verify",
    summary_path: Optional[Path] = None,
    annotation_commit: Optional[str] = None,
    max_value_utf16_bytes: int = MAX_VALUE_UTF16_BYTES,
    max_result_bytes: int = MAX_RESULT_BYTES,
) -> tuple[str, ...]:
    if max_value_utf16_bytes < _utf16_size(TRUNCATION_MARKER):
        raise ValueError("Action output limit cannot hold the truncation marker")
    if max_result_bytes <= 0:
        raise ValueError("result size limit must be positive")
    if operation not in {"verify", "review"}:
        raise ValueError("operation must be verify or review")

    payload, source_status = _load_payload(result_path, max_result_bytes)
    if operation == "review" and source_status == SOURCE_COMPLETE:
        if (
            payload.get("schema") != PLAN_SCHEMA
            or payload.get("complete") is not True
            or not isinstance(payload.get("selection"), dict)
        ):
            payload = {}
            source_status = "invalid-plan"
    source_incomplete = source_status != SOURCE_COMPLETE
    fallback_written = False
    if source_incomplete and source_status != SOURCE_OVERSIZED:
        payload = _fallback_payload(source_status)
        fallback_written = _write_fallback(result_path, payload)
    issues, issues_truncated = _bounded_lines(
        payload.get("issues", []), max_value_utf16_bytes
    )
    observations, observations_truncated = _bounded_lines(
        payload.get("observations", []), max_value_utf16_bytes
    )
    consumer_impact, impact_truncated = _bounded_consumer_impact(
        payload.get("consumer_impact", []), max_value_utf16_bytes
    )
    selection = payload.get("selection", {})
    if not isinstance(selection, dict):
        selection = {}
    plan_values = {}
    plan_truncated = {}
    for output_name in PLAN_ARRAY_OUTPUTS:
        key = output_name.replace("-", "_")
        encoded, bounded = _bounded_json_array(
            selection.get(key, []), max_value_utf16_bytes
        )
        plan_values[output_name] = encoded
        plan_truncated[output_name] = bounded
    if source_incomplete:
        marker = (
            TRUNCATION_MARKER
            if source_status == SOURCE_OVERSIZED
            else UNAVAILABLE_MARKER
        )
        issues = observations = marker
        consumer_impact = "[]"
        issues_truncated = observations_truncated = impact_truncated = True
        if operation == "review":
            plan_values = {name: "[]" for name in PLAN_ARRAY_OUTPUTS}
            plan_truncated = {name: True for name in PLAN_ARRAY_OUTPUTS}

    truncated_values = [
        name
        for name, state in zip(
            SIZED_OUTPUTS,
            (issues_truncated, observations_truncated, impact_truncated),
        )
        if state
    ]
    if operation == "review":
        truncated_values.extend(
            name for name in PLAN_ARRAY_OUTPUTS if plan_truncated[name]
        )
    annotations_truncated = False
    if not source_incomplete and operation == "review":
        annotations_truncated = _emit_source_annotations(
            payload,
            annotation_commit,
        )
        if annotations_truncated:
            truncated_values.append("source-annotations")
            print(
                "::warning title=Boundver source annotations bounded::"
                f"Only the first {MAX_SOURCE_ANNOTATIONS} exact target files "
                "were annotated; inspect result-file for every source location.",
                file=sys.stderr,
            )
    truncated = tuple(truncated_values)
    values = {
        "issues": issues,
        "observations": observations,
        "consumer-impact": consumer_impact,
        **plan_values,
        "truncated-outputs": json.dumps(truncated, separators=(",", ":")),
        "result-schema": (
            PLAN_SCHEMA
            if not source_incomplete and payload.get("schema") == PLAN_SCHEMA
            else ""
        ),
        "transport-complete": "false" if source_incomplete else "true",
        "selection-complete": (
            "true"
            if operation == "review"
            and not source_incomplete
            and not any(plan_truncated.values())
            else "false"
        ),
        # This is a machine-routing value, not terminal text. GitHub's
        # delimiter form safely carries newlines, so preserve the exact path
        # even on POSIX self-hosted runners whose temporary directory contains
        # otherwise terminal-sensitive characters.
        "result-file": str(result_path.resolve()),
        "summary-file": (
            str(summary_path.resolve())
            if operation == "review" and summary_path
            else ""
        ),
    }
    with github_output.open("a", encoding="utf-8", newline="\n") as handle:
        for name, value in values.items():
            _append_output(handle, name, value)

    if source_incomplete:
        if source_status == SOURCE_OVERSIZED:
            fallback_detail = "the full oversized JSON remains at result-file"
        elif fallback_written:
            fallback_detail = "a valid diagnostic JSON was written to result-file"
        else:
            fallback_detail = "result-file could not be replaced with diagnostic JSON"
        print(
            "::warning title=Boundver Action result incomplete::"
            f"boundver JSON result is {source_status}; all repository-sized "
            f"outputs are marked incomplete and {fallback_detail}.",
            file=sys.stderr,
        )
    elif truncated:
        names = ", ".join(truncated)
        print(
            "::warning title=Boundver Action outputs bounded::"
            f"{names} exceeded the safe output budget; inspect result-file "
            "or the step log for the full JSON result.",
            file=sys.stderr,
        )
    return truncated


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export bounded boundver outputs for a composite Action."
    )
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--github-output", required=True, type=Path)
    parser.add_argument("--operation", choices=["verify", "review"], default="verify")
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--annotation-commit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    export_outputs(
        arguments.result,
        arguments.github_output,
        operation=arguments.operation,
        summary_path=arguments.summary,
        annotation_commit=arguments.annotation_commit,
    )
    if arguments.operation == "review":
        payload, status = _load_payload(arguments.result, MAX_RESULT_BYTES)
        if (
            status != SOURCE_COMPLETE
            or payload.get("schema") != PLAN_SCHEMA
            or payload.get("complete") is not True
        ):
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
