"""Snapshot validation must not depend on another session's index or disk."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess

import pytest

from boundver import _git
from tests._parity import run_cli, run_cli_in_process
from tests._repo_fixtures import commit_all, init_git_repo


def _repository(root: Path) -> Path:
    init_git_repo(root)
    (root / "api").mkdir()
    (root / "api" / "contract.json").write_text("{}", encoding="utf-8")
    (root / "api" / "runtime.txt").write_text("runtime", encoding="utf-8")
    config = root / "boundary.config.json"
    config.write_text(json.dumps({
        "project": "validation-source",
        "components": {"api": {
            "path": "api",
            "boundary": {"provider": "path-hash", "paths": ["*.json"]},
            "behavior": {"paths": ["runtime.txt", "*.json"]},
        }},
    }), encoding="utf-8")
    commit_all(root)
    return config


@pytest.mark.parametrize("command", ["validate-config", "check-config"])
def test_head_validation_ignores_dirty_config_and_held_index_lock(tmp_path, command):
    config = _repository(tmp_path)
    clean = run_cli(tmp_path, command, "--source", "head")
    assert clean.returncode == 0, clean.stderr
    config.write_text("invalid disk JSON", encoding="utf-8")
    (tmp_path / "api" / "contract.json").unlink()
    index_before = (tmp_path / ".git" / "index").read_bytes()
    lock = tmp_path / ".git" / "index.lock"
    lock.write_bytes(b"other session owns this")
    try:
        dirty = run_cli(tmp_path, command, "--source", "head")
        assert dirty.returncode == 0, dirty.stderr
        assert dirty.stdout == clean.stdout
        assert lock.read_bytes() == b"other session owns this"
        assert (tmp_path / ".git" / "index").read_bytes() == index_before
    finally:
        lock.unlink()


def test_head_validation_never_invokes_write_tree(tmp_path, monkeypatch):
    _repository(tmp_path)
    original = _git._git_run
    commands = []

    def checked(root, args, *positional, **keywords):
        commands.append(args[0])
        assert args[0] != "write-tree"
        return original(root, args, *positional, **keywords)

    monkeypatch.setattr(_git, "_git_run", checked)
    result = run_cli_in_process(tmp_path, "validate-config", "--source", "head")
    assert result.returncode == 0, result.stderr
    assert commands


def test_index_validation_reads_staged_config_not_disk(tmp_path):
    config = _repository(tmp_path)
    config.write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "boundary.config.json"], cwd=tmp_path,
                   check=True, capture_output=True)
    config.write_text("invalid disk JSON", encoding="utf-8")
    result = run_cli(tmp_path, "validate-config", "--source", "index")
    assert result.returncode == 2
    assert "components" in result.stdout
    assert "JSON" not in result.stderr


def test_default_validation_still_reads_working_tree(tmp_path):
    config = _repository(tmp_path)
    config.write_text("invalid disk JSON", encoding="utf-8")
    result = run_cli(tmp_path, "validate-config")
    assert result.returncode == 2
    assert run_cli(tmp_path, "validate-config", "--source", "head").returncode == 0


def test_concurrent_head_validation_does_not_contend_on_index(tmp_path):
    _repository(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: run_cli(tmp_path, "validate-config", "--source", "head"),
            range(2),
        ))
    assert all(result.returncode == 0 for result in results), results


def test_head_validation_requires_a_committed_config(tmp_path):
    _repository(tmp_path)
    (tmp_path / "other.json").write_text("{}", encoding="utf-8")
    result = run_cli(tmp_path, "validate-config", "--source", "head",
                     "--config", "other.json")
    assert result.returncode == 2


def test_selected_index_contention_is_not_silently_replaced_with_head(tmp_path):
    _repository(tmp_path)
    lock = tmp_path / ".git" / "index.lock"
    lock.write_bytes(b"other session")
    try:
        result = run_cli(tmp_path, "validate-config", "--source", "index")
        assert result.returncode == 2
        assert "write-tree" in result.stderr
        assert "Config is valid" not in result.stdout
    finally:
        lock.unlink()
