"""Behavioral contracts for the maintainer release entry point.

The local command is deliberately a gate and dispatcher, not a second
publisher.  Irreversible registry and GitHub mutations belong to the protected
workflows after this command has proved exactly which commit is being released.
"""

from __future__ import annotations

import copy
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
CONTROL_SHA = "2" * 40
RUN_ID = 7654321
RUN_ATTEMPT = 3
ALIAS = "v0.11"


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


def _resume_api_payloads(
    *,
    run_attempt: int = RUN_ATTEMPT,
    artifact_attempt: int | None = None,
) -> dict[str, object]:
    if artifact_attempt is None:
        artifact_attempt = run_attempt
    run_endpoint = f"repos/yzm1/boundver/actions/runs/{RUN_ID}"
    association = {
        "id": RUN_ID,
        "head_branch": TAG,
        "head_sha": SHA,
    }
    return {
        run_endpoint: {
            "id": RUN_ID,
            "repository": {"full_name": "yzm1/boundver"},
            "path": ".github/workflows/publish.yml",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "failure",
            "head_branch": TAG,
            "head_sha": SHA,
            "run_attempt": run_attempt,
        },
        f"{run_endpoint}/jobs?filter=all&per_page=100": {
            "total_count": 1,
            "jobs": [
                {
                    "id": 31,
                    "name": "verify-release",
                    "run_id": RUN_ID,
                    "run_attempt": artifact_attempt,
                    "head_sha": SHA,
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        },
        f"{run_endpoint}/artifacts?per_page=100": {
            "total_count": 2,
            "artifacts": [
                {
                    "id": 41,
                    "size_in_bytes": 100,
                    "name": f"python-dist-{TAG}-{RUN_ID}-{artifact_attempt}",
                    "expired": False,
                    "expires_at": "2999-01-01T00:00:00Z",
                    "digest": f"sha256:{'a' * 64}",
                    "workflow_run": dict(association),
                },
                {
                    "id": 42,
                    "size_in_bytes": 200,
                    "name": f"release-assets-{TAG}-{RUN_ID}-{artifact_attempt}",
                    "expired": False,
                    "expires_at": "2999-01-01T00:00:00Z",
                    "digest": f"sha256:{'b' * 64}",
                    "workflow_run": dict(association),
                },
            ],
        },
    }


def _verify_release_job_log(
    *,
    tag: str = TAG,
    sha: str = SHA,
    alias: str = ALIAS,
    repetitions: int = 2,
) -> str:
    lines: list[str] = []
    for index in range(repetitions):
        timestamp = f"2026-08-14T09:20:{21 + index:02d}.1234567Z"
        lines.extend(
            (
                f"{timestamp}   RELEASE_TAG: {tag}",
                f"{timestamp}   RELEASE_SHA: {sha}",
                f"{timestamp}   COMPATIBILITY_ALIAS: {alias}",
            )
        )
    return "\n".join(lines) + "\n"


class PublishReleaseInterfaceTests(unittest.TestCase):
    def test_check_start_and_resume_are_explicit_subcommands(self):
        top = _run("--help")
        self.assertEqual(top.returncode, 0, top.stderr)
        self.assertRegex(top.stdout, r"\{check,start,resume\}")

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

        resume = _run("resume", "--help")
        self.assertEqual(resume.returncode, 0, resume.stderr)
        for option in (
            "--tag",
            "--confirm",
            "--alias",
            "--run-id",
            "--repo",
            "--remote",
            "--format",
        ):
            self.assertIn(option, resume.stdout)

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

    def test_resume_rejects_malformed_run_ids_before_evaluation(self):
        for run_id in ("0", "-1", "+1", "01", "1.0", "abc"):
            with self.subTest(run_id=run_id):
                result = _run(
                    "resume",
                    "--tag",
                    TAG,
                    "--alias",
                    "v0.11",
                    "--run-id",
                    run_id,
                    "--confirm",
                    f"{TAG}@{SHA}#{run_id}",
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("run-id", result.stderr.lower())

    def test_resume_confirmation_binds_tag_sha_and_run_id_before_evaluation(self):
        confirmations = (
            f"{TAG}@{'A' * 40}#{RUN_ID}",
            f"{TAG}@{SHA}",
            f"{TAG}@{SHA}#{RUN_ID + 1}",
            f"v0.11.1@{SHA}#{RUN_ID}",
        )
        for confirmation in confirmations:
            with self.subTest(confirmation=confirmation):
                result = _run(
                    "resume",
                    "--tag",
                    TAG,
                    "--alias",
                    "v0.11",
                    "--run-id",
                    str(RUN_ID),
                    "--confirm",
                    confirmation,
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("confirm", result.stderr.lower())

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

    def test_successful_resume_performs_only_the_exact_publish_dispatch(self):
        publisher = _load_script()
        checks = [publisher.Check("resume release gates", "passed", "passed")]
        dispatch_result = subprocess.CompletedProcess(
            args=(), returncode=0, stdout="dispatch accepted\n", stderr=""
        )
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(
                publisher, "_evaluate_resume", return_value=(CONTROL_SHA, checks)
            ) as evaluate,
            mock.patch.object(publisher, "_evaluate") as start_evaluate,
            mock.patch.object(
                publisher, "_main_identity", return_value=CONTROL_SHA
            ) as revalidate,
            mock.patch.object(
                publisher, "_run", return_value=dispatch_result
            ) as runner,
            mock.patch("builtins.print") as output,
        ):
            resolved_repo = Path(td).resolve()
            result = publisher.main(
                [
                    "resume",
                    "--tag",
                    TAG,
                    "--alias",
                    "v0.11",
                    "--run-id",
                    str(RUN_ID),
                    "--confirm",
                    f"{TAG}@{SHA}#{RUN_ID}",
                    "--repo",
                    td,
                    "--format",
                    "json",
                ]
            )

        self.assertEqual(result, 0)
        evaluate.assert_called_once_with(
            Path(td), "origin", TAG, ALIAS, RUN_ID, SHA
        )
        start_evaluate.assert_not_called()
        revalidate.assert_called_once_with(resolved_repo, "origin", CONTROL_SHA)
        runner.assert_called_once()
        self.assertEqual(
            runner.call_args.args[0],
            (
                "gh",
                "workflow",
                "run",
                "publish.yml",
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
                "--field",
                f"resume_run_id={RUN_ID}",
            ),
        )
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["phase"], "resume")
        self.assertEqual(payload["sha"], SHA)
        self.assertEqual(payload["status"], "dispatched")
        self.assertEqual(payload["dispatch"]["workflow"], "publish.yml")
        self.assertEqual(payload["dispatch"]["ref"], "main")
        self.assertEqual(payload["dispatch"]["resume_run_id"], str(RUN_ID))

    def test_resume_keeps_current_main_separate_from_the_tagged_release(self):
        publisher = _load_script()
        passing = {
            "_surface_inventory": "surfaces",
            "_repo_identity": "repository",
            "_clean": "clean",
            "_repository_hygiene": "hygiene",
            "_project_at_commit": "version",
            "_main_identity": CONTROL_SHA,
            "_resume_release_state": "tag",
            "_release_is_on_main": "ancestor",
            "_github_controls": "controls",
            "_source_release_artifacts": "artifacts",
        }
        patches = [
            mock.patch.object(publisher, name, return_value=value)
            for name, value in passing.items()
        ]
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            publisher, "_head", return_value=CONTROL_SHA
        ), patches[0] as surfaces, patches[1] as identity, patches[2] as clean, patches[
            3
        ] as hygiene, patches[4] as project, patches[5] as main, patches[
            6
        ] as tag_state, patches[7] as ancestry, patches[8] as controls, patches[
            9
        ] as artifacts:
            resolved_repo = Path(td).resolve()
            control_sha, checks = publisher._evaluate_resume(
                Path(td), "origin", TAG, ALIAS, RUN_ID, SHA
            )

        self.assertEqual(control_sha, CONTROL_SHA)
        self.assertTrue(all(check.status == "passed" for check in checks))
        main.assert_called_once_with(resolved_repo, "origin", CONTROL_SHA)
        tag_state.assert_called_once_with(resolved_repo, "origin", TAG, SHA)
        ancestry.assert_called_once_with(resolved_repo, SHA, CONTROL_SHA)
        controls.assert_called_once_with(
            resolved_repo, CONTROL_SHA, TAG, allow_draft_release=True
        )
        artifacts.assert_called_once_with(
            resolved_repo, RUN_ID, TAG, SHA, ALIAS
        )
        for called in (surfaces, identity, clean, hygiene, project):
            called.assert_called_once()
        project.assert_called_once_with(resolved_repo, SHA, TAG)

    def test_resume_reads_version_from_release_commit_not_current_main(self):
        publisher = _load_script()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            release_sha = _init_repo(root)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "boundver"\nversion = "0.12.0"\n',
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "pyproject.toml"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "advance development version"],
                cwd=root,
                check=True,
            )

            detail = publisher._project_at_commit(root, release_sha, TAG)
            with self.assertRaisesRegex(publisher.GateError, "does not match"):
                publisher._project(root, TAG)

        self.assertIn(TAG[1:], detail)
        self.assertIn(release_sha, detail)

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

    def test_resume_source_run_and_artifacts_are_bound_to_exact_identity(self):
        publisher = _load_script()
        payloads = _resume_api_payloads()

        def response(_repo, _repository, endpoint):
            return copy.deepcopy(payloads[endpoint])

        with mock.patch.object(
            publisher, "_gh_json", side_effect=response
        ) as api, mock.patch.object(
            publisher,
            "_gh_job_log",
            return_value=_verify_release_job_log(),
        ) as job_log:
            detail = publisher._source_release_artifacts(
                Path("."), RUN_ID, TAG, SHA, ALIAS
            )

        self.assertIn(str(RUN_ID), detail)
        self.assertIn(str(RUN_ATTEMPT), detail)
        self.assertIn(ALIAS, detail)
        self.assertEqual(api.call_count, 3)
        job_log.assert_called_once_with(Path("."), 31)

        with mock.patch.object(
            publisher, "_gh_json", side_effect=response
        ), mock.patch.object(
            publisher,
            "_gh_job_log",
            return_value=_verify_release_job_log(),
        ), self.assertRaisesRegex(publisher.GateError, "COMPATIBILITY_ALIAS"):
            publisher._source_release_artifacts(
                Path("."), RUN_ID, TAG, SHA, "none"
            )

    def test_resume_reuses_verified_artifact_attempt_after_failed_job_rerun(self):
        publisher = _load_script()
        payloads = _resume_api_payloads(run_attempt=2, artifact_attempt=1)

        def response(_repo, _repository, endpoint):
            return copy.deepcopy(payloads[endpoint])

        with mock.patch.object(
            publisher, "_gh_json", side_effect=response
        ), mock.patch.object(
            publisher,
            "_gh_job_log",
            return_value=_verify_release_job_log(),
        ):
            detail = publisher._source_release_artifacts(
                Path("."), RUN_ID, TAG, SHA, ALIAS
            )

        self.assertIn("attempt 2", detail)
        self.assertIn("verify-release attempt 1", detail)

    def test_resume_source_log_binds_the_exact_original_release_inputs(self):
        publisher = _load_script()
        detail = publisher._require_source_release_inputs(
            _verify_release_job_log(repetitions=3), TAG, SHA, ALIAS
        )
        self.assertIn("3 release input triple(s)", detail)
        self.assertIn(ALIAS, detail)
        none_detail = publisher._require_source_release_inputs(
            _verify_release_job_log(alias="none", repetitions=1),
            TAG,
            SHA,
            "none",
        )
        self.assertIn("alias none", none_detail)

        base = _verify_release_job_log(repetitions=1)
        missing_sha = "\n".join(
            line for line in base.splitlines() if "RELEASE_SHA" not in line
        )
        malformed_alias = base.replace(
            f"COMPATIBILITY_ALIAS: {ALIAS}",
            f"COMPATIBILITY_ALIAS={ALIAS}",
        )
        alternate_triple = base + _verify_release_job_log(
            alias="none", repetitions=1
        )
        cases = (
            (
                "requested alias differs",
                _verify_release_job_log(alias="none", repetitions=1),
                "expected value",
            ),
            ("missing SHA", missing_sha, "complete release input triples"),
            ("malformed alias", malformed_alias, "malformed"),
            ("spoofed alternate triple", alternate_triple, "expected value"),
        )
        for label, job_log, message in cases:
            with self.subTest(label=label), self.assertRaisesRegex(
                publisher.GateError, message
            ):
                publisher._require_source_release_inputs(
                    job_log, TAG, SHA, ALIAS
                )

    def test_resume_fetches_only_the_selected_verify_release_job_log(self):
        publisher = _load_script()
        expected_log = _verify_release_job_log(repetitions=1)
        result = subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout=expected_log.encode("utf-8"),
            stderr=b"",
        )
        help_without_flag = subprocess.CompletedProcess(
            args=(), returncode=0, stdout=b"USAGE: gh api", stderr=b""
        )
        help_with_flag = subprocess.CompletedProcess(
            args=(),
            returncode=0,
            stdout=b"--allow-escape-sequences",
            stderr=b"",
        )
        with mock.patch.object(
            publisher.subprocess,
            "run",
            side_effect=[help_without_flag, result],
        ) as runner:
            actual = publisher._gh_job_log(Path("repo"), 31)

        self.assertEqual(actual, expected_log)
        self.assertEqual(
            runner.call_args_list,
            [
                mock.call(
                    ["gh", "api", "--help"],
                    cwd=Path("repo"),
                    capture_output=True,
                    check=False,
                ),
                mock.call(
                    ["gh", "api", "repos/yzm1/boundver/actions/jobs/31/logs"],
                    cwd=Path("repo"),
                    capture_output=True,
                    check=False,
                ),
            ],
        )

        with mock.patch.object(
            publisher.subprocess,
            "run",
            side_effect=[help_with_flag, result],
        ) as guarded_runner:
            guarded = publisher._gh_job_log(Path("repo"), 31)

        self.assertEqual(guarded, expected_log)
        self.assertEqual(
            guarded_runner.call_args_list,
            [
                mock.call(
                    ["gh", "api", "--help"],
                    cwd=Path("repo"),
                    capture_output=True,
                    check=False,
                ),
                mock.call(
                    [
                        "gh",
                        "api",
                        "--allow-escape-sequences",
                        "repos/yzm1/boundver/actions/jobs/31/logs",
                    ],
                    cwd=Path("repo"),
                    capture_output=True,
                    check=False,
                ),
            ],
        )

        with mock.patch.object(
            publisher.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                (), 1, b"", b"help unavailable"
            ),
        ), self.assertRaisesRegex(publisher.GateError, "API capabilities"):
            publisher._gh_job_log(Path("repo"), 31)

        for invalid_job_id in (0, -1, True, "31"):
            with self.subTest(job_id=invalid_job_id), self.assertRaisesRegex(
                publisher.GateError, "job ID is malformed"
            ):
                publisher._gh_job_log(Path("repo"), invalid_job_id)

        failures = (
            (
                subprocess.CompletedProcess((), 1, b"", b"forbidden"),
                "GitHub API failed",
            ),
            (subprocess.CompletedProcess((), 0, b"", b""), "job log is empty"),
            (
                subprocess.CompletedProcess((), 0, b"\x81", b""),
                "not valid UTF-8",
            ),
        )
        for response, message in failures:
            with self.subTest(message=message), mock.patch.object(
                publisher.subprocess,
                "run",
                side_effect=[help_without_flag, response],
            ), self.assertRaisesRegex(publisher.GateError, message):
                publisher._gh_job_log(Path("repo"), 31)

        with mock.patch.object(
            publisher.subprocess,
            "run",
            side_effect=[
                help_without_flag,
                subprocess.CompletedProcess((), 0, b"12345", b""),
            ],
        ), mock.patch.object(
            publisher, "MAX_JOB_LOG_BYTES", 4
        ), self.assertRaisesRegex(publisher.GateError, "inspection limit"):
            publisher._gh_job_log(Path("repo"), 31)

    def test_resume_requires_the_existing_exact_tag_and_no_legacy_branch(self):
        publisher = _load_script()
        with mock.patch.object(
            publisher,
            "_remote_ref",
            side_effect=(SHA, None),
        ):
            detail = publisher._resume_release_state(Path("."), "origin", TAG, SHA)
        self.assertIn(SHA, detail)

        for tag_sha, branch, message in (
            (None, None, "absent"),
            ("2" * 40, None, "not release SHA"),
            (SHA, "3" * 40, "legacy release branch"),
        ):
            with self.subTest(message=message), mock.patch.object(
                publisher,
                "_remote_ref",
                side_effect=(tag_sha, branch),
            ), self.assertRaisesRegex(publisher.GateError, message):
                publisher._resume_release_state(Path("."), "origin", TAG, SHA)

    def test_resume_rejects_stale_or_spoofed_source_evidence(self):
        publisher = _load_script()
        run_endpoint = f"repos/yzm1/boundver/actions/runs/{RUN_ID}"
        cases = {}

        stale = _resume_api_payloads()
        stale[run_endpoint]["head_sha"] = "2" * 40
        cases["stale source SHA"] = (stale, "exact release tag and SHA")

        malformed = _resume_api_payloads()
        malformed[run_endpoint] = []
        cases["malformed source run"] = (malformed, "malformed")

        future_attempt = _resume_api_payloads(artifact_attempt=RUN_ATTEMPT + 1)
        cases["future verification attempt"] = (future_attempt, "successful exact")

        malformed_job = _resume_api_payloads()
        malformed_job[
            f"{run_endpoint}/jobs?filter=all&per_page=100"
        ]["jobs"][0]["id"] = True
        cases["malformed verification job"] = (malformed_job, "successful exact")

        expired = _resume_api_payloads()
        expired[f"{run_endpoint}/artifacts?per_page=100"]["artifacts"][0][
            "expires_at"
        ] = "2000-01-01T00:00:00Z"
        cases["expired artifact"] = (expired, "expired")

        spoofed_run = _resume_api_payloads()
        spoofed_run[f"{run_endpoint}/artifacts?per_page=100"]["artifacts"][0][
            "workflow_run"
        ]["id"] = RUN_ID + 1
        cases["spoofed artifact association"] = (spoofed_run, "association")

        spoofed_digest = _resume_api_payloads()
        spoofed_digest[f"{run_endpoint}/artifacts?per_page=100"]["artifacts"][0][
            "digest"
        ] = "sha256:not-a-digest"
        cases["spoofed artifact digest"] = (spoofed_digest, "digest")

        empty_artifact = _resume_api_payloads()
        empty_artifact[f"{run_endpoint}/artifacts?per_page=100"]["artifacts"][0][
            "size_in_bytes"
        ] = 0
        cases["empty artifact"] = (empty_artifact, "identity")

        wrong_name = _resume_api_payloads()
        wrong_name[f"{run_endpoint}/artifacts?per_page=100"]["artifacts"][0][
            "name"
        ] = f"python-dist-{TAG}-{RUN_ID + 1}-{RUN_ATTEMPT}"
        cases["artifact name from another run"] = (wrong_name, "names")

        for label, (payloads, message) in cases.items():
            def response(_repo, _repository, endpoint):
                return copy.deepcopy(payloads[endpoint])

            with self.subTest(label=label), mock.patch.object(
                publisher, "_gh_json", side_effect=response
            ), mock.patch.object(
                publisher,
                "_gh_job_log",
                return_value=_verify_release_job_log(),
            ), self.assertRaisesRegex(publisher.GateError, message):
                publisher._source_release_artifacts(
                    Path("."), RUN_ID, TAG, SHA, ALIAS
                )

    def test_resume_fails_closed_on_source_api_errors_or_incomplete_pages(self):
        publisher = _load_script()
        with mock.patch.object(
            publisher,
            "_gh_json",
            side_effect=publisher.GateError("GitHub API unavailable"),
        ), self.assertRaisesRegex(publisher.GateError, "API unavailable"):
            publisher._source_release_artifacts(
                Path("."), RUN_ID, TAG, SHA, ALIAS
            )

        payloads = _resume_api_payloads()
        jobs_endpoint = (
            f"repos/yzm1/boundver/actions/runs/{RUN_ID}"
            "/jobs?filter=all&per_page=100"
        )
        payloads[jobs_endpoint]["total_count"] = 101

        def response(_repo, _repository, endpoint):
            return copy.deepcopy(payloads[endpoint])

        with mock.patch.object(
            publisher, "_gh_json", side_effect=response
        ), self.assertRaisesRegex(publisher.GateError, "completely inspect"):
            publisher._source_release_artifacts(
                Path("."), RUN_ID, TAG, SHA, ALIAS
            )

    def test_resume_accepts_only_absent_or_draft_github_release(self):
        publisher = _load_script()

        def response(_repo, _repository, endpoint):
            if endpoint.endswith(f"/releases/tags/{TAG}"):
                return {"tag_name": TAG, "draft": True}
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
                                    "reviewers": [{"type": "User"}],
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
                    "conditions": {
                        "ref_name": {
                            "include": ["refs/tags/v*.*.*"],
                            "exclude": [],
                        }
                    },
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

        present = subprocess.CompletedProcess(
            ["gh", "api"], 0, "HTTP/2.0 200 OK\n{}\n", ""
        )
        with mock.patch.object(
            publisher, "_gh_json", side_effect=response
        ), mock.patch.object(publisher, "_run", return_value=present):
            publisher._github_controls(
                Path("."), SHA, TAG, allow_draft_release=True
            )

        def public_response(repo, repository, endpoint):
            value = response(repo, repository, endpoint)
            if endpoint.endswith(f"/releases/tags/{TAG}"):
                value["draft"] = False
            return value

        with mock.patch.object(
            publisher, "_gh_json", side_effect=public_response
        ), mock.patch.object(
            publisher, "_run", return_value=present
        ), self.assertRaisesRegex(publisher.GateError, "already public"):
            publisher._github_controls(
                Path("."), SHA, TAG, allow_draft_release=True
            )

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

    def test_runbook_routes_maintainers_through_start_and_supported_resume(self):
        runbook = (REPO_ROOT / "docs" / "RELEASING.md").read_text(
            encoding="utf-8"
        )
        check_command = "scripts/publish_release.py check --tag"
        start_command = "scripts/publish_release.py start --tag"
        resume_command = "scripts/publish_release.py resume"
        self.assertIn(check_command, runbook)
        self.assertIn(start_command, runbook)
        self.assertIn(resume_command, runbook)
        self.assertLess(runbook.index(check_command), runbook.index(start_command))
        self.assertIn("--confirm", runbook)
        self.assertIn("--alias", runbook)
        self.assertIn("--run-id", runbook)
        self.assertIn("#$run_id", runbook)
        self.assertIn("COMPATIBILITY_ALIAS", runbook)
        self.assertIn("Missing, malformed,\nor alternate values fail closed", runbook)
        self.assertIn("read-only", runbook.lower())
        self.assertNotIn("gh workflow run publish.yml", runbook)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
