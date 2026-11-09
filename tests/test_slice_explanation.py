"""Slice aggregate drift, member contributions and version movement are distinct."""

import json
from pathlib import Path
import subprocess

import pytest

from boundver._lockfile import dump_lockfile, generate_lockfile
from boundver._slice_explanation import analyze_slice_drift as _analyze_slice_drift
from boundver._version_changes import resolved_version_change
from tests._parity import run_cli
from tests._repo_fixtures import commit_all, init_git_repo


def validate_result(result):
    try:
        import jsonschema
    except ImportError:
        return
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "spec"
            / "cli-output.slice-explanation.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(result)


def analyze_slice_drift(*args, **kwargs):
    result = _analyze_slice_drift(*args, **kwargs)
    validate_result(result)
    return result


@pytest.fixture
def slice_repository(tmp_path):
    init_git_repo(tmp_path)
    config = {
        "project": "slice",
        "defaults": {"verify_facets": ["boundary"]},
        "components": {},
        "slices": {"api": {"mode": "boundary", "components": ["a", "b"]}},
    }
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "contract.txt").write_text("original\n", encoding="utf-8")
        config["components"][name] = {
            "path": name,
            "boundary": {"provider": "path-hash", "paths": ["contract.txt"]},
        }
    (tmp_path / "a" / "version.json").write_text(
        '{"version":"1.0.0"}\n', encoding="utf-8"
    )
    config["components"]["a"]["version_source"] = {
        "file": "version.json",
        "field": "version",
    }
    (tmp_path / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(tmp_path)
    lock = generate_lockfile(config, tmp_path)
    (tmp_path / "boundary.lock.json").write_text(dump_lockfile(lock), encoding="utf-8")
    commit_all(tmp_path)
    return tmp_path, config, lock


@pytest.mark.parametrize("limit", [0, 1, 1000])
def test_contributing_members_are_counted_when_rows_are_hidden(slice_repository, limit):
    root, config, lock = slice_repository
    before = (root / "boundary.lock.json").read_bytes()
    (root / "a" / "contract.txt").write_text("changed\n", encoding="utf-8")
    commit_all(root)
    result = analyze_slice_drift(config, lock, root, "api", limit=limit)
    assert result["complete"] and result["comparable"], result["issues"]
    assert result["drifted"] and result["exit_code"] == 1
    assert result["counts"]["contributing_members"] == 1
    assert result["counts"]["changed_members"] == 1
    assert len(result["members"]) == min(limit, 2)
    assert result["presentation"]["omitted"] == 2 - min(limit, 2)
    assert result["locked_source"] == {"status": "unavailable", "commit": None}
    assert (root / "boundary.lock.json").read_bytes() == before


def test_version_movement_does_not_imply_boundary_slice_drift(slice_repository):
    root, config, lock = slice_repository
    (root / "a" / "version.json").write_text('{"version":"1.1.0"}\n', encoding="utf-8")
    commit_all(root)
    result = analyze_slice_drift(config, lock, root, "api")
    assert result["complete"] and result["exit_code"] == 0, result["issues"]
    assert not result["slice_changed"] and not result["drifted"]
    assert result["counts"]["version_changes"] == 1
    assert result["counts"]["unavailable_versions"] == 1
    assert result["members"][0]["version"] == {
        "previous": "1.0.0",
        "current": "1.1.0",
        "status": "changed",
    }
    assert not result["members"][0]["contributes"]


def test_declaration_change_is_incomparable_not_ordinary_slice_drift(slice_repository):
    root, config, lock = slice_repository
    config["components"]["a"]["consumers"] = ["b"]
    result = analyze_slice_drift(config, lock, root, "api")
    assert not result["complete"] and not result["comparable"]
    assert result["drifted"] is None and result["exit_code"] == 2
    assert any("config_digest" in issue for issue in result["issues"])


def test_corrupt_aggregate_is_not_reported_as_source_drift(slice_repository):
    root, config, lock = slice_repository
    lock["slices"]["api"]["fingerprint"] = "0" * 64
    result = analyze_slice_drift(config, lock, root, "api")
    assert not result["complete"] and result["exit_code"] == 2
    assert any("inconsistent" in issue for issue in result["issues"])


def test_unknown_slice_is_actionable_and_incomplete(slice_repository):
    root, config, lock = slice_repository
    result = analyze_slice_drift(config, lock, root, "unknown")
    assert not result["complete"] and result["exit_code"] == 2
    assert "Unknown slice" in result["issues"][0]


@pytest.mark.parametrize(
    "options,expected", [([], 2), (["--source", "head"], 0), (["--summary"], 0)]
)
def test_cli_requires_one_target_and_reports_slice_json(
    slice_repository, options, expected
):
    root, _, _ = slice_repository
    target = [] if not options else ["--slice", "api"]
    result = run_cli(root, "why", *target, "--format", "json", *options)
    assert result.returncode == expected, result.stderr
    if expected == 0:
        payload = json.loads(result.stdout)
        validate_result(payload)
        assert (
            payload["schema"] == "boundver-slice-explanation/v1" and payload["complete"]
        )
        assert len(payload["members"]) == (0 if "--summary" in options else 2)


@pytest.mark.parametrize(
    "previous,current,status",
    [
        ({"version": "1.0.0"}, {"version": "1.0.0"}, "unchanged"),
        ({"version": None}, {"version": None}, "unavailable"),
        (
            {"version": "1.0.0"},
            {"version": "2.0.0", "version_errors": ["invalid"]},
            "unavailable",
        ),
        ({"version": "bad"}, {"version": "1.0.0"}, "unavailable"),
    ],
)
def test_version_status_does_not_call_unavailable_values_equal(
    previous, current, status
):
    assert resolved_version_change(previous, current)["status"] == status


def test_provider_contract_change_is_not_reported_as_comparable_drift(slice_repository):
    root, config, lock = slice_repository
    lock["components"]["a"]["boundary_provider_version"] = "another-runtime"
    result = analyze_slice_drift(config, lock, root, "api")
    assert not result["complete"] and result["exit_code"] == 2
    assert "provider contract changed" in result["issues"][0]


def test_head_explanation_ignores_deleted_disk_files_and_held_index_lock(
    slice_repository,
):
    root, config, lock = slice_repository
    (root / "a" / "contract.txt").unlink()
    index_lock = root / ".git" / "index.lock"
    index_lock.write_bytes(b"another session")
    try:
        result = analyze_slice_drift(config, lock, root, "api")
        assert result["complete"] and not result["slice_changed"]
        assert index_lock.read_bytes() == b"another session"
        assert not (root / "a" / "contract.txt").exists()
    finally:
        index_lock.unlink()


@pytest.mark.parametrize("source", ["index", "working-tree"])
def test_explicit_source_is_used_for_member_changes(slice_repository, source):
    root, config, lock = slice_repository
    path = root / "a" / "contract.txt"
    path.write_text("staged\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "a/contract.txt"], cwd=root, check=True, capture_output=True
    )
    if source == "index":
        path.write_text("original\n", encoding="utf-8")
    result = analyze_slice_drift(config, lock, root, "api", source=source)
    assert result["complete"] and result["slice_changed"], result["issues"]
    assert result["source"]["mode"] == source
    assert (result["source"]["tree"] is None) == (source == "working-tree")


def test_unavailable_member_facet_is_not_clean_even_if_aggregate_bytes_match(
    slice_repository,
):
    root, config, _ = slice_repository
    config["slices"]["api"]["mode"] = "compat"
    lock = generate_lockfile(config, root, strict=False)
    result = analyze_slice_drift(config, lock, root, "api")
    assert not result["complete"] and result["exit_code"] == 2
    assert result["slice_changed"] is None


def test_cli_slice_drift_and_unknown_name_use_explicit_outcomes(slice_repository):
    root, _, _ = slice_repository
    (root / "a" / "contract.txt").write_text("changed\n", encoding="utf-8")
    commit_all(root)
    result = run_cli(root, "why", "--slice", "api", "--summary", "--format", "json")
    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    validate_result(payload)
    assert payload["counts"]["contributing_members"] == 1 and not payload["members"]
    unknown = run_cli(root, "why", "--slice", "unknown", "--format", "json")
    assert unknown.returncode == 2
    validate_result(json.loads(unknown.stdout))


@pytest.mark.parametrize(
    "options",
    [
        ["a", "--slice", "api"],
        ["--slice", "api", "--base-ref", "HEAD"],
        ["--slice", "api", "--limit", "-1", "--summary"],
        ["--slice", "api", "--transitive"],
    ],
)
def test_cli_rejects_ambiguous_or_unsupported_slice_options(slice_repository, options):
    root, _, _ = slice_repository
    result = run_cli(root, "why", *options)
    assert result.returncode == 2


def test_output_budget_uses_the_actual_pretty_printed_representation(
    slice_repository, monkeypatch
):
    from boundver import _slice_explanation

    root, config, lock = slice_repository
    result = analyze_slice_drift(config, lock, root, "api")
    compact = len(json.dumps(result).encode("utf-8"))
    pretty = len(json.dumps(result, indent=2, sort_keys=True).encode("utf-8")) + 1
    assert compact < pretty
    monkeypatch.setattr(_slice_explanation, "MAX_SLICE_EXPLANATION_BYTES", compact + 1)
    result = analyze_slice_drift(config, lock, root, "api")
    assert not result["complete"] and result["exit_code"] == 2
    assert result["counts"] is None and not result["members"]
    assert "output budget" in result["issues"][0]
