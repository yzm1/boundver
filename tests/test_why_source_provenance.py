"""A lock-containing commit is not evidence of its generating source."""

import json
import subprocess

import pytest

from boundver._lockfile import dump_lockfile, generate_lockfile
from tests._parity import run_cli
from tests._repo_fixtures import commit_all, init_git_repo
from tests._schema_validation import assert_output_schema


@pytest.fixture
def copied_lock(tmp_path):
    init_git_repo(tmp_path)
    (tmp_path / "svc").mkdir()
    contract = tmp_path / "svc" / "contract.txt"
    contract.write_text("original\n", encoding="utf-8")
    config = {"project": "provenance", "components": {"svc": {
        "path": "svc", "boundary": {"provider": "path-hash", "paths": ["contract.txt"]},
    }}}
    (tmp_path / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")
    commit_all(tmp_path)
    subprocess.run(["git", "tag", "explicit-old"], cwd=tmp_path, check=True, capture_output=True)
    lock = generate_lockfile(config, tmp_path)
    # Store the old lock together with newer contract content. The commit that
    # introduces the lock cannot serve as the old contract's source identity.
    (tmp_path / "boundary.lock.json").write_text(dump_lockfile(lock), encoding="utf-8")
    contract.write_text("new contract\n", encoding="utf-8")
    commit_all(tmp_path)
    return tmp_path


def test_default_why_does_not_invent_provenance_from_copied_lock(copied_lock):
    result = run_cli(copied_lock, "why", "svc", "--format", "json")
    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["drifted"]
    assert payload["diagnostic_base"] is None
    assert payload["changed_files_status"] == "not-run"
    assert payload["changed_files"] == []
    assert "unavailable" in payload["diagnostic_base_origin"]


def test_explicit_ref_permits_comparison_without_claiming_inferred_source(copied_lock):
    result = run_cli(copied_lock, "why", "svc", "--base-ref", "explicit-old", "--format", "json")
    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["diagnostic_base_origin"] == "explicit --base-ref"
    assert payload["changed_files_status"] == "ok"
    assert payload["changed_files"] == [{"status": "M", "path": "svc/contract.txt"}]


def test_default_text_explains_missing_source_evidence(copied_lock):
    result = run_cli(copied_lock, "why", "svc")
    assert result.returncode == 1
    assert "Lock source provenance is unavailable" in result.stdout
    assert "Supply --base-ref" in result.stdout


def test_explain_requires_explicit_head_base_instead_of_inferred_history(copied_lock):
    result = run_cli(copied_lock, "explain", "svc")
    assert result.returncode == 2
    assert "Lock source provenance is unavailable" in result.stderr
    assert "--base-ref" in result.stderr
    explicit = run_cli(copied_lock, "explain", "svc", "--base-ref", "explicit-old")
    assert explicit.returncode == 0, explicit.stderr
    assert "svc/contract.txt" in explicit.stdout


def test_reverted_bytes_do_not_become_changed_files_from_intervening_commits(copied_lock):
    contract = copied_lock / "svc" / "contract.txt"
    contract.write_text("another intermediate change\n", encoding="utf-8")
    commit_all(copied_lock)
    contract.write_text("original\n", encoding="utf-8")
    commit_all(copied_lock)
    result = run_cli(copied_lock, "explain", "svc", "--base-ref", "explicit-old")
    assert result.returncode == 0, result.stderr
    assert "No tracked file changes detected" in result.stdout
    assert "Changed files (" not in result.stdout


@pytest.mark.parametrize("source", ["head", "index", "working-tree"])
@pytest.mark.parametrize("previous,current,status", [
    ("1.0.0", "2.0.0", "changed"),
    ("1.0.0", "1.0.0", "unchanged"),
    (None, None, "unavailable"),
    ("1.0.0", None, "unavailable"),
    (None, "1.0.0", "unavailable"),
])
def test_component_why_reports_recorded_and_resolved_versions(tmp_path, source, previous, current, status):
    init_git_repo(tmp_path)
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "api.txt").write_text("contract\n", encoding="utf-8")
    component = {
        "path": "svc", "boundary": {"provider": "path-hash", "paths": ["api.txt"]},
        "version_source": {"constant": previous} if previous is not None else None,
    }
    config = {"project": "versions", "components": {"svc": component}}
    config_path = tmp_path / "boundary.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    commit_all(tmp_path)
    lock = generate_lockfile(config, tmp_path, source="head")
    lock_path = tmp_path / "boundary.lock.json"
    lock_path.write_text(dump_lockfile(lock), encoding="utf-8")
    commit_all(tmp_path)
    component["version_source"] = {"constant": current} if current is not None else None
    config_path.write_text(json.dumps(config), encoding="utf-8")
    if source == "head" and previous != current:
        commit_all(tmp_path)
    elif source == "index":
        subprocess.run(["git", "add", "--", "boundary.config.json"], cwd=tmp_path,
                       check=True, capture_output=True)
    before = lock_path.read_bytes()
    result = run_cli(tmp_path, "why", "svc", "--source", source, "--format", "json")
    assert result.returncode in (0, 1), result.stderr
    payload = json.loads(result.stdout)
    assert_output_schema("cli-output.why.schema.json", payload)
    assert payload["version_change"] == {
        "previous": previous, "current": current, "status": status,
    }
    assert lock_path.read_bytes() == before
    if source == "head":
        assert payload["diagnostic_base"] is None
    if previous == current:
        assert result.returncode == 0


def test_component_why_reports_unavailable_versions_in_text(copied_lock):
    result = run_cli(copied_lock, "why", "svc")
    assert result.returncode == 1
    assert "Versions:   unavailable -> unavailable (unavailable)" in result.stdout
