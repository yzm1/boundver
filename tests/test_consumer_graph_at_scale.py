"""Release-scale impact checks with declared, independently expected topology."""

import json
import subprocess

import pytest

from boundver._lockfile import dump_lockfile, generate_lockfile
from tests._parity import run_cli_in_process
from tests._repo_fixtures import commit_all, init_git_repo


def _write_config(root, config):
    (root / "boundary.config.json").write_text(json.dumps(config), encoding="utf-8")


def _reconcile(root, config, message):
    _write_config(root, config)
    commit_all(root, message)
    lock = generate_lockfile(config, root, source="head")
    (root / "boundary.lock.json").write_text(dump_lockfile(lock), encoding="utf-8")
    commit_all(root, "record reconciled lock")
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
    ).strip()


def _component(path, consumers=(), terminals=()):
    return {
        "path": path,
        "boundary": {"provider": "path-hash", "paths": ["contract.txt"]},
        "consumers": list(consumers),
        "external_consumers": list(terminals),
    }


def _verify(root, *, transitive):
    args = ["verify", "--source", "head", "--facets", "boundary", "--format", "json"]
    if transitive:
        args.append("--transitive")
    result = run_cli_in_process(root, *args)
    assert result.returncode == 4, (result.stdout, result.stderr)
    return {row["component"]: row for row in json.loads(result.stdout)["consumer_impact"]}


def _review(root, base, target, *, transitive):
    args = ["review", f"{base}..{target}", "--format", "json"]
    if transitive:
        args.append("--transitive")
    result = run_cli_in_process(root, *args)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["complete"]
    return {row["component"]: row for row in payload["consumer_impact"]}


def _assert_bounded_preview(root):
    lock_before = (root / "boundary.lock.json").read_bytes()
    preview = run_cli_in_process(
        root, "generate", "--source", "head", "--dry-run", "--format", "changes-json",
        "--limit", "0",
    )
    assert preview.returncode == 0, preview.stderr
    result = json.loads(preview.stdout)
    assert result["complete"]
    assert result["counts"]["changed"] == 2
    assert result["components"] == []
    assert result["presentation"]["truncated"]
    assert (root / "boundary.lock.json").read_bytes() == lock_before


def _assert_scope_keeps_integrity_gate(root, config):
    config["components"]["service"]["external_consumers"] = ["migration-audit"]
    _write_config(root, config)
    commit_all(root, "change repository declarations without reconciling the lock")
    scoped = run_cli_in_process(
        root, "verify", "--source", "head", "--components", "peer", "--format", "scoped-json",
    )
    assert scoped.returncode != 0, scoped.stdout + scoped.stderr
    result = json.loads(scoped.stdout)
    assert result["selection"]["comparison_complete"]
    assert result["selection"]["fingerprint_drift"] is False
    assert not result["verification"]["ok"]
    assert any(
        issue.startswith("METADATA MISMATCH config_digest")
        for issue in result["repository_issues"]
    )


@pytest.mark.parametrize("count", [100, 250])
def test_declared_dependency_impact_at_scale(tmp_path, count):
    init_git_repo(tmp_path)
    depth = count // 2
    chain = [f"chain-{index:03}" for index in range(depth)]
    leaves = [f"leaf-{index:03}" for index in range(count - depth - 5)]
    components = {
        "schema": _component("system/schema", ["sdk-a", "sdk-b"], ["schema-audit"]),
        "sdk-a": _component("system/sdk-a", ["service"]),
        "sdk-b": _component("system/sdk-b", ["service"]),
        "service": _component("system/service", [chain[0], *leaves]),
        "peer": _component("peer"),
    }
    for index, name in enumerate(chain):
        tail = index == depth - 1
        components[name] = _component(
            f"system/{name}",
            ["sdk-a"] if tail else [chain[index + 1]],
            ["external-terminal"] if tail else [],
        )
    for name in leaves:
        components[name] = _component(f"system/{name}")
    assert len(components) == count

    config = {
        "project": "consumer-scale",
        "components": components,
        "slices": {
            "schema-closure": {"mode": "exact", "closure_of": "schema"},
            "sdk-b-closure": {"mode": "exact", "closure_of": "sdk-b"},
        },
    }
    for component in components.values():
        directory = tmp_path / component["path"]
        directory.mkdir(parents=True)
        (directory / "contract.txt").write_text("original\n", encoding="utf-8")

    base = _reconcile(tmp_path, config, "fine component declarations")
    roots = ("schema", "sdk-b")
    expected = {
        "schema": sorted(["sdk-a", "sdk-b", "service", *chain, *leaves]),
        "sdk-b": sorted(["sdk-a", "service", *chain, *leaves]),
    }
    terminal_sets = {
        "schema": ["external-terminal", "schema-audit"],
        "sdk-b": ["external-terminal"],
    }
    changed_paths = [
        tmp_path / components[name]["path"] / "contract.txt" for name in roots
    ]
    changed_paths[0].write_text("changed\n", encoding="utf-8")
    commit_all(tmp_path, "schema contract changes")
    single = _verify(tmp_path, transitive=True)
    assert list(single) == ["schema"]
    assert single["schema"]["components"] == expected["schema"]

    changed_paths[1].write_text("changed\n", encoding="utf-8")
    commit_all(tmp_path, "an independent SDK contract changes")

    direct = _verify(tmp_path, transitive=False)
    assert sorted(direct) == list(roots)
    assert direct["schema"]["components"] == ["sdk-a", "sdk-b"]
    assert direct["schema"]["external_consumers"] == ["schema-audit"]
    assert direct["sdk-b"]["components"] == ["service"]

    transitive = _verify(tmp_path, transitive=True)
    assert sorted(transitive) == list(roots)
    for root in roots:
        assert transitive[root]["components"] == expected[root]
        assert transitive[root]["external_consumers"] == terminal_sets[root]
        assert transitive[root]["transitive"]

    if count == 250:
        _assert_bounded_preview(tmp_path)

    removed = leaves[-1]
    components["service"]["consumers"].remove(removed)
    target = _reconcile(tmp_path, config, "remove one declared service edge")
    direct_rows = _review(tmp_path, base, target, transitive=False)
    for root in roots:
        assert direct_rows[root]["components"] == [
            {"name": name, "source": "both"} for name in direct[root]["components"]
        ]

    rows = _review(tmp_path, base, target, transitive=True)
    for root in roots:
        assert rows[root]["components"] == [
            {"name": name, "source": "base" if name == removed else "both"}
            for name in expected[root]
        ]
        assert rows[root]["external_consumers"] == [
            {"name": name, "source": "both"} for name in terminal_sets[root]
        ]
        assert {
            (edge["from"], edge["to"], edge["source"])
            for edge in rows[root]["edges"]
            if edge["to"] == removed
        } == {("service", removed, "base")}

    if count == 250:
        _assert_scope_keeps_integrity_gate(tmp_path, config)
        # Reconcile the same source directories with a coarser ownership model,
        # then replay exactly the same two changes.
        coarse = {
            "project": config["project"],
            "components": {"system": _component("system"), "peer": components["peer"]},
        }
        coarse["components"]["system"]["boundary"]["paths"] = ["**/contract.txt"]
        coarse["components"]["system"]["external_consumers"] = ["schema-audit"]
        for path in changed_paths:
            path.write_text("original\n", encoding="utf-8")
        _reconcile(tmp_path, coarse, "coarse component declarations over the same files")
        for path in changed_paths:
            path.write_text("changed\n", encoding="utf-8")
        commit_all(tmp_path, "replay the original contract changes")
        coarse_impact = _verify(tmp_path, transitive=True)
        assert list(coarse_impact) == ["system"]
        assert coarse_impact["system"]["external_consumers"] == ["schema-audit"]
        assert coarse_impact["system"]["components"] == []
