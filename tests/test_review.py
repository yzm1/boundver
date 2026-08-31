"""Historical range-review endpoint, facet, and graph contracts."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import boundver._review as review_module
from boundver._lockfile import (
    dump_lockfile,
    generate_lockfile,
    generate_lockfile_for_components,
)
from boundver._review import analyze_review_range
from boundver._utils import GuardrailError, LockfileError
from tests._repo_fixtures import commit_all, init_git_repo


ROOT = Path(__file__).resolve().parents[1]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _component(path: str, **values: object) -> dict:
    return {
        "path": path,
        "boundary": {"provider": "leaf", "paths": []},
        **values,
    }


def _config() -> dict:
    return {
        "project": "review-fixture",
        "components": {
            "layer": _component(
                "layer",
                boundary={"provider": "path-hash", "paths": ["api.json"]},
                behavior={"paths": ["api.json", "behavior.txt"]},
                version_source={"file": "package.json", "field": "version"},
                consumers=["service", "legacy"],
                external_consumers=["old-audit"],
                verify_facets=["exact", "behavior", "boundary", "compat"],
            ),
            "service": _component(
                "service",
                consumers=["app"],
                verify_facets=["exact"],
            ),
            "legacy": _component("legacy", verify_facets=["exact"]),
            "new": _component("new", verify_facets=["exact"]),
            "app": _component("app", verify_facets=["exact"]),
        },
        "slices": {
            "fanout": {"mode": "exact", "closure_of": "layer"},
            "contract": {"mode": "boundary", "components": ["layer"]},
        },
    }


def _write_components(root: Path) -> None:
    for name in ("layer", "service", "legacy", "new", "app"):
        component = root / name
        component.mkdir(parents=True, exist_ok=True)
        (component / "impl.txt").write_text(f"{name} implementation\n", encoding="utf-8")
    (root / "layer" / "api.json").write_text(
        '{"contract": 1}\n', encoding="utf-8"
    )
    (root / "layer" / "behavior.txt").write_text(
        "behavior 1\n", encoding="utf-8"
    )
    (root / "layer" / "package.json").write_text(
        '{"version": "1.0.0"}\n', encoding="utf-8"
    )


def _commit_endpoint(
    root: Path,
    config: dict,
    message: str,
    *,
    existing_lock: dict | None = None,
    selected_components: list[str] | None = None,
) -> tuple[str, dict]:
    (root / "boundary.config.json").write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    _git(root, "add", "--all")
    if selected_components is None:
        lockfile = generate_lockfile(config, root, source="index")
    else:
        assert existing_lock is not None
        lockfile = generate_lockfile_for_components(
            config,
            root,
            selected_components=selected_components,
            out_path=root / "boundary.lock.json",
            source="index",
            existing_lockfile=existing_lock,
            running_version="0.14.1",
        )
    (root / "boundary.lock.json").write_text(
        dump_lockfile(lockfile),
        encoding="utf-8",
    )
    commit_all(root, message)
    return _git(root, "rev-parse", "HEAD"), lockfile


def _make_range(root: Path, *, partial_target: bool = False) -> tuple[str, str]:
    init_git_repo(root, initial_branch="main")
    _write_components(root)
    base_config = _config()
    base, base_lock = _commit_endpoint(root, base_config, "base")

    target_config = copy.deepcopy(base_config)
    target_config["components"]["layer"]["consumers"] = ["service", "new"]
    target_config["components"]["layer"]["external_consumers"] = ["new-audit"]
    (root / "layer" / "impl.txt").write_text(
        "layer implementation 2\n", encoding="utf-8"
    )
    (root / "layer" / "behavior.txt").write_text(
        "behavior 2\n", encoding="utf-8"
    )
    (root / "layer" / "api.json").write_text(
        '{"contract": 2}\n', encoding="utf-8"
    )
    (root / "layer" / "package.json").write_text(
        '{"version": "2.0.0"}\n', encoding="utf-8"
    )
    target, _target_lock = _commit_endpoint(
        root,
        target_config,
        "target",
        existing_lock=base_lock if partial_target else None,
        selected_components=["layer"] if partial_target else None,
    )
    return base, target


def _run_cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "boundver", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )


def test_review_compares_all_facets_after_target_lock_is_reconciled(tmp_path: Path) -> None:
    base, target = _make_range(tmp_path)

    result = analyze_review_range(tmp_path, base, target)

    assert result["schema"] == "boundver-review/v1"
    assert result["complete"] is True
    assert result["endpoints"]["base"]["commit"] == base
    assert result["endpoints"]["target"]["commit"] == target
    layer = next(item for item in result["components"]["changed"] if item["name"] == "layer")
    assert [item["facet"] for item in layer["facets"]] == [
        "exact",
        "behavior",
        "boundary",
        "compat",
    ]
    assert all(item["selected"]["effective"] for item in layer["facets"])
    assert result["summary"]["changed_components"] == 1


def test_review_json_result_matches_the_public_schema(tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    base, target = _make_range(tmp_path)
    result = analyze_review_range(tmp_path, base, target, transitive=True)
    schema = json.loads(
        (ROOT / "spec" / "cli-output.review.schema.json").read_text(
            encoding="utf-8"
        )
    )

    jsonschema.validate(result, schema)


def test_review_uses_conservative_graph_union_with_edge_provenance(tmp_path: Path) -> None:
    base, target = _make_range(tmp_path)

    result = analyze_review_range(tmp_path, base, target, transitive=True)

    impact = result["consumer_impact"][0]
    assert impact["component"] == "layer"
    assert impact["trigger_facets"] == ["boundary", "compat"]
    assert impact["graph_changed"] is True
    assert impact["transitive"] is True
    assert impact["components"] == [
        {"name": "app", "source": "both"},
        {"name": "legacy", "source": "base"},
        {"name": "new", "source": "target"},
        {"name": "service", "source": "both"},
    ]
    edge_index = {
        (edge["from"], edge["to"], edge["kind"]): edge["source"]
        for edge in impact["edges"]
    }
    assert edge_index[("layer", "legacy", "component")] == "base"
    assert edge_index[("layer", "new", "component")] == "target"
    assert edge_index[("layer", "service", "component")] == "both"
    assert edge_index[("service", "app", "component")] == "both"
    assert edge_index[("layer", "old-audit", "external")] == "base"
    assert edge_index[("layer", "new-audit", "external")] == "target"


def test_review_direct_mode_stops_after_immediate_consumers(tmp_path: Path) -> None:
    base, target = _make_range(tmp_path)

    result = analyze_review_range(tmp_path, base, target)

    impact = result["consumer_impact"][0]
    assert impact["transitive"] is False
    assert impact["components"] == [
        {"name": "legacy", "source": "base"},
        {"name": "new", "source": "target"},
        {"name": "service", "source": "both"},
    ]
    assert not any(edge["from"] == "service" for edge in impact["edges"])


def test_review_represents_added_and_removed_components_as_facet_transitions(
    tmp_path: Path,
) -> None:
    base, _target = _make_range(tmp_path)
    config = json.loads(
        (tmp_path / "boundary.config.json").read_text(encoding="utf-8")
    )
    del config["components"]["legacy"]
    config["components"]["newcomer"] = _component(
        "newcomer", verify_facets=["exact"]
    )
    newcomer = tmp_path / "newcomer"
    newcomer.mkdir()
    (newcomer / "impl.txt").write_text("newcomer\n", encoding="utf-8")
    final_target, _ = _commit_endpoint(tmp_path, config, "membership change")

    result = analyze_review_range(tmp_path, base, final_target)

    transitions = {item["name"]: item for item in result["components"]["changed"]}
    assert transitions["legacy"]["status"] == "removed"
    assert transitions["legacy"]["facets"][0]["after"] is None
    assert transitions["newcomer"]["status"] == "added"
    assert transitions["newcomer"]["facets"][0]["before"] is None


def test_review_reports_slice_change_and_consumer_slice_impact(tmp_path: Path) -> None:
    base, target = _make_range(tmp_path)

    result = analyze_review_range(tmp_path, base, target, transitive=True)

    assert {item["name"] for item in result["slices"]["changed"]} == {
        "contract",
        "fanout",
    }
    impacts = {item["name"]: item for item in result["slice_impact"]}
    assert impacts["contract"]["components"] == [
        {"name": "layer", "roles": ["changed"], "source": "both"}
    ]
    fanout = {item["name"]: item for item in impacts["fanout"]["components"]}
    assert fanout["legacy"]["source"] == "base"
    assert fanout["new"]["source"] == "target"
    assert fanout["app"]["roles"] == ["consumer"]


def test_review_accepts_a_safe_component_scoped_lock_update(tmp_path: Path) -> None:
    base, target = _make_range(tmp_path, partial_target=True)

    result = analyze_review_range(tmp_path, base, target)

    assert result["complete"] is True
    assert result["summary"]["changed_components"] == 1


def test_review_fails_closed_when_target_config_and_lock_are_not_reconciled(
    tmp_path: Path,
) -> None:
    base, _target = _make_range(tmp_path)
    config = json.loads((tmp_path / "boundary.config.json").read_text(encoding="utf-8"))
    config["components"]["layer"]["consumers"] = ["service"]
    (tmp_path / "boundary.config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    commit_all(tmp_path, "stale config")
    stale_target = _git(tmp_path, "rev-parse", "HEAD")

    with pytest.raises(LockfileError, match="config and lock are not reconciled"):
        analyze_review_range(tmp_path, base, stale_target)


def test_review_fails_closed_when_endpoint_content_outpaces_its_lock(
    tmp_path: Path,
) -> None:
    base, _target = _make_range(tmp_path)
    (tmp_path / "layer" / "api.json").write_text(
        '{"contract": 999}\n', encoding="utf-8"
    )
    commit_all(tmp_path, "stale endpoint lock")
    stale_target = _git(tmp_path, "rev-parse", "HEAD")

    with pytest.raises(
        LockfileError,
        match="lock is not reconciled to its immutable source tree",
    ):
        analyze_review_range(tmp_path, base, stale_target)


def test_review_merge_base_uses_common_ancestor_not_requested_branch_tip(
    tmp_path: Path,
) -> None:
    base, _ = _make_range(tmp_path)
    _git(tmp_path, "branch", "feature", base)
    (tmp_path / "main-only.txt").write_text("main\n", encoding="utf-8")
    commit_all(tmp_path, "advance main")
    main_tip = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "switch", "feature")
    config = json.loads((tmp_path / "boundary.config.json").read_text(encoding="utf-8"))
    (tmp_path / "layer" / "api.json").write_text(
        '{"contract": 3}\n', encoding="utf-8"
    )
    feature_tip, _ = _commit_endpoint(tmp_path, config, "feature target")

    result = analyze_review_range(
        tmp_path,
        main_tip,
        feature_tip,
        use_merge_base=True,
    )

    assert result["endpoints"]["base"]["requested_commit"] == main_tip
    assert result["endpoints"]["base"]["commit"] == base
    assert result["request"]["merge_base"] is True


def test_review_cli_supports_range_and_explicit_forms(tmp_path: Path) -> None:
    base, target = _make_range(tmp_path)

    positional = _run_cli(
        tmp_path,
        "review",
        f"{base}..{target}",
        "--transitive",
        "--format",
        "json",
    )
    explicit = _run_cli(
        tmp_path,
        "review",
        "--base",
        base,
        "--target",
        target,
        "--facets",
        "boundary,exact",
        "--format",
        "json",
    )

    assert positional.returncode == 0, positional.stderr
    assert explicit.returncode == 0, explicit.stderr
    positional_payload = json.loads(positional.stdout)
    explicit_payload = json.loads(explicit.stdout)
    assert positional_payload["policy"]["impact"] == "transitive"
    assert explicit_payload["policy"]["explicit_facets"] == ["exact", "boundary"]
    assert explicit_payload["policy"]["compared_facets"] == [
        "exact",
        "behavior",
        "boundary",
        "compat",
    ]


def test_review_cli_changes_do_not_change_success_exit_status(tmp_path: Path) -> None:
    base, target = _make_range(tmp_path)

    completed = _run_cli(tmp_path, "review", f"{base}..{target}")

    assert completed.returncode == 0, completed.stderr
    assert "CHANGED COMPONENTS (1)" in completed.stdout
    assert "run verify as the integrity gate" in completed.stdout


@pytest.mark.parametrize(
    "arguments, expected",
    [
        (("HEAD",), "BASE..TARGET"),
        (("HEAD...HEAD",), "exactly BASE..TARGET"),
        (("HEAD..HEAD", "--base", "HEAD", "--target", "HEAD"), "not both"),
        (("--base", "HEAD"), "both --base BASE and --target TARGET"),
    ],
)
def test_review_cli_rejects_ambiguous_endpoint_spelling(
    tmp_path: Path,
    arguments: tuple[str, ...],
    expected: str,
) -> None:
    _make_range(tmp_path)

    completed = _run_cli(tmp_path, "review", *arguments)

    assert completed.returncode == 2
    assert expected in completed.stderr


def test_review_missing_ref_in_shallow_clone_has_exact_fetch_remediation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    base, target = _make_range(source)
    clone = tmp_path / "clone"
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            source.as_uri(),
            str(clone),
        ],
        check=True,
        capture_output=True,
    )

    completed = _run_cli(clone, "review", f"{base}..{target}")

    assert completed.returncode == 2
    assert "Repository is shallow" in completed.stderr
    assert "fetch-depth: 0" in completed.stderr
    assert "GIT_DEPTH: 0" in completed.stderr


def test_review_rejects_absent_ref_in_complete_repository(tmp_path: Path) -> None:
    _base, target = _make_range(tmp_path)

    completed = _run_cli(tmp_path, "review", f"NO-SUCH-REF..{target}")

    assert completed.returncode == 2
    assert "Cannot resolve base Git ref 'NO-SUCH-REF'" in completed.stderr
    assert "Repository is shallow" not in completed.stderr


def test_review_rejects_ambiguous_short_ref(tmp_path: Path) -> None:
    base, target = _make_range(tmp_path)
    _git(tmp_path, "branch", "ambiguous", base)
    _git(tmp_path, "tag", "ambiguous", target)

    completed = _run_cli(tmp_path, "review", "ambiguous..HEAD")

    assert completed.returncode == 2
    assert "unambiguously" in completed.stderr


def test_review_enforces_one_aggregate_graph_and_slice_work_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, target = _make_range(tmp_path)
    monkeypatch.setattr(review_module, "MAX_REVIEW_WORK_STEPS", 10)

    with pytest.raises(GuardrailError, match="No partial review result was emitted"):
        analyze_review_range(tmp_path, base, target, transitive=True)


def test_review_refuses_an_oversized_complete_result_instead_of_truncating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, target = _make_range(tmp_path)
    monkeypatch.setattr(review_module, "MAX_REVIEW_RESULT_BYTES", 512)

    with pytest.raises(GuardrailError, match="No partial review result was emitted"):
        analyze_review_range(tmp_path, base, target, transitive=True)
