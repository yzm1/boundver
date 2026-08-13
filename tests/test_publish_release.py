"""Behavioral contracts for the maintainer release entry point.

The local command is deliberately a gate and dispatcher, not a second
publisher.  Irreversible registry and GitHub mutations belong to the protected
workflows after this command has proved exactly which commit is being released.
"""

from __future__ import annotations

import json
import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "publish_release.py"
TAG = "v0.11.0"
SHA = "1" * 40


def _load_script():
    spec = importlib.util.spec_from_file_location("publish_release", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run(*arguments: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "release-test@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Release Test"], cwd=root, check=True
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "boundver"\nversion = "0.11.0"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "pyproject.toml"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "release candidate"], cwd=root, check=True)
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()


def _refs(root: Path) -> str:
    return subprocess.run(
        ["git", "show-ref"], cwd=root, capture_output=True, text=True
    ).stdout


class PublishReleaseInterfaceTests(unittest.TestCase):
    def test_check_and_start_are_explicit_subcommands(self):
        top = _run("--help")
        self.assertEqual(top.returncode, 0, top.stderr)
        self.assertRegex(top.stdout, r"\{check,start\}")

        check = _run("check", "--help")
        self.assertEqual(check.returncode, 0, check.stderr)
        for option in ("--tag", "--repo", "--remote", "--format"):
            self.assertIn(option, check.stdout)

        start = _run("start", "--help")
        self.assertEqual(start.returncode, 0, start.stderr)
        for option in (
            "--tag",
            "--confirm",
            "--alias",
            "--repo",
            "--remote",
            "--format",
        ):
            self.assertIn(option, start.stdout)

    def test_start_requires_an_explicit_compatibility_alias_policy(self):
        result = _run(
            "start",
            "--tag",
            TAG,
            "--confirm",
            f"{TAG}@{SHA}",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--alias", result.stderr)

    def test_start_rejects_an_alias_outside_the_release_minor_line(self):
        result = _run(
            "start",
            "--tag",
            TAG,
            "--alias",
            "v0",
            "--confirm",
            f"{TAG}@{SHA}",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("alias", result.stderr.lower())

    def test_confirmation_is_the_exact_tag_and_lowercase_commit(self):
        result = _run(
            "start",
            "--tag",
            TAG,
            "--alias",
            "none",
            "--confirm",
            f"{TAG}@{'A' * 40}",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confirm", result.stderr.lower())

    def test_confirmation_must_equal_head_before_any_dispatch(self):
        publisher = _load_script()
        checks = [publisher.Check("all release gates", "passed", "passed")]
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(publisher, "_evaluate", return_value=(SHA, checks)),
            mock.patch.object(publisher, "_main_identity") as revalidate,
            mock.patch.object(publisher, "_run") as runner,
            mock.patch("builtins.print"),
        ):
            result = publisher.main(
                [
                    "start",
                    "--tag",
                    TAG,
                    "--alias",
                    "v0.11",
                    "--confirm",
                    f"{TAG}@{'2' * 40}",
                    "--repo",
                    td,
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(result, 1)
        revalidate.assert_not_called()
        runner.assert_not_called()

    def test_successful_start_performs_only_the_one_workflow_dispatch(self):
        publisher = _load_script()
        checks = [publisher.Check("all release gates", "passed", "passed")]
        dispatch_result = subprocess.CompletedProcess(
            args=(), returncode=0, stdout="dispatch accepted\n", stderr=""
        )
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(publisher, "_evaluate", return_value=(SHA, checks)),
            mock.patch.object(publisher, "_main_identity", return_value=SHA),
            mock.patch.object(
                publisher, "_run", return_value=dispatch_result
            ) as runner,
            mock.patch("builtins.print") as output,
        ):
            result = publisher.main(
                [
                    "start",
                    "--tag",
                    TAG,
                    "--alias",
                    "v0.11",
                    "--confirm",
                    f"{TAG}@{SHA}",
                    "--repo",
                    td,
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(result, 0)
        runner.assert_called_once()
        command = runner.call_args.args[0]
        self.assertEqual(
            command,
            (
                "gh",
                "workflow",
                "run",
                "create-release-tag.yml",
                "--repo",
                "yzm1/boundver",
                "--ref",
                "main",
                "--field",
                f"release_tag={TAG}",
                "--field",
                f"release_sha={SHA}",
                "--field",
                "compatibility_alias=v0.11",
            ),
        )
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["status"], "dispatched")
        self.assertEqual(payload["dispatch"]["workflow"], "create-release-tag.yml")
        self.assertEqual(payload["dispatch"]["ref"], "main")

    def test_json_failures_keep_stdout_machine_readable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sha = _init_repo(root)
            (root / "untracked.txt").write_text("not releasable\n", encoding="utf-8")
            result = _run(
                "check", "--tag", TAG, "--repo", str(root), "--format", "json"
            )

        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "phase",
                "tag",
                "sha",
                "status",
                "checks",
                "dispatch",
            },
        )
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["phase"], "check")
        self.assertEqual(payload["tag"], TAG)
        self.assertEqual(payload["sha"], sha)
        self.assertEqual(payload["status"], "failed")
        self.assertIsNone(payload["dispatch"])
        self.assertTrue(payload["checks"])
        for check in payload["checks"]:
            self.assertEqual(set(check), {"name", "status", "detail"})
            self.assertIn(check["status"], {"passed", "failed"})

    def test_json_success_states_distinguish_ready_from_dispatched(self):
        publisher = _load_script()
        checks = [publisher.Check("all release gates", "passed", "passed")]
        with mock.patch("builtins.print") as output:
            result = publisher._emit(
                publisher.argparse.Namespace(
                    command="check", tag=TAG, format="json"
                ),
                SHA,
                checks,
                None,
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["status"], "ready")
        self.assertIsNone(payload["dispatch"])

        dispatch = {
            "workflow": "create-release-tag.yml",
            "ref": "main",
            "tag": TAG,
            "sha": SHA,
            "alias": "v0.11",
            "detail": "dispatch accepted",
        }
        with mock.patch("builtins.print") as output:
            result = publisher._emit(
                publisher.argparse.Namespace(
                    command="start", tag=TAG, format="json"
                ),
                SHA,
                checks,
                dispatch,
            )
        self.assertEqual(result, 0)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["status"], "dispatched")
        self.assertEqual(payload["dispatch"], dispatch)

    def test_successful_check_never_crosses_the_dispatch_boundary(self):
        publisher = _load_script()
        checks = [publisher.Check("all release gates", "passed", "passed")]
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(publisher, "_evaluate", return_value=(SHA, checks)),
            mock.patch.object(publisher, "_main_identity") as revalidate,
            mock.patch.object(publisher, "_run") as runner,
            mock.patch("builtins.print") as output,
        ):
            result = publisher.main(
                [
                    "check",
                    "--tag",
                    TAG,
                    "--repo",
                    td,
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(result, 0)
        revalidate.assert_not_called()
        runner.assert_not_called()
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["status"], "ready")
        self.assertIsNone(payload["dispatch"])

    def test_check_rejects_each_kind_of_dirty_repository_without_mutation(self):
        dirty_states = {
            "tracked": lambda root: (root / "pyproject.toml").write_text(
                '[project]\nname = "boundver"\nversion = "9.9.9"\n',
                encoding="utf-8",
            ),
            "staged": lambda root: subprocess.run(
                ["git", "add", "pyproject.toml"], cwd=root, check=True
            ),
            "untracked": lambda root: (root / "untracked.txt").write_text(
                "not releasable\n", encoding="utf-8"
            ),
        }
        for kind, make_dirty in dirty_states.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                _init_repo(root)
                if kind == "staged":
                    (root / "pyproject.toml").write_text(
                        '[project]\nname = "boundver"\nversion = "9.9.9"\n',
                        encoding="utf-8",
                    )
                make_dirty(root)
                refs_before = _refs(root)
                status_before = subprocess.check_output(
                    ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                    cwd=root,
                    text=True,
                )

                result = _run("check", "--tag", TAG, "--repo", str(root))

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("clean", (result.stdout + result.stderr).lower())
                self.assertEqual(_refs(root), refs_before)
                self.assertEqual(
                    subprocess.check_output(
                        [
                            "git",
                            "status",
                            "--porcelain=v1",
                            "--untracked-files=all",
                        ],
                        cwd=root,
                        text=True,
                    ),
                    status_before,
                )

    def test_script_is_a_gate_not_a_direct_publisher(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("create-release-tag.yml", source)
        self.assertIn("release_tag", source)
        self.assertIn("release_sha", source)
        self.assertIn("compatibility_alias", source)
        self.assertNotRegex(source, r"workflow\s+run\s+publish\.yml")
        self.assertNotRegex(source, r"(?s)[\"']git[\"']\s*,\s*[\"'](?:push|tag)[\"']")
        self.assertNotIn("twine upload", source)
        self.assertNotIn("gh release create", source)
        self.assertNotRegex(source, r"[\"']gh[\"']\s*,\s*[\"']api[\"']\s*,\s*[\"']--repo[\"']")

    def test_full_local_gate_builds_in_a_disposable_checkout(self):
        source = SCRIPT.read_text(encoding="utf-8")
        gate = source[source.index("def _disposable_gate") : source.index("def _surface_inventory")]
        self.assertIn("TemporaryDirectory", gate)
        self.assertIn('"clone"', gate)
        self.assertIn('"checkout", "--quiet", "--detach", sha', gate)
        self.assertIn('env["GITHUB_REPOSITORY"] = REPOSITORY', gate)
        self.assertRegex(
            gate,
            r"_run\(\(\"bash\", \"scripts/packaging_smoke\.sh\"\), cwd=checkout",
        )
        self.assertNotRegex(
            gate,
            r"packaging_smoke\.sh[^\n]*cwd=repo",
        )

    def test_start_dispatch_contract_is_main_and_never_publish_workflow(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertRegex(source, r"[\"']--ref[\"']\s*,\s*[\"']main[\"']")
        self.assertRegex(
            source,
            r"[\"']--field[\"'].*release_tag",
        )
        self.assertRegex(
            source,
            r"[\"']--field[\"'].*release_sha",
        )
        self.assertRegex(
            source,
            r"[\"']--field[\"'].*compatibility_alias",
        )
        self.assertNotRegex(source, rf"[\"']--ref[\"']\s*,\s*{re.escape(SHA)}")

        workflow = (
            REPO_ROOT / ".github" / "workflows" / "create-release-tag.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        for release_input in (
            "release_tag:",
            "release_sha:",
            "compatibility_alias:",
        ):
            self.assertIn(release_input, workflow)
        self.assertIn("ref: ${{ inputs.release_sha }}", workflow)

    def test_full_gate_names_every_release_surface_and_local_contract(self):
        source = SCRIPT.read_text(encoding="utf-8").lower()
        for helper in (
            "check_repo_hygiene.py",
            "verify_release_readiness.py",
            "audit_release_reviews.sh",
            "packaging_smoke.sh",
        ):
            self.assertIn(helper, source)
        for surface in (
            "readme",
            "documentation",
            "changelog",
            "schema",
            "action",
            "docker",
            "pre-commit",
            "testpypi",
            "pypi",
            "github release",
            "marketplace",
        ):
            self.assertIn(surface, source)

    def test_github_controls_require_visibility_metadata_rules_and_absence(self):
        publisher = _load_script()

        def response(_repo, _repository, endpoint):
            if endpoint == "repos/yzm1/boundver":
                return {
                    "full_name": "yzm1/boundver",
                    "default_branch": "main",
                    "archived": False,
                    "visibility": "public",
                    "homepage": "https://github.com/marketplace/actions/boundver",
                    "description": "Contract change detection",
                    "topics": [
                        "api-compatibility",
                        "ci",
                        "openapi",
                        "semantic-versioning",
                    ],
                }
            if endpoint.endswith("/environments"):
                return {
                    "environments": [
                        {
                            "name": name,
                            "protection_rules": [
                                {
                                    "type": "required_reviewers",
                                    "reviewers": [{"type": "User", "reviewer": {"id": 1}}],
                                }
                            ],
                        }
                        for name in ("testpypi", "pypi", "marketplace")
                    ]
                }
            if endpoint.endswith("/immutable-releases"):
                return {"enabled": True}
            if endpoint.endswith("/rulesets?includes_parents=true"):
                return [{"id": 7, "target": "tag", "enforcement": "active"}]
            if endpoint.endswith("/rulesets/7"):
                return {
                    "rules": [{"type": "update"}, {"type": "deletion"}],
                    "conditions": {"ref_name": {"include": ["refs/tags/v*.*.*"], "exclude": []}},
                }
            if "actions/workflows/ci.yml/runs" in endpoint:
                return {
                    "workflow_runs": [
                        {
                            "head_sha": SHA,
                            "head_branch": "main",
                            "event": "push",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                }
            if "actions/workflows/" in endpoint:
                return {"workflow_runs": []}
            raise AssertionError(endpoint)

        absent = subprocess.CompletedProcess(
            ["gh", "api"], 1, "HTTP/2.0 404 Not Found\n", ""
        )
        with mock.patch.object(publisher, "_gh_json", side_effect=response), mock.patch.object(
            publisher, "_run", return_value=absent
        ):
            detail = publisher._github_controls(Path("."), SHA, TAG)
        self.assertIn("environments", detail)

        forbidden = subprocess.CompletedProcess(
            ["gh", "api"], 1, "HTTP/2.0 403 Forbidden\n", ""
        )
        with mock.patch.object(publisher, "_gh_json", side_effect=response), mock.patch.object(
            publisher, "_run", return_value=forbidden
        ):
            with self.assertRaisesRegex(publisher.GateError, "cannot prove"):
                publisher._github_controls(Path("."), SHA, TAG)

    def test_version_tag_ruleset_does_not_capture_mutable_minor_alias(self):
        publisher = _load_script()
        self.assertTrue(
            publisher._github_ref_pattern_matches(
                "refs/tags/v*.*.*", "refs/tags/v0.11.0"
            )
        )
        self.assertFalse(
            publisher._github_ref_pattern_matches(
                "refs/tags/v*.*.*", "refs/tags/v0.11"
            )
        )
        self.assertTrue(
            publisher._github_ref_pattern_matches(
                "refs/tags/v*", "refs/tags/v0.11"
            )
        )
        exact = {"include": ["refs/tags/v*.*.*"], "exclude": []}
        broad = {"include": ["refs/tags/v*"], "exclude": []}
        self.assertTrue(
            publisher._ruleset_targets_ref(exact, "refs/tags/v0.11.0")
        )
        self.assertFalse(
            publisher._ruleset_targets_ref(exact, "refs/tags/v0.11")
        )
        self.assertTrue(
            publisher._ruleset_targets_ref(broad, "refs/tags/v0.11")
        )

        scoped = {
            "rules": [{"type": "update"}, {"type": "deletion"}],
            "conditions": {"ref_name": exact},
        }
        publisher._validate_tag_rulesets([scoped], TAG)
        broad_update = {
            "rules": [{"type": "update"}],
            "conditions": {"ref_name": broad},
        }
        with self.assertRaisesRegex(publisher.GateError, "mutable"):
            publisher._validate_tag_rulesets([scoped, broad_update], TAG)
        exact_creation = {
            "rules": [{"type": "creation"}],
            "conditions": {"ref_name": exact},
        }
        with self.assertRaisesRegex(publisher.GateError, "creation restriction"):
            publisher._validate_tag_rulesets([scoped, exact_creation], TAG)

    def test_release_environments_require_real_reviewers(self):
        publisher = _load_script()
        self.assertFalse(
            publisher._environment_requires_review(
                {"protection_rules": [{"type": "wait_timer", "wait_timer": 30}]}
            )
        )
        self.assertFalse(
            publisher._environment_requires_review(
                {"protection_rules": [{"type": "required_reviewers", "reviewers": []}]}
            )
        )
        self.assertTrue(
            publisher._environment_requires_review(
                {
                    "protection_rules": [
                        {
                            "type": "required_reviewers",
                            "reviewers": [{"type": "User", "reviewer": {"id": 1}}],
                        }
                    ]
                }
            )
        )

    def test_surface_inventory_is_scoped_to_repo_and_checks_workflow_topology(self):
        publisher = _load_script()
        with tempfile.TemporaryDirectory() as td:
            incomplete_repo = Path(td)
            with self.assertRaisesRegex(publisher.GateError, "missing release surface"):
                publisher._surface_inventory(incomplete_repo)

        source = SCRIPT.read_text(encoding="utf-8")
        for workflow_contract in (
            "publish-testpypi",
            "verify-testpypi",
            "prepare-release-draft",
            "verify-marketplace",
            "publish-pypi",
            "verify-pypi",
            "advance-compatibility-alias",
            "verify-public-surfaces",
        ):
            self.assertIn(workflow_contract, source)

    def test_runbook_routes_maintainers_through_check_then_confirmed_start(self):
        runbook = (REPO_ROOT / "docs" / "RELEASING.md").read_text(
            encoding="utf-8"
        )
        check_command = "scripts/publish_release.py check --tag"
        start_command = "scripts/publish_release.py start --tag"
        self.assertIn(check_command, runbook)
        self.assertIn(start_command, runbook)
        self.assertLess(runbook.index(check_command), runbook.index(start_command))
        self.assertIn("--confirm", runbook)
        self.assertIn("--alias", runbook)
        self.assertIn("read-only", runbook.lower())
        self.assertNotIn("gh workflow run publish.yml", runbook)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
