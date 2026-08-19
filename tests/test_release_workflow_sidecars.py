"""Focused tests for read-only release-workflow sidecars."""

from __future__ import annotations

import importlib.util
import io
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from tests._repo_fixtures import commit_all, init_git_repo


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_release_probe():
    path = REPO_ROOT / "scripts" / "probe_github_release.py"
    spec = importlib.util.spec_from_file_location("probe_github_release", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_release_probe()


def _load_release_surface_verifier():
    path = REPO_ROOT / "scripts" / "verify_release_surfaces.py"
    spec = importlib.util.spec_from_file_location("release_surface_phase_contract", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release_surfaces = _load_release_surface_verifier()


def _load_alias_validator():
    path = REPO_ROOT / "scripts" / "validate_compatibility_alias.py"
    spec = importlib.util.spec_from_file_location(
        "validate_compatibility_alias", path
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


alias_validator = _load_alias_validator()


def _response(status: int, payload: str = "") -> str:
    return f"HTTP/2.0 {status} status\r\ncontent-type: application/json\r\n\r\n{payload}"


class GitHubReleaseProbeTests(unittest.TestCase):
    def test_classifies_public_draft_and_absent_releases(self):
        self.assertEqual(
            probe.release_state("0", _response(200, '{"draft": false}'))[0],
            "public",
        )
        self.assertEqual(
            probe.release_state("0", _response(200, '{"draft": true}'))[0],
            "draft",
        )
        self.assertEqual(probe.release_state("1", _response(404))[0], "absent")

    def test_uses_the_final_http_response_after_a_redirect(self):
        response = _response(301) + "\r\n" + _response(200, '{"draft": false}')
        self.assertEqual(probe.release_state("0", response)[0], "public")

    def test_rejects_failed_or_malformed_success_responses(self):
        cases = (
            ("not-an-exit", _response(200, '{"draft": false}')),
            ("9" * 5_000, _response(200, '{"draft": false}')),
            ("256", _response(200, '{"draft": false}')),
            ("1", _response(200, '{"draft": false}')),
            ("0", _response(500, "{}")),
            ("0", _response(200, "not-json")),
            ("0", _response(200, '{"draft": 0}')),
            ("0", "no HTTP response"),
        )
        for api_exit, response in cases:
            with self.subTest(api_exit=api_exit, response=response):
                with self.assertRaises(probe.ReleaseProbeError):
                    probe.release_state(api_exit, response)

    def test_cli_writes_only_the_classified_workflow_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner_temp = Path(temp_dir)
            response = runner_temp / "github-release-response.ABC123"
            response.write_bytes(_response(200, '{"draft": true}').encode("utf-8"))
            github_output = runner_temp / "github-output"

            result = probe.main(
                [
                    "--api-exit",
                    "0",
                    "--response",
                    str(response),
                    "--runner-temp",
                    str(runner_temp),
                    "--github-output",
                    str(github_output),
                ]
            )

            self.assertEqual(result, 0)
            self.assertEqual(
                github_output.read_text(encoding="utf-8"),
                "release-state=draft\n",
            )

    def test_cli_rejects_a_response_outside_the_runner_temp_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            response = root / "github-release-response.ABC123"
            response.write_bytes(_response(404).encode("utf-8"))
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                result = probe.main(
                    [
                        "--api-exit",
                        "1",
                        "--response",
                        str(response),
                        "--runner-temp",
                        str(runner_temp),
                        "--github-output",
                        str(root / "github-output"),
                    ]
                )

            self.assertEqual(result, 1)
            self.assertIn("response path is unsafe", stderr.getvalue())

    def test_workflow_invokes_the_reviewed_control_checkout(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "publish.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '"$GITHUB_WORKSPACE/.release-control/scripts/probe_github_release.py"',
            workflow,
        )
        self.assertNotIn("release_probe=$(", workflow)

    def test_workflow_uses_only_supported_public_surface_phases(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "publish.yml").read_text(
            encoding="utf-8"
        )
        invoked = set(re.findall(r"--phase\s+([a-z]+)", workflow))
        self.assertIn("github", invoked)
        self.assertTrue(invoked <= release_surfaces.PHASES, invoked)


class CompatibilityAliasValidatorTests(unittest.TestCase):
    def _git(self, root: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_monotonic_alias_move_and_idempotent_resume(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote = root / "remote.git"
            repo = root / "repo"
            runner_temp = root / "runner-temp"
            repo.mkdir()
            runner_temp.mkdir()
            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                capture_output=True,
            )
            init_git_repo(repo, initial_branch="main")
            self._git(repo, "remote", "add", "origin", str(remote))

            tracked = repo / "contract.txt"
            tracked.write_text("patch zero\n", encoding="utf-8")
            commit_all(repo, "patch zero")
            previous_sha = self._git(repo, "rev-parse", "HEAD")
            self._git(repo, "tag", "v0.11.0")
            self._git(repo, "tag", "v0.11")
            self._git(repo, "push", "origin", "main", "--tags")

            tracked.write_text("patch one\n", encoding="utf-8")
            commit_all(repo, "patch one")
            release_sha = self._git(repo, "rev-parse", "HEAD")
            self._git(repo, "tag", "v0.11.1")
            self._git(repo, "push", "origin", "main", "refs/tags/v0.11.1")

            output = runner_temp / "github-output"
            output.touch()
            self.assertEqual(
                alias_validator.main(
                    [
                        "--release-tag",
                        "v0.11.1",
                        "--release-sha",
                        release_sha,
                        "--alias",
                        "v0.11",
                        "--repo",
                        str(repo),
                        "--runner-temp",
                        str(runner_temp),
                        "--github-output",
                        str(output),
                    ]
                ),
                0,
            )
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "update-required=true\n"
                f"expected-current={previous_sha}\n",
            )

            self._git(repo, "tag", "--force", "v0.11", release_sha)
            self._git(repo, "push", "--force", "origin", "refs/tags/v0.11")
            resumed_output = runner_temp / "github-output-resume"
            resumed_output.touch()
            self.assertEqual(
                alias_validator.main(
                    [
                        "--release-tag",
                        "v0.11.1",
                        "--release-sha",
                        release_sha,
                        "--alias",
                        "v0.11",
                        "--repo",
                        str(repo),
                        "--runner-temp",
                        str(runner_temp),
                        "--github-output",
                        str(resumed_output),
                    ]
                ),
                0,
            )
            self.assertEqual(
                resumed_output.read_text(encoding="utf-8"),
                "update-required=false\n"
                f"expected-current={release_sha}\n",
            )

    def test_alias_rollback_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            remote = root / "remote.git"
            repo = root / "repo"
            runner_temp = root / "runner-temp"
            repo.mkdir()
            runner_temp.mkdir()
            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                capture_output=True,
            )
            init_git_repo(repo, initial_branch="main")
            self._git(repo, "remote", "add", "origin", str(remote))
            tracked = repo / "contract.txt"
            tracked.write_text("patch zero\n", encoding="utf-8")
            commit_all(repo, "patch zero")
            old_sha = self._git(repo, "rev-parse", "HEAD")
            self._git(repo, "tag", "v0.11.0")
            tracked.write_text("patch one\n", encoding="utf-8")
            commit_all(repo, "patch one")
            self._git(repo, "tag", "v0.11.1")
            self._git(repo, "tag", "v0.11")
            self._git(repo, "push", "origin", "main", "--tags")

            output = runner_temp / "github-output"
            output.touch()
            self.assertEqual(
                alias_validator.main(
                    [
                        "--release-tag",
                        "v0.11.0",
                        "--release-sha",
                        old_sha,
                        "--alias",
                        "v0.11",
                        "--repo",
                        str(repo),
                        "--runner-temp",
                        str(runner_temp),
                        "--github-output",
                        str(output),
                    ]
                ),
                1,
            )
            self.assertEqual(output.read_bytes(), b"")

    def test_tag_record_parser_is_bounded(self):
        with self.assertRaisesRegex(
            alias_validator.AliasValidationError, "record limit"
        ):
            alias_validator._records(
                b"a" * (alias_validator.MAX_TAG_LINE_BYTES + 1)
            )


if __name__ == "__main__":
    unittest.main()
