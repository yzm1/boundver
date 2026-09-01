"""Typed, bounded, format-neutral structural-provider diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
import tracemalloc

import pytest

from boundver._provider_diff import (
    StructuralDiffBudget,
    _diff_json,
    _validate_tree,
    structural_diff_payload,
)
from boundver._utils import GuardrailError
from boundver.providers import (
    STRUCTURAL_DIFF_INTERFACE,
    OpenApiCanonicalProvider,
    ProviderContext,
    StructuralChange,
    StructuralDiffBudget as ExportedStructuralDiffBudget,
    StructuralDiffProvider,
    StructuralDiffResult,
    StructuralDocumentDiff,
)


def _context(raw: bytes) -> ProviderContext:
    path = "svc/api.openapi"
    files = {path: raw}

    def read_file(repo_relative: str) -> bytes:
        return files[repo_relative]

    def read_file_limited(repo_relative: str, max_bytes: int) -> bytes:
        value = files[repo_relative]
        if len(value) > max_bytes:
            raise GuardrailError("fixture exceeds requested read limit")
        return value

    def list_files(prefix: str) -> list[str]:
        return sorted(name for name in files if name.startswith(prefix))

    return ProviderContext(
        repo_root=Path("."),
        component_path="svc",
        boundary_cfg={"paths": ["api.openapi"]},
        source="head",
        read_file=read_file,
        read_file_limited=read_file_limited,
        list_files=list_files,
    )


def _before() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "ignored", "version": "1"},
        "paths": {
            "/users": {
                "get": {
                    "parameters": [
                        {"name": "limit", "in": "query", "required": False}
                    ],
                    "responses": {"200": {}, "404": {}},
                }
            }
        },
    }


def _after() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "also ignored", "version": "2"},
        "paths": {
            "/health": {"get": {"responses": {"204": {}}}},
            "/users": {
                "get": {
                    "parameters": [
                        {"name": "limit", "in": "query", "required": True}
                    ],
                    "responses": {"200": {}},
                }
            },
        },
    }


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True).encode("utf-8")


def _yaml_bytes(value: dict) -> bytes:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_dump(value, sort_keys=False).encode("utf-8")


def test_structural_provider_interface_is_versioned_and_public() -> None:
    provider = OpenApiCanonicalProvider()

    assert STRUCTURAL_DIFF_INTERFACE == "boundver-structural-diff/v1"
    assert provider.structural_diff_interface == STRUCTURAL_DIFF_INTERFACE
    assert isinstance(provider, StructuralDiffProvider)
    assert ExportedStructuralDiffBudget is StructuralDiffBudget
    change = StructuralChange("changed", "/paths", "object", "object")
    document = StructuralDocumentDiff(
        "canonical:api.json",
        "changed",
        (change,),
    )
    assert StructuralDiffResult((document,)).documents[0].label == "canonical:api.json"


def test_openapi_structural_diff_is_json_yaml_format_neutral() -> None:
    provider = OpenApiCanonicalProvider()
    before = _context(_json_bytes(_before()))

    json_result = provider.structural_diff(
        before,
        _context(_json_bytes(_after())),
        StructuralDiffBudget(),
    )
    yaml_result = provider.structural_diff(
        before,
        _context(_yaml_bytes(_after())),
        StructuralDiffBudget(),
    )

    assert json_result == yaml_result
    payload = structural_diff_payload(json_result)
    assert payload["documents"][0]["changes"] == [
        {
            "kind": "added",
            "path": "/paths/~1health",
            "before_type": None,
            "after_type": "object",
        },
        {
            "kind": "changed",
            "path": "/paths/~1users/get/parameters/0/required",
            "before_type": "boolean",
            "after_type": "boolean",
        },
        {
            "kind": "removed",
            "path": "/paths/~1users/get/responses/404",
            "before_type": "object",
            "after_type": None,
        },
    ]


def test_equivalent_json_and_yaml_have_no_structural_changes() -> None:
    provider = OpenApiCanonicalProvider()

    result = provider.structural_diff(
        _context(_json_bytes(_before())),
        _context(_yaml_bytes(_before())),
        StructuralDiffBudget(),
    )

    assert structural_diff_payload(result) == {
        "documents": [],
        "summary": {"added": 0, "removed": 0, "changed": 0},
    }


def test_structural_diff_does_not_copy_changed_scalar_values() -> None:
    provider = OpenApiCanonicalProvider()
    before = {"openapi": "3.1.0", "paths": {}, "x-secret": "alpha-token"}
    after = {"openapi": "3.1.0", "paths": {}, "x-secret": "beta-token"}

    result = provider.structural_diff(
        _context(_json_bytes(before)),
        _context(_json_bytes(after)),
        StructuralDiffBudget(),
    )
    payload = structural_diff_payload(result)

    assert payload["documents"][0]["changes"] == [
        {
            "kind": "changed",
            "path": "/x-secret",
            "before_type": "string",
            "after_type": "string",
        }
    ]
    rendered = json.dumps(payload)
    assert "alpha-token" not in rendered
    assert "beta-token" not in rendered


def test_structural_diff_row_limit_returns_no_partial_result() -> None:
    provider = OpenApiCanonicalProvider()

    with pytest.raises(
        GuardrailError,
        match="No partial structural result was emitted",
    ):
        provider.structural_diff(
            _context(_json_bytes(_before())),
            _context(_json_bytes(_after())),
            StructuralDiffBudget(max_rows=1),
        )


def test_structural_diff_bounds_aggregate_input() -> None:
    provider = OpenApiCanonicalProvider()

    with pytest.raises(GuardrailError, match="aggregate input limit"):
        provider.structural_diff(
            _context(_json_bytes(_before())),
            _context(_json_bytes(_after())),
            StructuralDiffBudget(max_input_bytes=1),
        )


def test_structural_diff_rejects_source_before_parser_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenApiCanonicalProvider()

    def parser_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("parser must not run after the source cap is exceeded")

    monkeypatch.setattr(
        "boundver.providers._parse_yaml_or_json",
        parser_must_not_run,
    )
    with pytest.raises(GuardrailError, match="aggregate input limit"):
        provider.structural_diff(
            _context(_json_bytes(_before())),
            _context(_json_bytes(_after())),
            StructuralDiffBudget(max_input_bytes=16),
        )


def test_structural_diff_bounds_nesting_independently_of_provider() -> None:
    provider = OpenApiCanonicalProvider()

    with pytest.raises(GuardrailError, match="nesting limit"):
        provider.structural_diff(
            _context(_json_bytes(_before())),
            _context(_json_bytes(_after())),
            StructuralDiffBudget(max_depth=1),
        )


def test_tree_work_budget_does_not_queue_unbudgeted_wide_input() -> None:
    value = [None] * 100_000
    tracemalloc.start()
    try:
        with pytest.raises(GuardrailError, match="aggregate work limit"):
            _validate_tree(value, StructuralDiffBudget(max_work_steps=1))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 2 * 1024 * 1024


def test_diff_work_budget_rejects_wide_union_before_allocating_it() -> None:
    before = {f"a{index:06d}": None for index in range(100_000)}
    after = {f"b{index:06d}": None for index in range(100_000)}
    tracemalloc.start()
    try:
        with pytest.raises(GuardrailError, match="aggregate work limit"):
            _diff_json(
                before,
                after,
                path="",
                depth=0,
                budget=StructuralDiffBudget(max_work_steps=1),
                changes=[],
            )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 2 * 1024 * 1024
