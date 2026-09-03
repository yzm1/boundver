"""End-to-end fixtures for maintained CI range-review integrations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_action_outputs import _parse_github_output
from tests.test_review import _commit_endpoint, _make_range


ROOT = Path(__file__).resolve().parents[1]


def _bash() -> str:
    candidate = shutil.which("bash")
    if os.name == "nt":
        git = shutil.which("git")
        git_bash = (
            Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if git is not None
            else None
        )
        if git_bash is not None and git_bash.is_file():
            candidate = str(git_bash)
    if candidate is None:  # pragma: no cover - all maintained runners have Bash
        pytest.skip("Bash is required for the maintained CI integrations")
    return candidate


def _action_script() -> str:
    action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
    return next(
        step["run"]
        for step in action["runs"]["steps"]
        if step.get("id") == "verify"
    )


def _gitlab_script() -> str:
    documents = list(
        yaml.safe_load_all(
            (ROOT / "templates" / "boundver.yml").read_text(encoding="utf-8")
        )
    )
    return next(iter(documents[1].values()))["script"][0]


def _review_environment(root: Path, base: str, target: str, *, transitive: bool) -> dict:
    head = ""
    if (root / ".git").exists():
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode == 0:
            head = completed.stdout.strip()
    return {
        **os.environ,
        "BOUNDVER_OPERATION": "review",
        "BOUNDVER_CONFIG": "boundary.config.json",
        "BOUNDVER_LOCK": "boundary.lock.json",
        "BOUNDVER_BASELINE": "",
        "BOUNDVER_SOURCE": "head",
        "BOUNDVER_FACETS": "",
        "BOUNDVER_COMPONENTS": "",
        "BOUNDVER_CHANGED_FROM": "",
        "BOUNDVER_BASE": base,
        "BOUNDVER_TARGET": target,
        "BOUNDVER_MERGE_BASE": "false",
        "BOUNDVER_TRANSITIVE": "true" if transitive else "false",
        "BOUNDVER_FAIL_FAST": "false",
        "BOUNDVER_UPDATE": "false",
        "BOUNDVER_UPLOAD_ARTIFACT": "false",
        "BOUNDVER_ARTIFACT_NAME": "boundver-review-plan",
        "CI_PROJECT_DIR": str(root),
        "GITHUB_SHA": head,
    }


def _make_wide_summary_range(root: Path) -> tuple[str, str]:
    base, _target = _make_range(root)
    config = json.loads(
        (root / "boundary.config.json").read_text(encoding="utf-8")
    )
    config["components"]["layer"]["external_consumers"] = [
        f"external-{index:03d}" for index in range(60)
    ]
    target, _lock = _commit_endpoint(root, config, "wide review presentation")
    return base, target


def _make_shallow_range(root: Path) -> tuple[Path, str, str]:
    source = root / "source"
    source.mkdir()
    base, target = _make_range(source)
    clone = root / "clone"
    subprocess.run(
        ["git", "clone", "--depth", "1", source.as_uri(), str(clone)],
        check=True,
        capture_output=True,
        text=True,
    )
    return clone, base, target


@pytest.mark.parametrize(
    ("mode", "transitive", "partial_target", "expected_impacted"),
    (
        ("clean", False, False, []),
        ("direct", False, False, ["legacy", "new", "service"]),
        ("transitive", True, False, ["app", "legacy", "new", "service"]),
        ("reconciled-partial", False, True, ["legacy", "new", "service"]),
    ),
)
def test_composite_action_emits_exact_complete_plan_and_summary(
    tmp_path: Path,
    mode: str,
    transitive: bool,
    partial_target: bool,
    expected_impacted: list[str],
) -> None:
    base, target = _make_range(tmp_path, partial_target=partial_target)
    if mode == "clean":
        target = base
    output = tmp_path / "github-output"
    step_summary = tmp_path / "github-summary.md"
    environment = _review_environment(
        tmp_path,
        base,
        target,
        transitive=transitive,
    )
    environment.update(
        {
            "BOUNDVER_ACTION_PATH": str(ROOT),
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(step_summary),
        }
    )

    completed = subprocess.run(
        [_bash(), "-c", _action_script()],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    values = _parse_github_output(output)
    assert values["result-schema"] == "boundver-plan/v1"
    assert values["transport-complete"] == "true"
    assert values["selection-complete"] == "true"
    assert json.loads(values["impacted-components"]) == expected_impacted
    plan = json.loads(Path(values["result-file"]).read_text(encoding="utf-8"))
    assert plan["endpoints"]["base"]["commit"] == base
    assert plan["endpoints"]["target"]["commit"] == target
    assert plan["selection"]["impacted_components"] == expected_impacted
    summary = Path(values["summary-file"]).read_text(encoding="utf-8")
    assert summary == step_summary.read_text(encoding="utf-8")
    assert "Presentation complete" in summary
    if mode == "clean":
        assert plan["selection"]["changed_components"] == []
        assert "::notice file=" not in completed.stderr
    else:
        assert plan["consumer_impact"][0]["graph_changed"] is True
        assert "::notice file=layer/api.json" in completed.stderr


@pytest.mark.parametrize(
    ("transitive", "partial_target", "expected_impacted"),
    (
        (False, False, ["legacy", "new", "service"]),
        (True, False, ["app", "legacy", "new", "service"]),
        (False, True, ["legacy", "new", "service"]),
    ),
)
def test_gitlab_component_retains_same_plan_and_summary_artifacts(
    tmp_path: Path,
    transitive: bool,
    partial_target: bool,
    expected_impacted: list[str],
) -> None:
    base, target = _make_range(tmp_path, partial_target=partial_target)
    environment = _review_environment(
        tmp_path,
        base,
        target,
        transitive=transitive,
    )

    completed = subprocess.run(
        [_bash(), "-c", _gitlab_script()],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    plan_path = tmp_path / "boundver-result.json"
    summary_path = tmp_path / "boundver-summary.md"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["schema"] == "boundver-plan/v1"
    assert plan["endpoints"]["base"]["commit"] == base
    assert plan["endpoints"]["target"]["commit"] == target
    assert plan["selection"]["impacted_components"] == expected_impacted
    assert plan["consumer_impact"][0]["graph_changed"] is True
    summary = summary_path.read_text(encoding="utf-8")
    assert "Routing evidence only" in summary
    assert "Presentation complete" in summary
    assert summary in completed.stdout


@pytest.mark.parametrize("integration", ("action", "gitlab"))
def test_ci_review_integrations_preserve_complete_plan_when_summary_is_truncated(
    tmp_path: Path,
    integration: str,
) -> None:
    base, target = _make_wide_summary_range(tmp_path)
    environment = _review_environment(tmp_path, base, target, transitive=False)
    if integration == "action":
        output = tmp_path / "github-output"
        environment.update(
            {
                "BOUNDVER_ACTION_PATH": str(ROOT),
                "RUNNER_TEMP": str(tmp_path),
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(tmp_path / "github-summary.md"),
            }
        )
        script = _action_script()
    else:
        output = None
        script = _gitlab_script()

    completed = subprocess.run(
        [_bash(), "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    if output is not None:
        values = _parse_github_output(output)
        assert values["transport-complete"] == "true"
        assert values["selection-complete"] == "true"
        plan_path = Path(values["result-file"])
        summary_path = Path(values["summary-file"])
    else:
        plan_path = tmp_path / "boundver-result.json"
        summary_path = tmp_path / "boundver-summary.md"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert len(plan["selection"]["external_consumers"]) == 61
    assert plan["complete"] is True
    summary = summary_path.read_text(encoding="utf-8")
    assert "Presentation truncated:" in summary
    assert "complete boundver-plan/v1 result remains" in summary
    assert "Presentation complete" not in summary


@pytest.mark.parametrize("integration", ("action", "gitlab"))
def test_ci_review_integrations_fail_closed_with_exact_shallow_history_remediation(
    tmp_path: Path,
    integration: str,
) -> None:
    clone, base, target = _make_shallow_range(tmp_path)
    environment = _review_environment(clone, base, target, transitive=False)
    if integration == "action":
        output = clone / "github-output"
        environment.update(
            {
                "BOUNDVER_ACTION_PATH": str(ROOT),
                "RUNNER_TEMP": str(clone),
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(clone / "github-summary.md"),
            }
        )
        script = _action_script()
    else:
        output = None
        script = _gitlab_script()

    completed = subprocess.run(
        [_bash(), "-c", script],
        cwd=clone,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "Repository is shallow" in completed.stderr
    if integration == "action":
        assert "GitHub Actions: fetch-depth: 0" in completed.stderr
        values = _parse_github_output(output)
        assert values["exit-code"] == "2"
        assert values["transport-complete"] == "false"
        assert Path(values["result-file"]).is_file()
        assert "Review incomplete" in Path(values["summary-file"]).read_text(
            encoding="utf-8"
        )
    else:
        assert "GitLab: GIT_DEPTH: 0" in completed.stderr
        assert (clone / "boundver-result.json").is_file()
        assert "Review incomplete" in (
            clone / "boundver-summary.md"
        ).read_text(encoding="utf-8")


def test_gitlab_component_retains_diagnostics_for_preflight_failure(
    tmp_path: Path,
) -> None:
    environment = _review_environment(tmp_path, "", "HEAD", transitive=False)

    completed = subprocess.run(
        [_bash(), "-c", _gitlab_script()],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "operation review requires both base and target" in completed.stderr
    diagnostic = json.loads(
        (tmp_path / "boundver-result.json").read_text(encoding="utf-8")
    )
    assert diagnostic == {
        "ok": False,
        "ci_transport": {"complete": False, "reason": "command-incomplete"},
    }
    assert "No complete machine result was emitted" in (
        tmp_path / "boundver-summary.md"
    ).read_text(encoding="utf-8")


def test_gitlab_component_refuses_preexisting_artifact_paths(
    tmp_path: Path,
) -> None:
    result = tmp_path / "boundver-result.json"
    result.write_text("do not overwrite\n", encoding="utf-8")
    environment = _review_environment(tmp_path, "", "HEAD", transitive=False)

    completed = subprocess.run(
        [_bash(), "-c", _gitlab_script()],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "refusing unsafe or pre-existing boundver output" in completed.stderr
    assert result.read_text(encoding="utf-8") == "do not overwrite\n"
    assert 'getattr(os, "O_NOFOLLOW", 0)' in _gitlab_script()
