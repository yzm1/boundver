"""A lock-containing commit is not evidence of its generating source."""

import json
import subprocess

import pytest

from boundver._lockfile import dump_lockfile, generate_lockfile
from tests._parity import run_cli
from tests._repo_fixtures import commit_all, init_git_repo


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
