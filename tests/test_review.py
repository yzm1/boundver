"""Historical range-review endpoint, facet, and graph contracts."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pytest

import boundver._review as review_module
import boundver._provider_diff as provider_diff_module
from boundver._lockfile import (
    dump_lockfile,
    generate_lockfile,
    generate_lockfile_for_components,
)
from boundver._review import analyze_review_range
from boundver._utils import (
    DIAGNOSTIC_TRUNCATION_SENTINEL,
    GuardrailError,
    LockfileError,
)
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


def _config(provider: str = "openapi-canonical") -> dict:
    return {
        "project": "review-fixture",
        "components": {
            "layer": _component(
                "layer",
                boundary={"provider": provider, "paths": ["api.json"]},
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
        json.dumps(
            {
                "openapi": "3.1.0",
                "paths": {
                    "/orders": {
                        "get": {
                            "parameters": [
                                {
                                    "name": "limit",
                                    "in": "query",
                                    "required": False,
                                    "schema": {"type": "integer"},
                                }
                            ],
                            "responses": {
                                "200": {"description": "ok"},
                                "404": {"description": "missing"},
                            },
                        }
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
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
    config_hint: str = "boundary.config.json",
    lock_hint: str = "boundary.lock.json",
) -> tuple[str, dict]:
    config_path = root / config_hint
    lock_path = root / lock_hint
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
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
            out_path=lock_path,
            source="index",
            existing_lockfile=existing_lock,
            running_version="0.14.1",
        )
    lock_path.write_text(
        dump_lockfile(lockfile),
        encoding="utf-8",
    )
    commit_all(root, message)
    return _git(root, "rev-parse", "HEAD"), lockfile


def _make_range(
    root: Path,
    *,
    partial_target: bool = False,
    provider: str = "openapi-canonical",
    config_hint: str = "boundary.config.json",
    lock_hint: str = "boundary.lock.json",
) -> tuple[str, str]:
    init_git_repo(root, initial_branch="main")
    _write_components(root)
    base_config = _config(provider)
    base, base_lock = _commit_endpoint(
        root,
        base_config,
        "base",
        config_hint=config_hint,
        lock_hint=lock_hint,
    )

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
        json.dumps(
            {
                "openapi": "3.1.0",
                "paths": {
                    "/health": {"get": {"responses": {"204": {}}}},
                    "/orders": {
                        "get": {
                            "parameters": [
                                {
                                    "name": "limit",
                                    "in": "query",
                                    "required": True,
                                    "schema": {"type": "integer"},
                                }
                            ],
                            "responses": {"200": {"description": "ok"}},
                        },
                        "post": {"responses": {"201": {}}},
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
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
        config_hint=config_hint,
        lock_hint=lock_hint,
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


def test_review_reports_source_bound_openapi_structural_changes(
    tmp_path: Path,
) -> None:
    base, target = _make_range(tmp_path)

    result = analyze_review_range(tmp_path, base, target)

    structural = result["structural_changes"]
    assert structural["complete"] is True
    assert structural["truncated"] is False
    assert structural["interface"] == "boundver-structural-diff/v1"
    assert structural["claim"] == "structural-explanation-only"
    assert len(structural["reports"]) == 1
    report = structural["reports"][0]
    assert report["component"] == "layer"
    assert report["status"] == "complete"
    assert report["complete"] is True
    assert report["reason"] is None
    assert report["inputs"]["base"]["endpoint"] == "base"
    assert report["inputs"]["base"]["commit"] == base
    assert report["inputs"]["base"]["present"] is True
    assert report["inputs"]["base"]["provider"] == "openapi-canonical"
    assert report["inputs"]["base"]["provider_version"] == "4"
    assert report["inputs"]["target"]["endpoint"] == "target"
    assert report["inputs"]["target"]["commit"] == target
    assert report["inputs"]["target"]["present"] is True
    assert report["inputs"]["target"]["provider"] == "openapi-canonical"
    assert report["inputs"]["target"]["provider_version"] == "4"
    assert report["inputs"]["base"]["boundary_digest"] != report["inputs"][
        "target"
    ]["boundary_digest"]
    document = report["documents"][0]
    assert document["label"] == "canonical:api.json"
    assert document["status"] == "changed"
    assert document["changes"] == [
        {
            "kind": "added",
            "path": "/paths/~1health",
            "before_type": None,
            "after_type": "object",
        },
        {
            "kind": "changed",
            "path": "/paths/~1orders/get/parameters/0/required",
            "before_type": "boolean",
            "after_type": "boolean",
        },
        {
            "kind": "removed",
            "path": "/paths/~1orders/get/responses/404",
            "before_type": "object",
            "after_type": None,
        },
        {
            "kind": "added",
            "path": "/paths/~1orders/post",
            "before_type": None,
            "after_type": "object",
        },
    ]
    assert report["summary"] == {"added": 2, "removed": 1, "changed": 1}


def test_review_marks_raw_provider_structure_as_unavailable(tmp_path: Path) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    base, target = _make_range(tmp_path, provider="path-hash")

    result = analyze_review_range(tmp_path, base, target)

    assert result["complete"] is True
    assert result["structural_changes"]["complete"] is False
    report = result["structural_changes"]["reports"][0]
    assert report["status"] == "unavailable"
    assert report["reason"] == "provider-unsupported"
    assert report["truncated"] is False
    assert report["documents"] == []
    assert report["summary"] == {"added": 0, "removed": 0, "changed": 0}
    schema = json.loads(
        (ROOT / "spec" / "cli-output.review.schema.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(result, schema)


def test_review_with_no_boundary_transition_needs_no_structural_report(
    tmp_path: Path,
) -> None:
    _base, target = _make_range(tmp_path)
    (tmp_path / "app" / "impl.txt").write_text(
        "app implementation 2\n",
        encoding="utf-8",
    )
    config = json.loads((tmp_path / "boundary.config.json").read_text(encoding="utf-8"))
    final_target, _ = _commit_endpoint(tmp_path, config, "implementation only")

    result = analyze_review_range(tmp_path, target, final_target)

    assert result["summary"]["changed_components"] == 1
    assert result["components"]["changed"][0]["name"] == "app"
    assert result["structural_changes"] == {
        "complete": True,
        "truncated": False,
        "interface": "boundver-structural-diff/v1",
        "claim": "structural-explanation-only",
        "reports": [],
    }


def test_review_reports_structural_limit_without_partial_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, target = _make_range(tmp_path)
    monkeypatch.setattr(provider_diff_module, "MAX_PROVIDER_DIFF_ROWS", 1)

    result = analyze_review_range(tmp_path, base, target)

    assert result["complete"] is True
    structural = result["structural_changes"]
    assert structural["complete"] is False
    assert structural["truncated"] is True
    report = structural["reports"][0]
    assert report["reason"] == "limit-exceeded"
    assert report["truncated"] is True
    assert report["documents"] == []
    assert "No partial structural result was emitted" in report["detail"]


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
    base, reconciled_target = _make_range(tmp_path)
    (tmp_path / "layer" / "api.json").write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "paths": {"/stale": {"get": {"responses": {"200": {}}}}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "service" / "impl.txt").write_text(
        "service implementation 2\n",
        encoding="utf-8",
    )
    (tmp_path / "legacy" / "impl.txt").write_text(
        "legacy implementation 2\n",
        encoding="utf-8",
    )
    commit_all(tmp_path, "stale endpoint lock")
    stale_target = _git(tmp_path, "rev-parse", "HEAD")

    with pytest.raises(LockfileError) as captured:
        analyze_review_range(tmp_path, base, stale_target)

    message = str(captured.value)
    assert "Range review compares reconciled endpoint commits" in message
    assert f"target commit {stale_target}" in message
    assert "unreconciled drift in 3 components" in message
    assert (
        "Reconciled checkpoint found by bounded first-parent search: "
        f"{reconciled_target} "
        "(1 commit back)."
    ) in message
    assert "Reconcile and commit that endpoint's lock before review" in message
    assert "Observed endpoint drift:" in message


def test_review_reports_reconciled_checkpoint_distance_across_stale_commits(
    tmp_path: Path,
) -> None:
    base, reconciled_target = _make_range(tmp_path)
    (tmp_path / "service" / "impl.txt").write_text(
        "service implementation 2\n",
        encoding="utf-8",
    )
    commit_all(tmp_path, "introduce unreconciled drift")
    _git(tmp_path, "commit", "--allow-empty", "-m", "unrelated one")
    _git(tmp_path, "commit", "--allow-empty", "-m", "unrelated two")
    stale_target = _git(tmp_path, "rev-parse", "HEAD")

    with pytest.raises(LockfileError) as captured:
        analyze_review_range(tmp_path, base, stale_target)

    assert (
        "Reconciled checkpoint found by bounded first-parent search: "
        f"{reconciled_target} "
        "(3 commits back)."
    ) in str(captured.value)


def test_review_bounds_reconciled_checkpoint_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, _target = _make_range(tmp_path)
    (tmp_path / "service" / "impl.txt").write_text(
        "service implementation 2\n",
        encoding="utf-8",
    )
    commit_all(tmp_path, "introduce unreconciled drift")
    _git(tmp_path, "commit", "--allow-empty", "-m", "still stale")
    stale_target = _git(tmp_path, "rev-parse", "HEAD")
    monkeypatch.setattr(review_module, "MAX_REVIEW_RECONCILIATION_CANDIDATES", 1)

    with pytest.raises(LockfileError) as captured:
        analyze_review_range(tmp_path, base, stale_target)

    assert (
        "No reconciled checkpoint was found among the nearest 1 first-parent "
        "commit."
        in str(captured.value)
    )


def test_review_caps_nearest_first_parent_checkpoint_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ancestors = [f"{index:040x}" for index in range(1, 11)]
    monkeypatch.setattr(review_module, "MAX_REVIEW_RECONCILIATION_CANDIDATES", 3)

    candidates, truncated = review_module._reconciliation_candidates(ancestors)

    assert candidates == [
        (ancestors[0], 1),
        (ancestors[1], 2),
        (ancestors[2], 3),
    ]
    assert truncated is True


def test_review_finds_a_source_only_reconciled_checkpoint(tmp_path: Path) -> None:
    base, _target = _make_range(tmp_path)
    implementation = tmp_path / "service" / "impl.txt"
    reconciled_content = implementation.read_text(encoding="utf-8")

    implementation.write_text("stale once\n", encoding="utf-8")
    commit_all(tmp_path, "source drift")
    implementation.write_text(reconciled_content, encoding="utf-8")
    commit_all(tmp_path, "source-only reconciliation")
    reconciled_source_only = _git(tmp_path, "rev-parse", "HEAD")
    implementation.write_text("stale again\n", encoding="utf-8")
    commit_all(tmp_path, "new source drift")
    stale_target = _git(tmp_path, "rev-parse", "HEAD")

    with pytest.raises(LockfileError) as captured:
        analyze_review_range(tmp_path, base, stale_target)

    assert (
        "Reconciled checkpoint found by bounded first-parent search: "
        f"{reconciled_source_only} (1 commit back)."
    ) in str(captured.value)


def test_review_checkpoint_search_does_not_filter_on_endpoint_pathspecs(
    tmp_path: Path,
) -> None:
    config_hint = "control/boundary[config].json"
    lock_hint = "control/boundary[lock].json"
    base, reconciled_target = _make_range(
        tmp_path,
        config_hint=config_hint,
        lock_hint=lock_hint,
    )
    (tmp_path / "service" / "impl.txt").write_text(
        "unreconciled\n",
        encoding="utf-8",
    )
    commit_all(tmp_path, "source drift")
    stale_target = _git(tmp_path, "rev-parse", "HEAD")

    with pytest.raises(LockfileError) as captured:
        analyze_review_range(
            tmp_path,
            base,
            stale_target,
            config_hint=config_hint,
            lock_hint=lock_hint,
        )

    assert (
        "Reconciled checkpoint found by bounded first-parent search: "
        f"{reconciled_target} (1 commit back)."
    ) in str(captured.value)


def test_review_labels_a_truncated_component_count_as_a_lower_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, target = _make_range(tmp_path)

    def truncated_verify(*_args: object, **kwargs: object) -> list[str]:
        drifted_components = kwargs["drifted_components"]
        observations = kwargs["observations"]
        assert isinstance(drifted_components, set)
        assert isinstance(observations, list)
        drifted_components.add("layer")
        observations.append(DIAGNOSTIC_TRUNCATION_SENTINEL)
        return []

    monkeypatch.setattr(review_module, "verify_lockfile", truncated_verify)

    with pytest.raises(LockfileError) as captured:
        analyze_review_range(
            tmp_path,
            base,
            target,
            allow_custom_providers=True,
        )

    assert "unreconciled drift in at least 1 component" in str(captured.value)


def test_review_checkpoint_hint_failure_does_not_hide_endpoint_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, _target = _make_range(tmp_path)
    (tmp_path / "service" / "impl.txt").write_text(
        "service implementation 2\n",
        encoding="utf-8",
    )
    commit_all(tmp_path, "unreconciled drift")
    stale_target = _git(tmp_path, "rev-parse", "HEAD")

    def fail_checkpoint_search(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("auxiliary failure")

    monkeypatch.setattr(
        review_module,
        "_reconciled_checkpoint_hint",
        fail_checkpoint_search,
    )

    with pytest.raises(LockfileError) as captured:
        analyze_review_range(tmp_path, base, stale_target)

    message = str(captured.value)
    assert f"target commit {stale_target} has unreconciled drift" in message
    assert "Checkpoint search was unavailable" in message
    assert "auxiliary failure" not in message


def test_review_skips_checkpoint_search_with_custom_providers(tmp_path: Path) -> None:
    hint = review_module._reconciled_checkpoint_hint(
        tmp_path,
        "f" * 40,
        config_hint="boundary.config.json",
        lock_hint="boundary.lock.json",
        has_custom_providers=True,
    )

    assert "skipped because the endpoint declares custom providers" in hint


def test_review_does_not_skip_checkpoint_search_for_unused_custom_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_module, "_first_parent_ancestors", lambda *_args: [])

    hint = review_module._reconciled_checkpoint_hint(
        tmp_path,
        "f" * 40,
        config_hint="boundary.config.json",
        lock_hint="boundary.lock.json",
        has_custom_providers=False,
    )

    assert "custom providers" not in hint
    assert "0 available first-parent commits" in hint


def test_review_stops_checkpoint_search_after_a_candidate_guardrail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ancestors = [f"{index:040x}" for index in range(1, 4)]
    attempted: list[str] = []
    monkeypatch.setattr(
        review_module,
        "_first_parent_ancestors",
        lambda *_args: ancestors,
    )
    monkeypatch.setattr(
        review_module,
        "_capture_git_ref_snapshot",
        lambda _root, candidate, **_kwargs: candidate,
    )

    def exceed_guardrail(
        _root: Path,
        candidate: object,
        **_kwargs: object,
    ) -> None:
        attempted.append(str(candidate))
        raise GuardrailError("candidate work limit")

    monkeypatch.setattr(review_module, "_load_review_endpoint", exceed_guardrail)

    hint = review_module._reconciled_checkpoint_hint(
        tmp_path,
        "f" * 40,
        config_hint="boundary.config.json",
        lock_hint="boundary.lock.json",
        has_custom_providers=False,
    )

    assert attempted == [ancestors[0]]
    assert "stopped after a candidate exceeded a safety guardrail" in hint


def test_verify_guidance_does_not_refer_ambiguously_to_review(tmp_path: Path) -> None:
    _base, _target = _make_range(tmp_path)
    (tmp_path / "service" / "impl.txt").write_text(
        "service implementation 2\n",
        encoding="utf-8",
    )
    commit_all(tmp_path, "unreconciled drift")

    completed = _run_cli(tmp_path, "verify", "--source", "head")

    assert completed.returncode != 0
    assert "after review" not in completed.stdout
    assert "If the drift is intentional" in completed.stdout
    assert "reconcile this source snapshot" in completed.stdout


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
        json.dumps(
            {
                "openapi": "3.1.0",
                "paths": {"/feature": {"get": {"responses": {"200": {}}}}},
            }
        )
        + "\n",
        encoding="utf-8",
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
    structural_base = result["structural_changes"]["reports"][0]["inputs"]["base"]
    assert structural_base["requested_ref"] == main_tip
    assert structural_base["requested_commit"] == main_tip
    assert structural_base["commit"] == base


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
    assert "Structural explanation: complete" in completed.stdout
    assert "/paths/~1orders/post" in completed.stdout
    assert "not a compatibility verdict" in completed.stdout
    assert "run verify as the integrity gate" in completed.stdout


def test_review_text_escapes_control_characters_in_structural_paths(
    tmp_path: Path,
) -> None:
    _original, base = _make_range(tmp_path)
    config = json.loads((tmp_path / "boundary.config.json").read_text(encoding="utf-8"))
    api_path = tmp_path / "layer" / "api.json"
    api = json.loads(api_path.read_text(encoding="utf-8"))
    api["paths"]["/\x1b[31madmin"] = {"get": {"responses": {"200": {}}}}
    api_path.write_text(json.dumps(api) + "\n", encoding="utf-8")
    target, _ = _commit_endpoint(tmp_path, config, "control-looking path")

    completed = _run_cli(tmp_path, "review", f"{base}..{target}")

    assert completed.returncode == 0, completed.stderr
    assert "\x1b" not in completed.stdout
    assert "\\x1b[31madmin" in completed.stdout


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


def test_review_work_budget_rejects_wide_union_before_allocating_it() -> None:
    base = {"slices": {f"a{index:06d}": {} for index in range(100_000)}}
    target = {"slices": {f"b{index:06d}": {} for index in range(100_000)}}
    budget = review_module._ReviewWorkBudget()
    budget.steps = review_module.MAX_REVIEW_WORK_STEPS

    tracemalloc.start()
    try:
        with pytest.raises(GuardrailError, match="aggregate limit"):
            review_module._slice_transitions(base, target, budget=budget)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 2 * 1024 * 1024


def test_review_refuses_an_oversized_complete_result_instead_of_truncating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, target = _make_range(tmp_path)
    monkeypatch.setattr(review_module, "MAX_REVIEW_RESULT_BYTES", 512)

    with pytest.raises(GuardrailError, match="No partial review result was emitted"):
        analyze_review_range(tmp_path, base, target, transitive=True)
