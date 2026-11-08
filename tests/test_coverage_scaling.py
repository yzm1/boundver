"""Large coverage listings must retain honest analysis scope and gate totals."""

import json
from pathlib import Path

import pytest

from boundver import _coverage as coverage
from tests._parity import run_cli, run_cli_in_process
from tests._repo_fixtures import commit_all, init_git_repo


@pytest.fixture(scope="module")
def large_repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("coverage-scale")
    init_git_repo(root)
    (root / "service").mkdir()
    (root / "client").mkdir()
    (root / "external").mkdir()
    (root / "service" / "contract.json").write_text("{}", encoding="utf-8")
    (root / "client" / "client.txt").write_text("client", encoding="utf-8")
    (root / "external" / "extra.py").write_text("extra", encoding="utf-8")
    for index in range(1100):
        (root / "service" / f"part-{index:04d}.py").write_text("pass\n", encoding="utf-8")
    config = {
        "project": "coverage-scale",
        "components": {
            "svc": {"path": "service", "boundary": {
                "provider": "path-hash", "paths": ["contract.json"]},
                "behavior": {"paths": ["contract.json"]}},
            "client": {"path": "client", "boundary": {"provider": "leaf", "paths": []}},
        },
        "coverage": {
            # A single ownership indicator cannot narrow component omissions.
            "source_indicators": ["external/*.py"],
            "exclusions": [{"paths": ["service/*.py"], "facets": ["behavior"],
                            "reason": "implementation outside runtime contract"}],
        },
    }
    (root / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(root)
    return root


def _report(root, *args):
    result = run_cli(root, "coverage", "--format", "json", *args)
    return result, json.loads(result.stdout)


def _groups(report):
    return [facet for component in report["components"] for facet in component["facets"]] + report["unowned_source_directories"]


def _assert_totals(report):
    groups = _groups(report)
    shown = sum(len(group["uncovered_files"]) + len(group["excluded"]) for group in groups)
    total = sum(group["uncovered_count"] + group["excluded_count"] for group in groups)
    assert shown == report["presentation"]["shown_paths"]
    assert total == report["presentation"]["total_paths"]
    assert total - shown == report["presentation"]["omitted_paths"]
    assert report["summary"]["uncovered_files"] == sum(group["uncovered_count"] for group in groups)
    assert report["summary"]["excluded_files"] == sum(group["excluded_count"] for group in groups)


def test_default_large_audit_finishes_and_counts_hidden_uncovered_paths(large_repo):
    result, report = _report(large_repo, "--strict")
    assert result.returncode == 1, result.stderr
    assert report["schema"] == "boundver-declaration-coverage/v2"
    assert report["complete"] is True and report["ok"] is False
    assert report["summary"]["uncovered_files"] == 1101
    assert report["summary"]["excluded_files"] == 1100
    assert report["presentation"]["shown_paths"] == 1000
    assert report["presentation"]["omitted_paths"] == 1201
    assert report["presentation"]["truncated"] is True
    # Every displayed row is an exclusion. Strict failure must not depend on
    # whether the later real uncovered rows fit in the displayed prefix.
    assert not any(group["uncovered_files"] for group in _groups(report))
    assert report["scope"]["filtered"] is False
    _assert_totals(report)


@pytest.mark.parametrize("args", [("--summary",), ("--limit", "0")])
def test_summary_and_zero_limit_keep_strict_failure_and_counts(large_repo, args):
    result, report = _report(large_repo, *args, "--strict")
    assert result.returncode == 1
    assert report["presentation"]["shown_paths"] == 0
    assert report["presentation"]["total_paths"] == 2201
    assert report["summary"]["uncovered_files"] == 1101
    _assert_totals(report)


def test_larger_limit_remains_subject_to_detail_byte_budget(large_repo):
    result, report = _report(large_repo, "--limit", "3000")
    assert result.returncode == 0
    assert 1000 < report["presentation"]["shown_paths"] <= 2201
    assert report["presentation"]["total_paths"] == 2201
    _assert_totals(report)


def test_component_filter_skips_ownership_but_preserves_component_totals(large_repo):
    result, report = _report(large_repo, "--component", "svc", "--summary")
    assert result.returncode == 0
    assert report["scope"] == {"filtered": True, "components": ["svc"],
                               "path_prefixes": [], "ownership_analyzed": False}
    assert report["unowned_source_directories"] == []
    assert report["summary"]["uncovered_files"] == 1100
    assert report["summary"]["excluded_files"] == 1100
    _assert_totals(report)


def test_component_union_is_canonical_and_clean_component_is_scoped(large_repo):
    result, report = _report(large_repo, "--component", "client", "--strict")
    assert result.returncode == 0 and report["ok"] is True
    assert report["scope"]["filtered"] is True
    assert report["components"][0]["tracked_files"] == 1
    assert report["components"][0]["facets"] == []
    result, report = _report(large_repo, "--component", "svc", "--component", "client",
                             "--component", "svc", "--summary")
    assert report["scope"]["components"] == ["client", "svc"]


def test_prefix_filters_component_and_ownership_paths_as_a_union(large_repo):
    result, report = _report(large_repo, "--path-prefix", "service/part-0001.py",
                             "--path-prefix", "external/", "--strict")
    assert result.returncode == 1
    assert report["scope"]["path_prefixes"] == ["external", "service/part-0001.py"]
    assert report["scope"]["ownership_analyzed"] is True
    assert report["summary"]["uncovered_files"] == 2
    assert report["summary"]["excluded_files"] == 1
    assert report["presentation"]["truncated"] is False
    _assert_totals(report)


def test_prefix_matching_is_segment_aware_not_a_string_startswith(large_repo):
    result, report = _report(large_repo, "--path-prefix", "serv", "--strict")
    assert result.returncode == 0 and report["ok"] is True
    assert report["scope"]["filtered"] is True
    assert report["presentation"]["total_paths"] == 0


@pytest.mark.parametrize("args", [
    ("--component", "missing"), ("--path-prefix", "../escape"),
    ("--path-prefix", "/absolute"), ("--path-prefix", "C:/drive"),
    ("--path-prefix", "service/*.py"), ("--path-prefix", "service\\part"),
    ("--limit", "-1"), ("--limit", "10001"),
])
def test_invalid_filters_and_limits_fail_closed_with_json(large_repo, args):
    result, report = _report(large_repo, *args)
    assert result.returncode == 2
    assert report["complete"] is report["ok"] is False
    assert report["issues"]


@pytest.mark.parametrize("constant", ["MAX_COVERAGE_ANALYZED_PATHS", "MAX_COVERAGE_GROUPS",
                                      "MAX_COVERAGE_RESULT_BYTES"])
def test_real_work_or_result_exhaustion_is_not_a_partial_clean_report(large_repo, monkeypatch, constant):
    monkeypatch.setattr(coverage, constant, 1)
    result = run_cli_in_process(large_repo, "coverage", "--format", "json", "--summary")
    report = json.loads(result.stdout)
    assert result.returncode == 2
    assert report["complete"] is report["ok"] is False
    assert report["issues"]


def test_detail_byte_exhaustion_truncates_presentation_not_analysis(large_repo, monkeypatch):
    monkeypatch.setattr(coverage, "MAX_COVERAGE_DETAIL_BYTES", 1)
    result = run_cli_in_process(large_repo, "coverage", "--format", "json", "--strict")
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert report["complete"] is True
    assert report["presentation"]["shown_paths"] == 0
    assert report["presentation"]["omitted_paths"] == 2201


def test_glob_analysis_budget_exhaustion_is_explicit_even_in_summary(large_repo, monkeypatch):
    original = coverage._PathGlobOperation
    monkeypatch.setattr(coverage, "_PathGlobOperation", lambda label: original(label, max_steps=2))
    result = run_cli_in_process(large_repo, "coverage", "--format", "json", "--summary")
    report = json.loads(result.stdout)
    assert result.returncode == 2
    assert report["complete"] is report["ok"] is False
    assert "steps" in report["issues"][0]


def test_component_filter_does_not_hide_invalid_other_config(tmp_path):
    init_git_repo(tmp_path)
    (tmp_path / "client").mkdir()
    (tmp_path / "client" / "file.txt").write_text("client", encoding="utf-8")
    config = {"project": "coverage-invalid", "components": {
        "client": {"path": "client", "boundary": {"provider": "leaf", "paths": []}},
        "broken": {"path": "missing", "boundary": {"provider": "leaf", "paths": []}},
    }}
    (tmp_path / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(tmp_path)
    result, report = _report(tmp_path, "--component", "client")
    assert result.returncode == 2
    assert report["complete"] is report["ok"] is False
    assert any("broken" in issue for issue in report["issues"])


def test_no_source_indicators_does_not_invent_ownership_coverage(tmp_path):
    init_git_repo(tmp_path)
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "contract.json").write_text("{}", encoding="utf-8")
    (tmp_path / "outside.py").write_text("pass", encoding="utf-8")
    config = {"project": "coverage-no-indicators", "components": {"svc": {"path": "service", "boundary": {
        "provider": "path-hash", "paths": ["contract.json"]}}}}
    (tmp_path / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(tmp_path)
    result, report = _report(tmp_path, "--strict", "--summary")
    assert result.returncode == 0
    assert report["policy"]["source_indicators"] == []
    assert report["summary"]["source_indicator_files"] == 0
    assert report["unowned_source_directories"] == []


def test_all_excluded_report_is_strict_clean_even_when_paths_are_omitted(tmp_path):
    init_git_repo(tmp_path)
    (tmp_path / "service").mkdir()
    (tmp_path / "service" / "contract.json").write_text("{}", encoding="utf-8")
    (tmp_path / "service" / "impl.py").write_text("pass", encoding="utf-8")
    (tmp_path / "outside.py").write_text("pass", encoding="utf-8")
    config = {
        "project": "coverage-excluded",
        "components": {"svc": {"path": "service",
            "boundary": {"provider": "path-hash", "paths": ["contract.json"]},
            "behavior": {"paths": ["contract.json"]}}},
        "coverage": {"source_indicators": ["**/*.py"], "exclusions": [{
            "paths": ["**/*.py"], "facets": ["behavior", "boundary", "ownership"],
            "reason": "reviewed advisory omission fixture"}]},
    }
    (tmp_path / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(tmp_path)
    result, report = _report(tmp_path, "--strict", "--summary")
    assert result.returncode == 0, report
    assert report["complete"] is report["ok"] is True
    assert report["presentation"]["truncated"] is True
    assert report["summary"]["uncovered_files"] == 0
    assert report["summary"]["excluded_files"] == 3
    _assert_totals(report)


def test_missing_selected_config_produces_explicit_json_failure(large_repo):
    result, report = _report(large_repo, "--config", "missing.json")
    assert result.returncode == 2
    assert report["complete"] is report["ok"] is False
    assert report["schema"] == "boundver-declaration-coverage/v2"
    assert report["issues"]


def test_filter_count_budgets_are_not_bypassed_by_duplicate_inputs(large_repo):
    for options in [list(("--component", "svc") * 257),
                    list(("--path-prefix", "service") * 257)]:
        result, report = _report(large_repo, *options)
        assert result.returncode == 2
        assert report["complete"] is report["ok"] is False


def test_text_summary_names_filtered_scope_and_omitted_entries(large_repo):
    result = run_cli(large_repo, "coverage", "--component", "svc", "--summary", "--strict")
    assert result.returncode == 1
    assert "Scope: filtered" in result.stdout
    assert "Ownership audit: not selected" in result.stdout
    assert "2200 omitted" in result.stdout
    assert "1100 uncovered" in result.stdout
    assert "UNCOVERED service/part" not in result.stdout


def test_v2_schema_accepts_truncated_complete_filtered_and_failed_results(large_repo, monkeypatch):
    jsonschema = pytest.importorskip("jsonschema")
    path = Path(__file__).resolve().parents[1] / "spec" / "cli-output.coverage.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    for args in [(), ("--summary",), ("--component", "client"), ("--component", "missing")]:
        _, report = _report(large_repo, *args)
        jsonschema.validate(report, schema)
    monkeypatch.setattr(coverage, "MAX_COVERAGE_ANALYZED_PATHS", 1)
    failed = run_cli_in_process(large_repo, "coverage", "--format", "json")
    jsonschema.validate(json.loads(failed.stdout), schema)
