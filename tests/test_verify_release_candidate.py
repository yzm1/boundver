"""Contracts for the shared release-candidate verification sequence."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._project_metadata import CURRENT_TAG, CURRENT_VERSION


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify_release_candidate.py"
PLATFORM_HELPER = REPO_ROOT / "scripts" / "_release_platform.py"
TAG = CURRENT_TAG
SHA = "1" * 40


def _load_script():
    spec = importlib.util.spec_from_file_location("verify_release_candidate", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_platform_helper():
    spec = importlib.util.spec_from_file_location("release_platform", PLATFORM_HELPER)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {PLATFORM_HELPER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class VerifyReleaseCandidateTests(unittest.TestCase):
    def test_isolated_direct_startup_loads_adjacent_platform_helper(self):
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release candidate", result.stdout.lower())

    def test_release_tag_numeric_identifiers_are_ascii_only(self):
        verifier = _load_script()
        with self.assertRaisesRegex(
            verifier.CandidateVerificationError,
            "exact vMAJOR.MINOR.PATCH",
        ):
            verifier.verify_candidate(REPO_ROOT, "v1.2.\u0663", SHA)

    def test_windows_prefers_git_bash_over_wsl_bash(self):
        platform = _load_platform_helper()
        git = r"C:\Program Files\Git\cmd\git.exe"
        git_bash = r"C:\Program Files\Git\bin\bash.exe"

        def which(command, *, path):
            self.assertEqual(path, "safe-tools")
            return git if command == "git" else r"C:\Windows\System32\bash.exe"

        with mock.patch.object(
            platform.shutil, "which", side_effect=which
        ) as finder, mock.patch.object(
                platform.os.path,
                "isfile",
                side_effect=lambda candidate: candidate == git_bash,
        ):
            bash = platform.resolve_bash("safe-tools", platform_name="nt")

        self.assertEqual(bash, git_bash)
        finder.assert_called_once_with("git", path="safe-tools")

    def test_windows_rejects_wsl_bash_when_git_bash_is_unavailable(self):
        platform = _load_platform_helper()

        def which(command, *, path):
            self.assertEqual(path, "safe-tools")
            if command == "git":
                return None
            if command == "bash":
                return r"C:\Windows\System32\bash.exe"
            self.fail(f"unexpected lookup: {command}")

        with mock.patch.object(platform.shutil, "which", side_effect=which) as finder:
            bash = platform.resolve_bash("safe-tools", platform_name="nt")

        self.assertIsNone(bash)
        finder.assert_called_once_with("git", path="safe-tools")

    def test_shared_sequence_uses_exact_commit_epoch_and_artifacts(self):
        verifier = _load_script()
        commands: list[tuple[tuple[str, ...], dict[str, str]]] = []

        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "dist").mkdir()

            def run(command, *, cwd, env, capture_output=False):
                self.assertEqual(cwd, repo.resolve())
                commands.append((tuple(command), dict(env)))
                if command[-1] == "scripts/packaging_smoke.sh":
                    (repo / "dist" / f"boundver-{CURRENT_VERSION}-py3-none-any.whl").touch()
                    (repo / "dist" / f"boundver-{CURRENT_VERSION}.tar.gz").touch()
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(
                verifier, "_git_output", side_effect=(SHA, "1700000000")
            ), mock.patch.object(verifier, "_run", side_effect=run), mock.patch.object(
                verifier, "_packaging_bash", return_value="/tools/bash"
            ):
                wheel, sdist = verifier.verify_candidate(
                    repo,
                    TAG,
                    SHA,
                    python=sys.executable,
                    environment={"PATH": "safe-tools", "SAFE_VALUE": "kept"},
                )

        self.assertEqual(wheel.name, f"boundver-{CURRENT_VERSION}-py3-none-any.whl")
        self.assertEqual(sdist.name, f"boundver-{CURRENT_VERSION}.tar.gz")
        self.assertEqual(
            commands[0][0],
            (
                sys.executable,
                "-I",
                "scripts/verify_release_readiness.py",
                "--tag",
                TAG,
            ),
        )
        self.assertEqual(
            commands[1][0], (sys.executable, "-I", "-m", "pytest", "-q")
        )
        self.assertEqual(
            commands[2][0], ("/tools/bash", "scripts/packaging_smoke.sh")
        )
        self.assertEqual(commands[2][1]["SOURCE_DATE_EPOCH"], "1700000000")
        self.assertNotIn("SOURCE_DATE_EPOCH", commands[0][1])
        self.assertEqual(
            commands[3][0][:5],
            (sys.executable, "-I", "-m", "twine", "check"),
        )
        self.assertEqual(commands[3][1]["SAFE_VALUE"], "kept")

    def test_verifier_rejects_wrong_checkout_before_running_candidate_code(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            verifier, "_git_output", return_value="2" * 40
        ), mock.patch.object(verifier, "_run") as runner:
            with self.assertRaisesRegex(
                verifier.CandidateVerificationError,
                "does not match release SHA",
            ):
                verifier.verify_candidate(Path(temporary), TAG, SHA)
        runner.assert_not_called()

    def test_verifier_requires_exactly_one_wheel_and_sdist(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / "dist").mkdir()
            (repo / "dist" / "one.whl").touch()
            (repo / "dist" / "two.whl").touch()
            (repo / "dist" / "one.tar.gz").touch()
            with self.assertRaisesRegex(
                verifier.CandidateVerificationError,
                "exactly one wheel and one source distribution",
            ):
                verifier._release_distributions(repo)

    def test_distribution_inventory_is_lazy_and_entry_bounded(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            distribution_dir = repo / "dist"
            distribution_dir.mkdir()
            (distribution_dir / "one.whl").touch()
            (distribution_dir / "one.tar.gz").touch()
            (distribution_dir / "extra.pyz").touch()
            with mock.patch.object(
                verifier.Path,
                "glob",
                side_effect=AssertionError("Path.glob must not be used"),
            ), mock.patch.object(verifier, "MAX_DIST_ENTRIES", 2):
                with self.assertRaisesRegex(
                    verifier.CandidateVerificationError,
                    "2-entry limit",
                ):
                    verifier._release_distributions(repo)

    def test_distribution_inventory_bounds_names_and_rejects_nonregular_entries(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            distribution_dir = repo / "dist"
            distribution_dir.mkdir()
            (distribution_dir / "one.whl").touch()
            (distribution_dir / "one.tar.gz").touch()
            with mock.patch.object(verifier, "MAX_DIST_NAME_BYTES", 4):
                with self.assertRaisesRegex(
                    verifier.CandidateVerificationError,
                    "name exceeds",
                ):
                    verifier._release_distributions(repo)

        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            distribution_dir = repo / "dist"
            distribution_dir.mkdir()
            (distribution_dir / "one.whl").mkdir()
            (distribution_dir / "one.tar.gz").touch()
            with self.assertRaisesRegex(
                verifier.CandidateVerificationError,
                "non-regular",
            ):
                verifier._release_distributions(repo)

    def test_distribution_inventory_bounds_aggregate_names_and_file_growth(self):
        verifier = _load_script()
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            distribution_dir = repo / "dist"
            distribution_dir.mkdir()
            (distribution_dir / "one.whl").touch()
            (distribution_dir / "one.tar.gz").touch()
            with mock.patch.object(verifier, "MAX_DIST_TOTAL_NAME_BYTES", 10):
                with self.assertRaisesRegex(
                    verifier.CandidateVerificationError,
                    "aggregate limit",
                ):
                    verifier._release_distributions(repo)

        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            distribution_dir = repo / "dist"
            distribution_dir.mkdir()
            (distribution_dir / "one.whl").touch()
            (distribution_dir / "one.tar.gz").touch()
            with mock.patch.object(
                verifier,
                "_changed",
                side_effect=(False, True),
            ):
                with self.assertRaisesRegex(
                    verifier.CandidateVerificationError,
                    "changed while being inspected",
                ):
                    verifier._release_distributions(repo)

    def test_windows_reparse_attribute_is_rejected(self):
        verifier = _load_script()
        identity = mock.Mock(st_file_attributes=0x400)

        self.assertTrue(verifier._is_windows_reparse_point(identity))

    def test_audit_and_install_boundaries_stay_outside_verifier(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("audit_release_reviews.sh", source)
        self.assertNotIn("pip install", source)
        self.assertNotIn("GH_TOKEN", source)
        self.assertNotIn("TWINE_PASSWORD", source)

        publisher = (REPO_ROOT / "scripts" / "publish_release.py").read_text(
            encoding="utf-8"
        )
        audit = publisher.index("scripts/audit_release_reviews.sh")
        install = publisher.index("scripts/install_locked_tools.py", audit)
        verify = publisher.index("scripts/verify_release_candidate.py", install)
        self.assertLess(audit, install)
        self.assertLess(install, verify)

    def test_release_workflows_call_the_same_verifier(self):
        import yaml

        for relative in (
            ".github/workflows/create-release-tag.yml",
            ".github/workflows/publish.yml",
        ):
            with self.subTest(workflow=relative):
                workflow = (REPO_ROOT / relative).read_text(encoding="utf-8")
                self.assertEqual(
                    workflow.count("scripts/verify_release_candidate.py"), 1
                )
                self.assertIn('--tag "$RELEASE_TAG"', workflow)
                self.assertIn('--release-sha "$RELEASE_SHA"', workflow)
                self.assertNotIn("python -m twine check dist/*.whl", workflow)
                self.assertNotIn("SOURCE_DATE_EPOCH=$(git show", workflow)

                parsed = yaml.safe_load(workflow)
                job_name = (
                    "verify-candidate"
                    if "create-release-tag" in relative
                    else "verify-release"
                )
                steps = parsed["jobs"][job_name]["steps"]
                by_name = {step["name"]: step for step in steps}
                install = by_name["Install hash-locked release verification tools"]
                verify = by_name[
                    "Verify release candidate source and distributions"
                ]
                self.assertIn("scripts/install_locked_tools.py release", install["run"])
                self.assertIn("--no-index --no-deps", install["run"])
                self.assertNotIn("pip install", verify["run"])
                self.assertNotIn("GH_TOKEN", verify.get("env", {}))
                self.assertLess(steps.index(install), steps.index(verify))

                if job_name == "verify-candidate":
                    audit = by_name[
                        "Require completed reviews since the previous release"
                    ]
                    self.assertIn("GH_TOKEN", audit["env"])
                    self.assertLess(steps.index(audit), steps.index(install))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
