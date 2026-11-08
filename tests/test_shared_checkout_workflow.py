"""Real merge/index contention, without automatic staging or conflict repair."""

from concurrent.futures import ThreadPoolExecutor
import subprocess

import pytest

from tests._parity import run_cli
from tests._scenarios import Scenario


def _generate_and_commit_lock(scene):
    result = run_cli(scene.root, "generate", "--source", "head", "--quiet")
    assert result.returncode == 0, result.stderr
    scene.git("add", "--", "boundary.lock.json")
    scene.git("commit", "-m", "record branch contract")


@pytest.fixture
def checkout():
    with Scenario("shared-checkout") as scene:
        scene.component(
            "svc", path="svc", provider="path-hash", boundary=["contract.txt"]
        )
        scene.component(
            "client", path="client", provider="path-hash", boundary=["contract.txt"]
        )
        scene.config["components"]["svc"]["consumers"] = ["client"]
        scene.config["slices"] = {
            "app": {"mode": "boundary", "components": ["svc", "client"]}
        }
        scene.file("svc/contract.txt", "original\n")
        scene.file("client/contract.txt", "original client\n")
        scene.commit()
        _generate_and_commit_lock(scene)
        yield scene


@pytest.fixture
def conflicted(checkout):
    scene = checkout
    branch = scene.git("symbolic-ref", "--short", "HEAD")
    scene.git("branch", "other")
    scene.file("svc/contract.txt", "ours\n")
    scene.commit("ours")
    _generate_and_commit_lock(scene)
    scene.git("switch", "other")
    scene.file("svc/contract.txt", "theirs\n")
    scene.file("client/contract.txt", "theirs client\n")
    scene.commit("theirs")
    _generate_and_commit_lock(scene)
    scene.git("switch", branch)
    merge = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", "other"],
        cwd=scene.root,
        capture_output=True,
        text=True,
    )
    assert merge.returncode == 1, merge.stdout + merge.stderr
    paths = scene.git("diff", "--name-only", "--diff-filter=U").splitlines()
    assert set(paths) == {"svc/contract.txt", "boundary.lock.json"}
    return scene


def _stage_reviewed_source_and_placeholder(scene):
    scene.file("svc/contract.txt", "reviewed merged ours and theirs\n")
    scene.git("add", "--", "svc/contract.txt")
    # Deliberate test/operator action, NOT behavior performed by boundver.
    # Both branch versions remain in Git. This generated placeholder is never
    # verified as the merged result or committed before full regeneration.
    scene.git("restore", "--source=HEAD", "--worktree", "--", "boundary.lock.json")
    scene.git("add", "--", "boundary.lock.json")
    assert scene.git("ls-files", "--unmerged") == ""


def test_default_validation_contention_is_the_tracking_snapshot_write_tree(checkout):
    scene = checkout
    index = scene.root / ".git" / "index"
    before = index.read_bytes()
    lock = scene.root / ".git" / "index.lock"
    lock.write_bytes(b"other writer")
    try:
        working = run_cli(scene.root, "validate-config")
        assert working.returncode == 2
        assert "write-tree" in working.stdout + working.stderr
        assert "index.lock" in working.stdout + working.stderr
        head = run_cli(scene.root, "validate-config", "--source", "head")
        assert head.returncode == 0, head.stderr
        assert index.read_bytes() == before and lock.read_bytes() == b"other writer"
    finally:
        lock.unlink()


def test_unresolved_merge_refuses_generation_without_modifying_lock_or_index(
    conflicted,
):
    scene = conflicted
    before = (scene.root / "boundary.lock.json").read_bytes()
    entries = scene.git("ls-files", "--stage", "-z")
    result = run_cli(scene.root, "generate", "--source", "working-tree")
    assert result.returncode == 2
    assert "write-tree" in result.stdout + result.stderr
    assert (scene.root / "boundary.lock.json").read_bytes() == before
    assert scene.git("ls-files", "--stage", "-z") == entries


def test_even_a_lock_only_index_conflict_must_be_resolved_before_relocking(conflicted):
    scene = conflicted
    scene.file("svc/contract.txt", "reviewed merged source\n")
    scene.git("add", "--", "svc/contract.txt")
    assert scene.git("diff", "--name-only", "--diff-filter=U") == "boundary.lock.json"
    before = (scene.root / "boundary.lock.json").read_bytes()
    result = run_cli(scene.root, "generate", "--source", "working-tree")
    assert result.returncode == 2
    assert (scene.root / "boundary.lock.json").read_bytes() == before


def test_complete_reviewed_index_relock_is_verified_before_and_after_merge_commit(
    conflicted,
):
    scene = conflicted
    _stage_reviewed_source_and_placeholder(scene)
    before_index = scene.git("ls-files", "--stage", "-z")
    placeholder = (scene.root / "boundary.lock.json").read_bytes()
    assert run_cli(scene.root, "validate-config", "--source", "index").returncode == 0
    generated = run_cli(scene.root, "generate", "--source", "index", "--quiet")
    assert generated.returncode == 0, generated.stderr
    assert (scene.root / "boundary.lock.json").read_bytes() != placeholder
    assert scene.git("ls-files", "--stage", "-z") == before_index
    # Publication did not stage or silently accept the new lock.
    assert run_cli(scene.root, "verify", "--source", "index", "--quiet").returncode != 0
    scene.git("add", "--", "boundary.lock.json")
    verified = run_cli(scene.root, "verify", "--source", "index", "--quiet")
    assert verified.returncode == 0, verified.stderr
    scene.git("commit", "-m", "merge reviewed source and regenerated contract")
    assert len(scene.git("rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    assert run_cli(scene.root, "verify", "--source", "head", "--quiet").returncode == 0


def test_expired_relock_leaves_placeholder_and_staged_entries_intact(conflicted):
    scene = conflicted
    _stage_reviewed_source_and_placeholder(scene)
    before = (scene.root / "boundary.lock.json").read_bytes()
    entries = scene.git("ls-files", "--stage", "-z")
    result = run_cli(
        scene.root, "generate", "--source", "index", "--timeout", "0.000000001"
    )
    assert result.returncode == 2
    assert any(
        term in (result.stdout + result.stderr).lower()
        for term in ("deadline", "expired", "timeout")
    )
    assert (scene.root / "boundary.lock.json").read_bytes() == before
    assert scene.git("ls-files", "--stage", "-z") == entries


def test_concurrent_head_validation_ignores_merge_stages_and_another_index_writer(
    conflicted,
):
    scene = conflicted
    entries = scene.git("ls-files", "--stage", "-z")
    lock = scene.root / ".git" / "index.lock"
    lock.write_bytes(b"another writer")
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: run_cli(
                        scene.root, "validate-config", "--source", "head"
                    ),
                    range(2),
                )
            )
        assert all(result.returncode == 0 for result in results), results
        assert lock.read_bytes() == b"another writer"
        assert scene.git("ls-files", "--stage", "-z") == entries
    finally:
        lock.unlink()


def test_missing_selection_ref_cannot_relock_or_modify_the_prepared_merge(conflicted):
    scene = conflicted
    _stage_reviewed_source_and_placeholder(scene)
    before = (scene.root / "boundary.lock.json").read_bytes()
    entries = scene.git("ls-files", "--stage", "-z")
    result = run_cli(
        scene.root, "verify", "--source", "index", "--changed-from", "NO-SUCH-REF"
    )
    assert result.returncode == 2
    assert "NO-SUCH-REF" in result.stdout + result.stderr
    assert (scene.root / "boundary.lock.json").read_bytes() == before
    assert scene.git("ls-files", "--stage", "-z") == entries
