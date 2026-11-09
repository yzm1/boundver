"""Structured facet attribution must not infer identity from display text."""

import json
from pathlib import Path
from urllib.parse import urljoin

import pytest

from boundver._lockfile import generate_lockfile, verify_lockfile
from tests._repo_fixtures import commit_all, init_git_repo


def validate_scoped_output(payload):
    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError:
        return
    spec = Path(__file__).resolve().parents[1] / "spec"
    scope = json.loads((spec / "cli-output.verification-scope.schema.json").read_text())
    verification = json.loads((spec / "cli-output.verify.schema.json").read_text())
    # Resolve schema composition locally; test validation must never fetch.
    registry = Registry().with_resource(
        urljoin(scope["$id"], "cli-output.verify.schema.json"), Resource.from_contents(verification),
    )
    jsonschema.Draft202012Validator(scope, registry=registry).validate(payload)


@pytest.fixture
def scope_repository(tmp_path):
    init_git_repo(tmp_path)
    config = {"project": "scope", "components": {}}
    for name in ("clean", "changed"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "contract.txt").write_text("original\n", encoding="utf-8")
        config["components"][name] = {
            "path": name,
            "boundary": {"provider": "path-hash", "paths": ["contract.txt"]},
        }
    (tmp_path / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(tmp_path)
    lock = generate_lockfile(config, tmp_path)
    return tmp_path, config, lock


def test_clean_selected_component_does_not_inherit_global_config_drift(scope_repository):
    root, config, lock = scope_repository
    config["components"]["changed"]["external_consumers"] = ["external"]
    report = {"stale": True}
    issues = verify_lockfile(config, lock, root, components_filter=["clean"], verification_report=report)
    assert any(issue.startswith("METADATA MISMATCH config_digest") for issue in issues)
    assert report["traversal_complete"]
    assert report["computed_components"] == ["clean"]
    assert report["compared_components"] == ["clean"]
    assert report["component_findings"] == []
    assert "stale" not in report


def test_receipt_separates_computation_dependencies_from_compared_components(scope_repository):
    root, config, _ = scope_repository
    config["slices"] = {"shared": {"components": ["clean", "changed"], "mode": "boundary"}}
    lock = generate_lockfile(config, root)
    (root / "changed" / "contract.txt").write_text("slice dependency changed\n", encoding="utf-8")
    commit_all(root)
    report = {}
    issues = verify_lockfile(config, lock, root, components_filter=["clean"], verification_report=report)
    assert report["computed_components"] == ["changed", "clean"]
    assert report["compared_components"] == ["clean"]
    assert report["compared_slices"] == ["shared"]
    assert report["component_findings"] == []
    assert any(issue.startswith("SLICE MISMATCH shared.boundary") for issue in issues)


def test_structured_findings_include_gating_and_observed_facets(scope_repository):
    root, config, lock = scope_repository
    (root / "changed" / "contract.txt").write_text("changed\n", encoding="utf-8")
    commit_all(root)
    report = {}
    issues = verify_lockfile(config, lock, root, facets=["boundary"], verification_report=report)
    assert issues and report["traversal_complete"]
    findings = {row["facet"]: row for row in report["component_findings"]}
    assert set(findings) == {"exact", "boundary"}
    assert findings["boundary"]["gating"] and not findings["exact"]["gating"]
    assert all(row["component"] == "changed" and row["before"] != row["after"] for row in findings.values())


def test_preflight_failure_clears_receipt_without_claiming_analysis(scope_repository):
    root, config, lock = scope_repository
    lock["schema"] = "boundary-lock/v3"
    report = {"traversal_complete": True, "component_findings": [{"old": "data"}]}
    assert verify_lockfile(config, lock, root, verification_report=report)
    assert not report["traversal_complete"]
    assert report["computed_components"] == report["component_findings"] == []


def test_receipt_is_opt_in_and_does_not_change_gating(scope_repository):
    root, config, lock = scope_repository
    (root / "changed" / "contract.txt").write_text("changed\n", encoding="utf-8")
    commit_all(root)
    plain = verify_lockfile(config, lock, root, fail_fast=True)
    report = {}
    structured = verify_lockfile(config, lock, root, fail_fast=True, verification_report=report)
    assert plain == structured
    assert report["traversal_complete"] and len(report["component_findings"]) == 2


def test_inherited_version_dependency_is_not_misreported_as_fingerprinted(scope_repository):
    root, config, _ = scope_repository
    config["components"]["changed"]["version_source"] = {"constant": "1.0.0"}
    config["components"]["clean"]["version_source"] = {"component": "changed"}
    lock = generate_lockfile(config, root)
    report = {}
    assert verify_lockfile(config, lock, root, components_filter=["clean"], verification_report=report) == []
    assert report["computed_components"] == report["compared_components"] == ["clean"]
    assert report["version_components"] == ["changed", "clean"]


def _commit_scope_inputs(root, config, lock):
    from boundver._lockfile import dump_lockfile

    (root / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    (root / "boundary.lock.json").write_text(dump_lockfile(lock), encoding="utf-8")
    commit_all(root)


def test_scoped_json_keeps_global_config_failure_separate_from_clean_selection(scope_repository):
    from tests._parity import run_cli

    root, config, lock = scope_repository
    _commit_scope_inputs(root, config, lock)
    config["components"]["changed"]["external_consumers"] = ["outside"]
    (root / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(root)
    result = run_cli(root, "verify", "--components", "clean", "--format", "scoped-json")
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    validate_scoped_output(payload)
    assert payload["schema"] == "boundver-verification-scope/v1"
    assert payload["selection"]["comparison_complete"]
    assert payload["selection"]["fingerprint_drift"] is False
    assert not payload["verification"]["ok"]
    assert any(issue.startswith("METADATA MISMATCH config_digest") for issue in payload["repository_issues"])


def test_changed_from_empty_selection_still_reports_full_integrity(scope_repository):
    from tests._parity import run_cli

    root, config, lock = scope_repository
    _commit_scope_inputs(root, config, lock)
    (root / "changed" / "contract.txt").write_text("stale lock\n", encoding="utf-8")
    commit_all(root)
    result = run_cli(root, "verify", "--changed-from", "HEAD", "--format", "scoped-json")
    assert result.returncode == 4
    payload = json.loads(result.stdout)
    validate_scoped_output(payload)
    assert payload["selection"]["components"] == []
    assert payload["selection"]["fingerprint_drift"] is False
    assert payload["analysis"]["compared_components"] == ["changed", "clean"]
    assert payload["other_component_findings"]
    assert not payload["verification"]["ok"]


def test_selected_drift_is_not_duplicated_as_repository_issue(scope_repository):
    from tests._parity import run_cli

    root, config, lock = scope_repository
    config["components"]["clean"]["consumers"] = ["changed"]
    lock = generate_lockfile(config, root)
    _commit_scope_inputs(root, config, lock)
    (root / "clean" / "contract.txt").write_text("drift\n", encoding="utf-8")
    commit_all(root)
    result = run_cli(root, "verify", "--components", "clean", "--format", "scoped-json")
    assert result.returncode == 4
    payload = json.loads(result.stdout)
    validate_scoped_output(payload)
    assert payload["selection"]["fingerprint_drift"] is True
    assert len(payload["selection"]["fingerprint_findings"]) == 2
    assert payload["repository_issues"] == []
    assert payload["verification"]["consumer_impact"][0]["components"] == ["changed"]


def test_unavailable_selected_facet_is_not_reported_clean(scope_repository):
    from types import SimpleNamespace
    from boundver._verification_scope import verification_scope_result

    root, config, lock = scope_repository
    receipt = {}
    issues = verify_lockfile(config, lock, root, facets=["compat"], verification_report=receipt)
    assert issues and receipt["unavailable_components"] == ["changed", "clean"]
    result = verification_scope_result(
        SimpleNamespace(components="clean", changed_from=""), receipt,
        {"components_filter": ["clean"], "changed_components": [], "issues": issues},
    )
    assert not result["selection"]["comparison_complete"]
    assert result["selection"]["fingerprint_drift"] is None


def test_unknown_scoped_component_returns_incomplete_versioned_json(scope_repository):
    from tests._parity import run_cli

    root, config, lock = scope_repository
    _commit_scope_inputs(root, config, lock)
    process = run_cli(root, "verify", "--components", "unknown", "--format", "scoped-json")
    assert process.returncode == 2
    payload = json.loads(process.stdout)
    validate_scoped_output(payload)
    assert payload["selection"]["components"] == ["unknown"]
    assert payload["selection"]["fingerprint_drift"] is None
    assert not payload["analysis"]["traversal_complete"]


def test_text_keeps_clean_selection_separate_from_global_failure(scope_repository):
    from tests._parity import run_cli

    root, config, lock = scope_repository
    _commit_scope_inputs(root, config, lock)
    config["components"]["changed"]["external_consumers"] = ["outside"]
    (root / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(root)
    process = run_cli(root, "verify", "--components", "clean")
    assert process.returncode != 0
    assert "Selected components (1): no fingerprint drift." in process.stdout
    assert "Repository integrity and affected-slice checks still gate" in process.stdout


def test_scoped_json_assigns_shared_slice_drift_to_repository_not_clean_member(scope_repository):
    from tests._parity import run_cli

    root, config, _ = scope_repository
    config["slices"] = {"shared": {"components": ["clean", "changed"], "mode": "boundary"}}
    lock = generate_lockfile(config, root)
    _commit_scope_inputs(root, config, lock)
    (root / "changed" / "contract.txt").write_text("dependency changed\n", encoding="utf-8")
    commit_all(root)
    process = run_cli(root, "verify", "--components", "clean", "--format", "scoped-json")
    assert process.returncode == 4
    payload = json.loads(process.stdout)
    validate_scoped_output(payload)
    assert payload["selection"]["fingerprint_drift"] is False
    assert payload["analysis"]["computed_components"] == ["changed", "clean"]
    assert payload["analysis"]["compared_slices"] == ["shared"]
    assert any(issue.startswith("SLICE MISMATCH shared.boundary") for issue in payload["repository_issues"])
