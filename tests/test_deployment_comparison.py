"""A deployment label is an assertion, not an attestation or integrity waiver."""

import json
from pathlib import Path
import subprocess

import pytest

from boundver._deployment import compare_deployment
from boundver._lockfile import dump_lockfile, generate_lockfile
from tests._repo_fixtures import commit_all, init_git_repo


@pytest.fixture
def repository(tmp_path):
    init_git_repo(tmp_path)
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "contract.txt").write_text("original\n", encoding="utf-8")
    config = {"project": "deployment", "defaults": {"verify_facets": ["boundary", "compat"]},
              "components": {"svc": {"path": "svc", "version_source": {"constant": "1.0.0"},
                                     "boundary": {"provider": "path-hash", "paths": ["contract.txt"]}}}}
    (tmp_path / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(tmp_path)
    lock = generate_lockfile(config, tmp_path)
    (tmp_path / "boundary.lock.json").write_text(dump_lockfile(lock), encoding="utf-8")
    commit_all(tmp_path)
    subprocess.run(["git", "tag", "production"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def validate_result(result):
    try:
        import jsonschema
    except ImportError:
        return  # Functional assertions still run without the optional engine.
    path = Path(__file__).resolve().parents[1] / "spec" / "cli-output.deployment.schema.json"
    jsonschema.Draft202012Validator(json.loads(path.read_text())).validate(result)


def test_matching_reference_is_not_deployment_or_current_lock_attestation(repository):
    result = compare_deployment(repository, "production", "production")
    validate_result(result)
    assert result["complete"] and result["ok"] and result["exit_code"] == 0, result["issues"]
    assert result["reference"]["verified"]
    assert not result["evidence"]["attested"]
    assert result["current_lock_verification"] == "not-performed"
    assert result["counts"] == {"components": 1, "changed": 0, "unchanged": 1,
                               "facets": {"exact": 0, "behavior": 0, "boundary": 0, "compat": 0}}


@pytest.mark.parametrize("limit", [0, 1])
def test_stale_current_lock_does_not_hide_drift_even_with_no_rows(repository, limit):
    before = (repository / "boundary.lock.json").read_bytes()
    (repository / "svc" / "contract.txt").write_text("changed\n", encoding="utf-8")
    commit_all(repository)
    result = compare_deployment(repository, "production", "production", limit=limit)
    validate_result(result)
    assert result["complete"] and not result["ok"] and result["exit_code"] == 4
    assert result["counts"]["facets"]["boundary"] == 1
    assert result["presentation"]["omitted"] == 1 - limit
    assert len(result["changes"]) == limit
    assert (repository / "boundary.lock.json").read_bytes() == before


def test_missing_ref_is_incomplete_without_inventing_source_identity(repository):
    result = compare_deployment(repository, "staging", "NO-SUCH-REF")
    validate_result(result)
    assert not result["complete"] and result["exit_code"] == 2
    assert result["reference"]["commit"] is None
    assert result["counts"] is None
    assert result["issues"]["items"]


def test_graph_change_is_not_acknowledged_by_matching_boundary_bytes(repository):
    path = repository / "boundary.config.json"
    config = json.loads(path.read_text())
    config["components"]["svc"]["external_consumers"] = ["external-app"]
    path.write_text(json.dumps(config), encoding="utf-8")
    commit_all(repository)
    result = compare_deployment(repository, "production", "production")
    validate_result(result)
    assert result["reference"]["verified"]
    assert not result["complete"] and result["exit_code"] == 2
    assert any("reconciliation" in issue for issue in result["issues"]["items"]), result["issues"]


def test_head_comparison_ignores_dirty_files_and_held_index_lock(repository):
    contract = repository / "svc" / "contract.txt"
    contract.unlink()
    index_lock = repository / ".git" / "index.lock"
    index_lock.write_bytes(b"another session")
    try:
        result = compare_deployment(repository, "production", "production", source="head")
        validate_result(result)
        assert result["complete"] and result["ok"]
        assert index_lock.read_bytes() == b"another session"
        assert not contract.exists()
    finally:
        index_lock.unlink()


def test_unreconciled_reference_cannot_be_hidden_by_comparison_facets(repository):
    (repository / "svc" / "contract.txt").write_text("unreconciled\n", encoding="utf-8")
    commit_all(repository)
    subprocess.run(["git", "tag", "unreconciled"], cwd=repository, check=True, capture_output=True)
    result = compare_deployment(repository, "production", "unreconciled", facets=["compat"])
    validate_result(result)
    assert not result["reference"]["verified"]
    assert not result["complete"] and result["exit_code"] == 2
    assert any("verified reconciled checkpoint" in item for item in result["issues"]["items"])


def test_unsupported_reference_digest_schema_is_not_relabelled(repository):
    path = repository / "boundary.lock.json"
    lock = json.loads(path.read_text())
    lock["schema"] = "boundary-lock/v3"
    path.write_text(json.dumps(lock), encoding="utf-8")
    commit_all(repository)
    subprocess.run(["git", "tag", "old-contract"], cwd=repository, check=True, capture_output=True)
    before = path.read_bytes()
    result = compare_deployment(repository, "production", "old-contract")
    validate_result(result)
    assert result["reference"]["lock_schema"] == "boundary-lock/v3"
    assert not result["complete"] and result["exit_code"] == 2
    assert "Unsupported reference digest contract" in result["issues"]["items"][0]
    assert path.read_bytes() == before


def test_comparison_success_does_not_replace_separate_current_lock_gate(repository):
    from tests._parity import run_cli
    (repository / "boundary.lock.json").write_text("not a lock", encoding="utf-8")
    commit_all(repository)
    assert run_cli(repository, "verify", "--source", "head", "--quiet").returncode == 2
    result = compare_deployment(repository, "production", "production")
    validate_result(result)
    assert result["complete"] and result["ok"]
    assert result["current_lock_verification"] == "not-performed"


def test_explicit_empty_reference_selector_is_not_defaulted(repository):
    from boundver._utils import ConfigError

    with pytest.raises(ConfigError, match="relative file path"):
        compare_deployment(repository, "production", "production", reference_config_hint="")


def test_issue_expansion_reports_truncation():
    from boundver._deployment import MAX_DEPLOYMENT_ISSUE_CHARS, _issue

    result = {"issues": {"items": [], "omitted": 0, "truncated": False}}
    _issue(result, ["\ud800" * 200])
    assert result["issues"]["truncated"]
    assert len(result["issues"]["items"][0]) <= MAX_DEPLOYMENT_ISSUE_CHARS


@pytest.mark.parametrize("source", ["index", "working-tree"])
def test_explicit_source_compares_changes_without_publishing(repository, source):
    contract = repository / "svc" / "contract.txt"
    lock = repository / "boundary.lock.json"
    original_lock = lock.read_bytes()
    contract.write_text("staged change\n", encoding="utf-8")
    subprocess.run(["git", "add", "svc/contract.txt"], cwd=repository, check=True, capture_output=True)
    staged = subprocess.run(["git", "ls-files", "--stage", "-z"], cwd=repository, check=True, capture_output=True).stdout
    if source == "index":
        contract.write_text("original\n", encoding="utf-8")
    result = compare_deployment(repository, "production", "production", source=source)
    validate_result(result)
    assert result["complete"] and result["exit_code"] == 4, result["issues"]
    assert result["current"]["mode"] == source
    assert (result["current"]["tree"] is None) == (source == "working-tree")
    assert lock.read_bytes() == original_lock
    assert subprocess.run(["git", "ls-files", "--stage", "-z"], cwd=repository, check=True, capture_output=True).stdout == staged


def test_reverted_contract_matches_reference_despite_intervening_change(repository):
    contract = repository / "svc" / "contract.txt"
    contract.write_text("intermediate change\n", encoding="utf-8")
    commit_all(repository)
    contract.write_text("original\n", encoding="utf-8")
    commit_all(repository)
    result = compare_deployment(repository, "production", "production")
    validate_result(result)
    assert result["complete"] and result["ok"], result["issues"]
    assert result["current"]["commit"] != result["reference"]["commit"]
