"""Contracts for the base-controlled, merge-critical CI status."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_required_ci_results.py"
HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
RUN_ID = 12_345
RUN_ATTEMPT = 2
PR_NUMBER = 95


def _load_script():
    spec = importlib.util.spec_from_file_location("check_required_ci_results", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - test invariant
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _repo(gate, *, repo_id: int | None = None, name: str = "boundver") -> dict:
    owner = "yzm1" if repo_id in (None, gate.REPOSITORY_ID) else "contributor"
    return {
        "id": gate.REPOSITORY_ID if repo_id is None else repo_id,
        "name": name,
        "full_name": f"{owner}/{name}",
        "url": f"https://api.github.com/repos/{owner}/{name}",
        "html_url": f"https://github.com/{owner}/{name}",
    }


def _event(gate, *, head_repo: dict | None = None) -> dict:
    head_repo = _repo(gate) if head_repo is None else head_repo
    return {
        "action": "completed",
        "repository": _repo(gate),
        "workflow_run": {
            "id": RUN_ID,
            "run_attempt": RUN_ATTEMPT,
            "name": gate.SOURCE_WORKFLOW_NAME,
            "path": gate.SOURCE_WORKFLOW_PATH,
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "head_sha": HEAD_SHA,
            "html_url": f"https://github.com/{gate.REPOSITORY}/actions/runs/{RUN_ID}",
            "pull_requests": [
                {
                    "number": PR_NUMBER,
                    "head": {
                        "sha": HEAD_SHA,
                        "ref": "feature",
                        "repo": head_repo,
                    },
                    "base": {
                        "sha": BASE_SHA,
                        "ref": gate.BASE_BRANCH,
                        "repo": _repo(gate),
                    },
                }
            ],
        },
    }


def _jobs(gate, *, conclusion: str = "success") -> dict:
    return {
        "total_count": len(gate.EXPECTED_JOBS),
        "jobs": [
            {
                "name": name,
                "run_id": RUN_ID,
                "run_attempt": RUN_ATTEMPT,
                "status": "completed",
                "conclusion": conclusion,
            }
            for name in gate.EXPECTED_JOBS
        ],
    }


def _pull(gate, *, head_repo: dict | None = None, changed_files: int = 1) -> dict:
    head_repo = _repo(gate) if head_repo is None else head_repo
    return {
        "number": PR_NUMBER,
        "state": "open",
        "changed_files": changed_files,
        "head": {"sha": HEAD_SHA, "ref": "feature", "repo": head_repo},
        "base": {
            "sha": BASE_SHA,
            "ref": gate.BASE_BRANCH,
            "repo": _repo(gate),
        },
    }


def _comparison(gate, *, files: list[dict] | None = None) -> dict:
    files = (
        [{"filename": "src/boundver/cli.py", "status": "modified"}]
        if files is None
        else files
    )
    path = f"{BASE_SHA}...{HEAD_SHA}"
    return {
        "url": f"https://api.github.com/repos/{gate.REPOSITORY}/compare/{path}",
        "html_url": f"https://github.com/{gate.REPOSITORY}/compare/{path}",
        "status": "identical" if not files else "ahead",
        "ahead_by": 0 if not files else 1,
        "behind_by": 0,
        "total_commits": 0 if not files else 1,
        "base_commit": {"sha": BASE_SHA},
        "merge_base_commit": {"sha": BASE_SHA},
        "files": files,
    }


def _fetcher(
    gate,
    *,
    jobs: dict | None = None,
    pull: dict | None = None,
    files: list[dict] | None = None,
    comparison: dict | None = None,
):
    jobs = _jobs(gate) if jobs is None else jobs
    pull = _pull(gate) if pull is None else pull
    comparison = _comparison(gate, files=files) if comparison is None else comparison

    def fetch(endpoint: str):
        if endpoint.startswith(
            f"/repos/{gate.REPOSITORY}/actions/runs/{RUN_ID}/attempts/"
        ):
            return jobs
        if endpoint == f"/repos/{gate.REPOSITORY}/pulls/{PR_NUMBER}":
            return pull
        if endpoint == (
            f"/repos/{gate.REPOSITORY}/compare/"
            f"{BASE_SHA}...{HEAD_SHA}?per_page=1"
        ):
            return comparison
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    return fetch


class RequiredCiEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = _load_script()

    def test_exact_success_topology_passes(self) -> None:
        result = self.gate.evaluate(_event(self.gate), _fetcher(self.gate))
        self.assertEqual(result["sha"], HEAD_SHA)
        self.assertEqual(result["pull_number"], PR_NUMBER)

    def test_legitimate_fork_head_is_supported(self) -> None:
        fork = _repo(self.gate, repo_id=998_877, name="boundver-fork")
        result = self.gate.evaluate(
            _event(self.gate, head_repo=fork),
            _fetcher(self.gate, pull=_pull(self.gate, head_repo=fork)),
        )
        self.assertEqual(result["sha"], HEAD_SHA)

    def test_source_workflow_must_be_complete_and_successful(self) -> None:
        for field, value, message in (
            ("name", "Other", "workflow name"),
            ("path", ".github/workflows/other.yml", "workflow path"),
            ("event", "push", "pull-request run"),
            ("status", "in_progress", "not complete"),
            ("conclusion", "failure", "did not succeed"),
            ("html_url", "https://example.invalid/run", "not canonical"),
        ):
            with self.subTest(field=field):
                event = _event(self.gate)
                event["workflow_run"][field] = value
                with self.assertRaisesRegex(self.gate.RequiredCiGateError, message):
                    self.gate.evaluate(event, _fetcher(self.gate))

    def test_every_non_success_job_state_fails_closed(self) -> None:
        for status, conclusion in (
            ("completed", "failure"),
            ("completed", "cancelled"),
            ("completed", "skipped"),
            ("in_progress", None),
        ):
            with self.subTest(status=status, conclusion=conclusion):
                jobs = _jobs(self.gate)
                jobs["jobs"][0]["status"] = status
                jobs["jobs"][0]["conclusion"] = conclusion
                with self.assertRaisesRegex(
                    self.gate.RequiredCiGateError, "did not succeed"
                ):
                    self.gate.evaluate(
                        _event(self.gate), _fetcher(self.gate, jobs=jobs)
                    )

    def test_missing_extra_and_duplicate_jobs_are_rejected(self) -> None:
        missing = _jobs(self.gate)
        missing["jobs"].pop()
        missing["total_count"] -= 1
        extra = _jobs(self.gate)
        extra["jobs"].append(
            {
                "name": "optional",
                "run_id": RUN_ID,
                "run_attempt": RUN_ATTEMPT,
                "status": "completed",
                "conclusion": "success",
            }
        )
        extra["total_count"] += 1
        duplicate = _jobs(self.gate)
        duplicate["jobs"][-1]["name"] = duplicate["jobs"][0]["name"]
        for jobs, message in (
            (missing, "missing:"),
            (extra, "unexpected: optional"),
            (duplicate, "duplicate job names"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(self.gate.RequiredCiGateError, message):
                    self.gate.evaluate(
                        _event(self.gate), _fetcher(self.gate, jobs=jobs)
                    )

    def test_job_run_identity_and_complete_page_are_required(self) -> None:
        wrong_attempt = _jobs(self.gate)
        wrong_attempt["jobs"][0]["run_attempt"] = RUN_ATTEMPT + 1
        incomplete = _jobs(self.gate)
        incomplete["total_count"] += 1
        for jobs, message in (
            (wrong_attempt, "wrong run identity"),
            (incomplete, "response is incomplete"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(self.gate.RequiredCiGateError, message):
                    self.gate.evaluate(
                        _event(self.gate), _fetcher(self.gate, jobs=jobs)
                    )

    def test_gate_control_changes_are_rejected_in_both_rename_directions(self) -> None:
        cases = (
            {"filename": ".github"},
            {"filename": ".github/workflows"},
            {"filename": ".github/workflows/ci.yml"},
            {"filename": ".github/workflows/new.yml"},
            {"filename": "scripts"},
            {"filename": "scripts/check_required_ci_results.py"},
            {"filename": ".github/rulesets/protect-main.json"},
            {
                "filename": "docs/old-gate.md",
                "previous_filename": ".github/workflows/old.yml",
            },
        )
        for record in cases:
            with self.subTest(record=record):
                with self.assertRaisesRegex(
                    self.gate.RequiredCiGateError, "protected gate control"
                ):
                    self.gate.evaluate(
                        _event(self.gate),
                        _fetcher(self.gate, files=[record]),
                    )

    def test_live_pull_identity_is_bound_to_event_and_current_head(self) -> None:
        cases = []
        wrong_head = _pull(self.gate)
        wrong_head["head"]["sha"] = "c" * 40
        cases.append((wrong_head, "head changed"))
        wrong_base = _pull(self.gate)
        wrong_base["base"]["sha"] = "d" * 40
        cases.append((wrong_base, "base changed"))
        closed = _pull(self.gate)
        closed["state"] = "closed"
        cases.append((closed, "not the expected open"))
        for pull, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(self.gate.RequiredCiGateError, message):
                    self.gate.evaluate(
                        _event(self.gate), _fetcher(self.gate, pull=pull)
                    )

    def test_file_policy_uses_the_immutable_validated_base_and_head(self) -> None:
        endpoints: list[str] = []
        delegate = _fetcher(self.gate)

        def fetch(endpoint: str):
            endpoints.append(endpoint)
            return delegate(endpoint)

        self.gate.evaluate(_event(self.gate), fetch)
        self.assertIn(
            f"/repos/{self.gate.REPOSITORY}/compare/"
            f"{BASE_SHA}...{HEAD_SHA}?per_page=1",
            endpoints,
        )
        self.assertFalse(any("/files?" in endpoint for endpoint in endpoints))

        wrong_base = _comparison(self.gate)
        wrong_base["merge_base_commit"] = {"sha": "c" * 40}
        with self.assertRaisesRegex(
            self.gate.RequiredCiGateError, "not anchored"
        ):
            self.gate.evaluate(
                _event(self.gate),
                _fetcher(self.gate, comparison=wrong_base),
            )

        wrong_pair = _comparison(self.gate)
        wrong_pair["url"] = wrong_pair["url"].replace(HEAD_SHA, "d" * 40)
        with self.assertRaisesRegex(
            self.gate.RequiredCiGateError, "does not identify"
        ):
            self.gate.evaluate(
                _event(self.gate),
                _fetcher(self.gate, comparison=wrong_pair),
            )

    def test_pull_file_count_is_bounded_by_the_immutable_compare_api(self) -> None:
        with self.assertRaisesRegex(
            self.gate.RequiredCiGateError,
            f"{self.gate.MAX_PULL_FILES}-file limit",
        ):
            self.gate.evaluate(
                _event(self.gate),
                _fetcher(
                    self.gate,
                    pull=_pull(
                        self.gate,
                        changed_files=self.gate.MAX_PULL_FILES + 1,
                    ),
                ),
            )

    def test_repository_and_file_listing_fail_closed(self) -> None:
        event = _event(self.gate)
        event["repository"]["id"] = 99
        with self.assertRaisesRegex(
            self.gate.RequiredCiGateError, "does not identify"
        ):
            self.gate.evaluate(event, _fetcher(self.gate))

        incomplete = _comparison(self.gate)
        incomplete["files"] = []
        with self.assertRaisesRegex(
            self.gate.RequiredCiGateError, "file listing is incomplete"
        ):
            self.gate.evaluate(
                _event(self.gate),
                _fetcher(self.gate, comparison=incomplete),
            )


class RequiredCiMainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = _load_script()

    def test_main_publishes_success_only_after_evaluation(self) -> None:
        client = mock.Mock()
        client.get.side_effect = _fetcher(self.gate)
        with (
            mock.patch.object(self.gate, "_read_event", return_value=_event(self.gate)),
            mock.patch.object(self.gate, "GitHubClient", return_value=client),
            mock.patch.dict(os.environ, {self.gate.TOKEN_ENV: "token"}, clear=True),
        ):
            self.assertEqual(self.gate.main(), 0)
        client.status.assert_called_once_with(
            HEAD_SHA,
            state="success",
            description="All merge-critical CI jobs passed under base-controlled policy.",
            target_url=f"https://github.com/{self.gate.REPOSITORY}/actions/runs/{RUN_ID}",
        )

    def test_main_publishes_failure_for_a_safe_identified_run(self) -> None:
        client = mock.Mock()
        client.get.side_effect = _fetcher(
            self.gate, files=[{"filename": ".github/workflows/ci.yml"}]
        )
        with (
            mock.patch.object(self.gate, "_read_event", return_value=_event(self.gate)),
            mock.patch.object(self.gate, "GitHubClient", return_value=client),
            mock.patch.dict(os.environ, {self.gate.TOKEN_ENV: "token"}, clear=True),
        ):
            self.assertEqual(self.gate.main(), 1)
        client.status.assert_called_once_with(
            HEAD_SHA,
            state="failure",
            description="Required PR gate rejected this CI run.",
            target_url=f"https://github.com/{self.gate.REPOSITORY}/actions/runs/{RUN_ID}",
        )

    def test_status_publication_failure_is_not_reported_as_success(self) -> None:
        client = mock.Mock()
        client.get.side_effect = _fetcher(self.gate)
        client.status.side_effect = self.gate.RequiredCiGateError("status failed")
        with (
            mock.patch.object(self.gate, "_read_event", return_value=_event(self.gate)),
            mock.patch.object(self.gate, "GitHubClient", return_value=client),
            mock.patch.dict(os.environ, {self.gate.TOKEN_ENV: "token"}, clear=True),
        ):
            self.assertEqual(self.gate.main(), 1)
        self.assertEqual(client.status.call_count, 2)


class RequiredCiWorkflowTests(unittest.TestCase):
    def test_base_controlled_workflow_publishes_the_required_status(self) -> None:
        path = ROOT / ".github" / "workflows" / "required-pr-gate.yml"
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        trigger = workflow[True]["workflow_run"]
        self.assertEqual(trigger["workflows"], ["CI"])
        self.assertEqual(trigger["types"], ["completed"])
        self.assertEqual(
            workflow["permissions"],
            {
                "actions": "read",
                "contents": "read",
                "pull-requests": "read",
                "statuses": "write",
            },
        )
        gate = workflow["jobs"]["publish-required-status"]
        self.assertEqual(
            gate["if"], "${{ github.event.workflow_run.event == 'pull_request' }}"
        )
        checkout = gate["steps"][0]
        self.assertEqual(
            checkout["with"]["ref"],
            "${{ github.event.workflow_run.pull_requests[0].base.sha }}",
        )
        self.assertFalse(checkout["with"]["persist-credentials"])
        publish = gate["steps"][1]
        self.assertEqual(publish["run"], "python -I scripts/check_required_ci_results.py")
        self.assertEqual(publish["env"]["BOUNDVER_GATE_TOKEN"], "${{ github.token }}")

    def test_untrusted_ci_workflow_cannot_publish_the_required_status(self) -> None:
        workflow = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "ci.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("required-pr-gate", workflow["jobs"])
        self.assertEqual(workflow["permissions"], {"contents": "read"})

    def test_checked_in_ruleset_requires_the_exact_github_actions_gate(self) -> None:
        ruleset = json.loads(
            (ROOT / ".github" / "rulesets" / "protect-main.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(ruleset["target"], "branch")
        self.assertEqual(ruleset["enforcement"], "active")
        self.assertEqual(ruleset["bypass_actors"], [])
        self.assertEqual(
            ruleset["conditions"]["ref_name"],
            {"include": ["refs/heads/main"], "exclude": []},
        )
        by_type = {rule["type"]: rule for rule in ruleset["rules"]}
        self.assertIn("deletion", by_type)
        self.assertIn("non_fast_forward", by_type)
        pull = by_type["pull_request"]["parameters"]
        self.assertEqual(pull["allowed_merge_methods"], ["squash"])
        self.assertTrue(pull["required_review_thread_resolution"])
        checks = by_type["required_status_checks"]["parameters"]
        self.assertTrue(checks["strict_required_status_checks_policy"])
        self.assertEqual(
            checks["required_status_checks"],
            [{"context": "required-pr-gate", "integration_id": 15368}],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
