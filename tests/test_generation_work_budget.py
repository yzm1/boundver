"""Ordinary immutable generation must not scan another source for a warning."""

import copy
import importlib.util
from pathlib import Path

import pytest

from boundver import core
from tests._parity import run_cli, run_cli_in_process


@pytest.fixture(scope="module")
def cli_profile(tmp_path_factory):
    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_generation_cli.py"
    spec = importlib.util.spec_from_file_location("boundver_cli_profile_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path_factory.mktemp("cli-generation")
    module.adoption.create_fixture(root, 6)
    module.add_unowned_files(root, 16)
    return module, root


def test_default_head_has_zero_advisory_disk_reads_and_index_capture(cli_profile):
    module, root = cli_profile
    result = module.measure(root, "default")
    assert result["exit_code"] == 0
    assert result["advisory_calls"] == result["working_tree_reads"] == 0
    assert result["unowned_working_tree_reads"] == 0
    assert result["total"]["git_commands"].get("write-tree", 0) == 0
    assert result["total"]["git_processes"] <= 22


def test_explicit_scan_still_runs_and_never_changes_head_lock(cli_profile):
    module, root = cli_profile
    default = module.measure(root, "default")
    inspected = module.measure(root, "inspect")
    quiet = module.measure(root, "quiet")
    assert inspected["advisory_calls"] == 1
    assert inspected["working_tree_reads"] > 0
    assert inspected["unowned_working_tree_reads"] >= 16
    assert inspected["lock_sha256"] == default["lock_sha256"] == quiet["lock_sha256"]
    assert quiet["advisory_calls"] == 0


def test_default_head_does_not_call_warning_helper_even_with_a_locked_dirty_index(cli_profile, monkeypatch):
    _, root = cli_profile
    changed = root / "components" / "component-00" / "impl-0.txt"
    original = changed.read_bytes()
    lock = root / ".git" / "index.lock"
    lock.write_bytes(b"another session")
    changed.write_text("dirty bytes unrelated to HEAD", encoding="utf-8")
    def forbidden(*args, **kwargs):
        raise AssertionError("HEAD must not inspect another source implicitly")
    monkeypatch.setattr(core, "dirty_component_paths", forbidden)
    try:
        result = run_cli_in_process(root, "generate", "--source", "head", "--dry-run")
        assert result.returncode == 0, result.stderr
        assert "Source: head" in result.stdout
        assert not result.stderr
        assert lock.read_bytes() == b"another session"
    finally:
        changed.write_bytes(original)
        lock.unlink()


def test_explicit_inspection_preserves_bounded_warning_for_locked_index(cli_profile):
    _, root = cli_profile
    lock = root / ".git" / "index.lock"
    lock.write_bytes(b"another session")
    try:
        result = run_cli(root, "generate", "--source", "head", "--dry-run", "--inspect-working-tree")
        assert result.returncode == 0
        assert "Could not inspect uncommitted component changes" in result.stderr
        assert "write-tree" in result.stderr
        assert lock.read_bytes() == b"another session"
    finally:
        lock.unlink()


@pytest.mark.parametrize("source", ["index", "working-tree"])
def test_inspection_is_an_explicit_head_only_diagnostic(cli_profile, source):
    _, root = cli_profile
    before = (root / "boundary.lock.json").read_bytes()
    result = run_cli(root, "generate", "--source", source, "--inspect-working-tree")
    assert result.returncode == 2
    assert "requires --source head" in result.stderr
    assert (root / "boundary.lock.json").read_bytes() == before


def test_quiet_does_not_silently_cancel_explicit_inspection(cli_profile):
    _, root = cli_profile
    changed = root / "components" / "component-00" / "impl-0.txt"
    original = changed.read_bytes()
    changed.write_text("dirty", encoding="utf-8")
    try:
        result = run_cli(root, "generate", "--source", "head", "--dry-run",
                         "--inspect-working-tree", "--quiet")
        assert result.returncode == 0
        assert not result.stdout
        assert "these components have uncommitted changes" in result.stderr
    finally:
        changed.write_bytes(original)


def test_measured_contract_rejects_each_work_or_output_regression(cli_profile):
    module, root = cli_profile
    default, quiet = module.measure(root, "default"), module.measure(root, "quiet")
    result = {"components": 6, "results": {
        "default": {"first": default, "repeated": [default], "median_wall_seconds": 1.0},
        "quiet": {"first": quiet, "repeated": [quiet], "median_wall_seconds": 1.0},
    }}
    assert module.contract_violations(result) == []
    mutations = [
        lambda case: case["first"].update(advisory_calls=1),
        lambda case: case["first"].update(working_tree_reads=1),
        lambda case: case["first"]["total"]["git_commands"].update({"write-tree": 1}),
        lambda case: case["first"]["total"].update(git_processes=99),
        lambda case: case["first"].update(lock_sha256="different"),
        lambda case: case.update(median_wall_seconds=2.0),
        lambda case: case["first"].update(exit_code=2),
    ]
    for mutate in mutations:
        modified = copy.deepcopy(result)
        mutate(modified["results"]["default"])
        assert module.contract_violations(modified)
