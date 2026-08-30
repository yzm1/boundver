"""Contracts for the stable, merge-critical CI aggregate."""

from __future__ import annotations

import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_required_ci_results.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_required_ci_results", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - test invariant
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _needs(result: str = "success") -> dict:
    return {
        name: {"result": result, "outputs": {}}
        for name in ("test", "build", "action", "public-installations")
    }


class RequiredCiResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = _load_script()

    def test_exact_success_set_passes(self) -> None:
        results = self.gate.validate_required_ci_results(json.dumps(_needs()))
        self.assertEqual(set(results), set(self.gate.REQUIRED_JOBS))
        self.assertEqual(set(results.values()), {"success"})

    def test_every_non_success_state_fails_closed(self) -> None:
        for state in ("failure", "cancelled", "skipped", "pending", ""):
            with self.subTest(state=state):
                value = _needs()
                value["action"]["result"] = state
                with self.assertRaisesRegex(
                    self.gate.RequiredCiGateError,
                    "action=",
                ):
                    self.gate.validate_required_ci_results(json.dumps(value))

    def test_missing_and_unexpected_jobs_are_rejected(self) -> None:
        missing = _needs()
        del missing["build"]
        unexpected = _needs()
        unexpected["optional"] = {"result": "success"}
        for value, message in (
            (missing, "missing: build"),
            (unexpected, "unexpected: optional"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    self.gate.RequiredCiGateError,
                    message,
                ):
                    self.gate.validate_required_ci_results(json.dumps(value))

    def test_malformed_duplicate_and_oversized_inputs_are_rejected(self) -> None:
        cases = (
            ("", "missing or empty"),
            ("[]", "JSON object"),
            ("{", "valid JSON"),
            ('{"test":{},"test":{}}', "duplicate JSON key"),
            ("x" * (self.gate.MAX_RESULTS_BYTES + 1), "byte limit"),
        )
        for raw, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    self.gate.RequiredCiGateError,
                    message,
                ):
                    self.gate.validate_required_ci_results(raw)

    def test_main_has_stable_exit_contract(self) -> None:
        with patch.dict(
            os.environ,
            {self.gate.RESULTS_ENV: json.dumps(_needs())},
            clear=True,
        ):
            self.assertEqual(self.gate.main(), 0)
        with patch.dict(
            os.environ,
            {self.gate.RESULTS_ENV: json.dumps(_needs("skipped"))},
            clear=True,
        ):
            self.assertEqual(self.gate.main(), 1)


class RequiredCiWorkflowTests(unittest.TestCase):
    def test_workflow_gate_depends_on_every_merge_critical_job(self) -> None:
        workflow = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "ci.yml").read_text(
                encoding="utf-8"
            )
        )
        gate = workflow["jobs"]["required-pr-gate"]

        self.assertEqual(gate["name"], "required-pr-gate")
        self.assertEqual(
            set(gate["needs"]),
            {"test", "build", "action", "public-installations"},
        )
        self.assertIn("always()", gate["if"])
        step = gate["steps"][1]
        self.assertEqual(
            step["env"]["BOUNDVER_REQUIRED_CI_NEEDS"],
            "${{ toJSON(needs) }}",
        )
        self.assertEqual(
            step["run"],
            "python -I scripts/check_required_ci_results.py",
        )

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
        self.assertTrue(
            by_type["pull_request"]["parameters"][
                "required_review_thread_resolution"
            ]
        )
        checks = by_type["required_status_checks"]["parameters"]
        self.assertTrue(checks["strict_required_status_checks_policy"])
        self.assertEqual(
            checks["required_status_checks"],
            [{"context": "required-pr-gate", "integration_id": 15368}],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
