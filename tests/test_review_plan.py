"""CI-native routing plans derived from one immutable range review."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from boundver._review import analyze_review_range
from boundver._review_plan import (
    PLAN_CLAIM,
    PLAN_SCHEMA,
    build_review_plan,
    render_review_plan_markdown,
)
from boundver._utils import ConfigError
from tests.test_review import _make_range, _run_cli


ROOT = Path(__file__).resolve().parents[1]


def test_plan_is_a_complete_schema_valid_routing_projection(tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    base, target = _make_range(tmp_path)

    review = analyze_review_range(tmp_path, base, target, transitive=True)
    plan = build_review_plan(review)

    assert plan["schema"] == PLAN_SCHEMA
    assert plan["complete"] is True
    assert plan["claim"] == PLAN_CLAIM
    assert plan["endpoints"] == review["endpoints"]
    assert plan["policy"] == review["policy"]
    assert plan["selection"]["changed_components"] == ["layer"]
    assert plan["selection"]["impacted_components"] == [
        "app",
        "legacy",
        "new",
        "service",
    ]
    assert plan["selection"]["test_components"] == [
        "app",
        "layer",
        "legacy",
        "new",
        "service",
    ]
    assert plan["selection"]["external_consumers"] == [
        "new-audit",
        "old-audit",
    ]
    assert plan["source_locations"] == [
        {
            "component": "layer",
            "path": "layer/api.json",
            "endpoint": "target",
            "commit": target,
            "kind": "structural-document",
        }
    ]
    schema = json.loads(
        (ROOT / "spec" / "cli-output.plan.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(plan, schema)


def test_plan_clean_range_has_empty_deterministic_selection(tmp_path: Path) -> None:
    base, _target = _make_range(tmp_path)

    plan = build_review_plan(analyze_review_range(tmp_path, base, base))

    assert plan["selection"] == {
        "changed_components": [],
        "impacted_components": [],
        "external_consumers": [],
        "test_components": [],
        "changed_slices": [],
        "impacted_slices": [],
        "test_slices": [],
    }
    assert plan["summary"]["structural_complete"] is True
    assert plan["source_locations"] == []


def test_plan_distinguishes_direct_and_transitive_impact(tmp_path: Path) -> None:
    base, target = _make_range(tmp_path)

    direct = build_review_plan(analyze_review_range(tmp_path, base, target))
    transitive = build_review_plan(
        analyze_review_range(tmp_path, base, target, transitive=True)
    )

    assert direct["policy"]["impact"] == "direct"
    assert direct["selection"]["impacted_components"] == [
        "legacy",
        "new",
        "service",
    ]
    assert transitive["policy"]["impact"] == "transitive"
    assert transitive["selection"]["impacted_components"] == [
        "app",
        "legacy",
        "new",
        "service",
    ]
    assert transitive["consumer_impact"][0]["graph_changed"] is True


def test_plan_omits_source_annotation_without_a_precise_document(
    tmp_path: Path,
) -> None:
    base, target = _make_range(tmp_path, provider="path-hash")

    plan = build_review_plan(analyze_review_range(tmp_path, base, target))

    assert plan["structural_changes"]["complete"] is False
    assert plan["source_locations"] == []


def test_cli_writes_plan_and_same_capture_markdown_summary(tmp_path: Path) -> None:
    base, target = _make_range(tmp_path)
    summary_path = tmp_path / "ci" / "boundver-summary.md"

    completed = _run_cli(
        tmp_path,
        "review",
        "--base",
        base,
        "--target",
        target,
        "--merge-base",
        "--transitive",
        "--format",
        "plan",
        "--summary-file",
        str(summary_path),
    )

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    summary = summary_path.read_text(encoding="utf-8")
    assert plan["schema"] == PLAN_SCHEMA
    assert plan["endpoints"]["base"]["requested_commit"] == base
    assert plan["endpoints"]["target"]["commit"] == target
    assert base in summary
    assert target in summary
    assert "Routing evidence only" in summary
    assert "Presentation complete" in summary


def test_summary_file_is_rejected_for_non_plan_output(tmp_path: Path) -> None:
    base, target = _make_range(tmp_path)
    summary_path = tmp_path / "should-not-exist.md"

    completed = _run_cli(
        tmp_path,
        "review",
        f"{base}..{target}",
        "--format",
        "json",
        "--summary-file",
        str(summary_path),
    )

    assert completed.returncode == 2
    assert "--summary-file requires --format plan" in completed.stderr
    assert not summary_path.exists()


@pytest.mark.parametrize("input_name", ["boundary.config.json", "boundary.lock.json"])
def test_summary_file_cannot_overwrite_an_endpoint_input(
    tmp_path: Path,
    input_name: str,
) -> None:
    base, target = _make_range(tmp_path)
    protected = tmp_path / input_name
    expected = protected.read_bytes()

    completed = _run_cli(
        tmp_path,
        "review",
        f"{base}..{target}",
        "--format",
        "plan",
        "--summary-file",
        input_name,
    )

    assert completed.returncode == 2
    assert "aliases the" in completed.stderr
    assert (
        f"endpoint {'config' if 'config' in input_name else 'lock'}" in completed.stderr
    )
    assert protected.read_bytes() == expected


@pytest.mark.parametrize("absolute", [False, True])
def test_summary_file_rejects_normalized_and_absolute_input_aliases(
    tmp_path: Path,
    absolute: bool,
) -> None:
    base, target = _make_range(tmp_path)
    protected = tmp_path / "boundary.config.json"
    expected = protected.read_bytes()
    alias = (
        str(protected.resolve())
        if absolute
        else (Path("unused") / ".." / protected.name).as_posix()
    )

    completed = _run_cli(
        tmp_path,
        "review",
        f"{base}..{target}",
        "--format",
        "plan",
        "--summary-file",
        alias,
    )

    assert completed.returncode == 2
    assert "aliases the" in completed.stderr
    assert protected.read_bytes() == expected


def test_summary_file_rejects_an_existing_hardlink_to_an_input(
    tmp_path: Path,
) -> None:
    base, target = _make_range(tmp_path)
    protected = tmp_path / "boundary.config.json"
    alias = tmp_path / "summary-hardlink.md"
    try:
        os.link(protected, alias)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")
    expected = protected.read_bytes()

    completed = _run_cli(
        tmp_path,
        "review",
        f"{base}..{target}",
        "--format",
        "plan",
        "--summary-file",
        alias.name,
    )

    assert completed.returncode == 2
    assert "aliases the" in completed.stderr
    assert protected.read_bytes() == expected
    assert alias.read_bytes() == expected


def test_bounded_summary_never_presents_partial_rows_as_the_complete_plan(
    tmp_path: Path,
) -> None:
    base, target = _make_range(tmp_path)
    plan = build_review_plan(
        analyze_review_range(tmp_path, base, target, transitive=True)
    )

    summary = render_review_plan_markdown(plan, max_rows=1, max_bytes=4096)

    assert "Presentation truncated: 1 of" in summary
    assert "complete boundver-plan/v1 result remains" in summary
    assert "Presentation complete" not in summary


def test_markdown_summary_escapes_repository_controlled_markup(
    tmp_path: Path,
) -> None:
    base, target = _make_range(tmp_path)
    plan = build_review_plan(analyze_review_range(tmp_path, base, target))
    plan["source_locations"][0]["path"] = "layer/</code>\n::error::api.json"

    summary = render_review_plan_markdown(plan)

    assert "&lt;/code&gt;\\n::error::api.json" in summary
    assert "\n::error::api.json" not in summary


def test_plan_builder_refuses_incomplete_or_wrong_contracts() -> None:
    with pytest.raises(ConfigError, match="complete boundver-review/v1"):
        build_review_plan({"schema": "boundver-review/v1", "complete": False})
    with pytest.raises(ConfigError, match="complete boundver-review/v1"):
        build_review_plan({"schema": "other", "complete": True})
