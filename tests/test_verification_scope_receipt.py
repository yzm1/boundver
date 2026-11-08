"""Structured facet attribution must not infer identity from display text."""

import json

import pytest

from boundver._lockfile import generate_lockfile, verify_lockfile
from tests._repo_fixtures import commit_all, init_git_repo


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
