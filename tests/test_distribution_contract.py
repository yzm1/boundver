"""Focused tests for shipped installation and release interfaces."""

from __future__ import annotations

import base64
import email.parser
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock
from zipfile import ZipFile


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_testpypi_verifier():
    path = REPO_ROOT / "scripts" / "verify_testpypi_release.py"
    spec = importlib.util.spec_from_file_location("testpypi_verifier", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_fake_distributions(dist: Path, version: str = "0.11.0") -> None:
    dist.mkdir()
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: boundver\n"
        f"Version: {version}\n\n"
    ).encode()
    wheel = dist / f"boundver-{version}-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(f"boundver-{version}.dist-info/METADATA", metadata)

    sdist = dist / f"boundver-{version}.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        member = tarfile.TarInfo(f"boundver-{version}/PKG-INFO")
        member.size = len(metadata)
        archive.addfile(member, io.BytesIO(metadata))


def _release_changelog(version: str, notes: str = "- Shipped safely.\n") -> str:
    return (
        "# Changelog\n\n"
        "## [Unreleased]\n\nNo changes yet.\n\n"
        f"## [{version}] - 2026-08-12\n\n{notes}\n"
        "## [0.10.0] - 2026-08-11\n\n- Previous release.\n\n"
        f"[Unreleased]: https://github.com/yzm1/boundver/compare/v{version}...HEAD\n"
        f"[{version}]: https://github.com/yzm1/boundver/compare/v0.10.0...v{version}\n"
        "[0.10.0]: https://github.com/yzm1/boundver/releases/tag/v0.10.0\n"
    )


def _project_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(
        r'(?ms)^\[project\]\s*$.*?^version\s*=\s*["\']([^"\']+)["\']',
        text,
    )
    if match is None:  # pragma: no cover - release metadata invariant
        raise AssertionError("static project.version not found")
    return match.group(1)


class StandaloneDistributionTests(unittest.TestCase):
    def test_zipapp_has_version_metadata_license_and_unique_staging(self):
        expected_version = _project_version()
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td)
            legacy_stage = output_dir / "_stage_boundver"
            legacy_stage.mkdir()
            sentinel = legacy_stage / "do-not-delete.txt"
            sentinel.write_text("owned by caller", encoding="utf-8")
            output = output_dir / "boundver.pyz"

            subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "build_standalone.py"),
                    "--output",
                    str(output),
                ],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "owned by caller")
            self.assertFalse(any(output_dir.glob(".boundver-standalone-*")))
            with ZipFile(output) as archive:
                names = set(archive.namelist())
                metadata_name = next(
                    name for name in names if name.endswith(".dist-info/METADATA")
                )
                metadata = email.parser.BytesParser().parsebytes(
                    archive.read(metadata_name)
                )
                self.assertEqual(metadata["Name"], "boundver")
                self.assertEqual(metadata["Version"], expected_version)
                self.assertEqual(
                    archive.read("LICENSE"), (REPO_ROOT / "LICENSE").read_bytes()
                )
                self.assertTrue(
                    any(name.endswith(".dist-info/licenses/LICENSE") for name in names)
                )

            result = subprocess.run(
                [sys.executable, str(output), "--version"],
                cwd=output_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(result.stdout.strip().endswith(f" {expected_version}"))


class AutomationContractTests(unittest.TestCase):
    def test_manifest_explicitly_prunes_repository_only_content(self):
        manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        for directory in ("tests", "scripts", ".github"):
            self.assertIn(f"prune {directory}", manifest)
        for path in (
            ".pre-commit-hooks.yaml",
            "Dockerfile",
            "action.yml",
            "boundary.config.json",
            "boundary.lock.json",
        ):
            self.assertIn(f"exclude {path}", manifest)

    def test_packaging_smoke_removes_stale_build_outputs(self):
        script = (REPO_ROOT / "scripts/packaging_smoke.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("rm -rf -- dist build src/boundver.egg-info", script)

    def test_release_workflow_orders_marketplace_before_production_and_uses_explicit_alias(self):
        import yaml

        jobs = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )["jobs"]
        self.assertIn("prepare-release-draft", jobs["verify-marketplace"]["needs"])
        self.assertEqual(jobs["pypi-preflight"]["needs"], "verify-release")
        self.assertIn("pypi-preflight", jobs["publish-pypi"]["needs"])
        self.assertIn("verify-marketplace", jobs["publish-pypi"]["needs"])
        self.assertIn("publish-pypi", jobs["verify-pypi"]["needs"])
        self.assertEqual(
            jobs["advance-compatibility-alias"]["needs"], "verify-pypi"
        )
        workflow = (REPO_ROOT / ".github/workflows/publish.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("compatibility_alias:", workflow)
        self.assertIn("COMPATIBILITY_ALIAS: ${{ inputs.compatibility_alias }}", workflow)
        self.assertIn("inputs.compatibility_alias == 'none'", workflow)
        self.assertIn("inputs.compatibility_alias != 'none'", workflow)
        self.assertIn("verification_alias_args=(--skip-alias)", workflow)
        self.assertIn("--phase complete", workflow)
        self.assertNotIn("refs/tags/v0", workflow)

    def test_publish_requires_dispatch_main_ancestry_and_distribution_smoke(self):
        workflow = (REPO_ROOT / ".github/workflows/publish.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("\n  push:\n", workflow)
        self.assertIn("refs/heads/main", workflow)
        self.assertIn("merge-base", workflow)
        self.assertIn("release commit is not on main", workflow.lower())
        self.assertIn("Version tag moved or disappeared", workflow)
        self.assertIn("bash scripts/packaging_smoke.sh", workflow)
        self.assertIn("dist/*.whl", workflow)
        self.assertIn("dist/*.tar.gz", workflow)
        self.assertGreaterEqual(
            workflow.count("scripts/release_changelog.py"), 2
        )
        self.assertIn("scripts/verify_release_readiness.py", workflow)
        self.assertIn('--notes-file "$notes_file"', workflow)
        self.assertIn("gh api --paginate --slurp", workflow)
        self.assertIn("releases?per_page=100", workflow)
        self.assertIn('releases/$release_id', workflow)
        self.assertIn("--draft", workflow)
        self.assertIn("SHA256SUMS", workflow)
        self.assertIn("environment: marketplace", workflow)
        self.assertIn("pypi-attestations verify pypi", workflow)
        self.assertNotIn("--generate-notes", workflow)
        self.assertNotIn("--clobber", workflow)
        self.assertNotIn("git tag --force v0 ", workflow)
        self.assertEqual(workflow.count("retention-days: 90"), 2)

    def test_publish_is_bound_to_exact_tag_sha_and_serializes_all_promotions(self):
        import yaml

        workflow_text = (
            REPO_ROOT / ".github/workflows/publish.yml"
        ).read_text(encoding="utf-8")
        workflow = yaml.safe_load(workflow_text)
        self.assertEqual(
            workflow["concurrency"]["group"], "boundver-release-promotion"
        )
        self.assertIs(workflow["concurrency"]["cancel-in-progress"], False)
        first_step = workflow["jobs"]["verify-release"]["steps"][0]
        self.assertEqual(
            first_step["name"],
            "Bind this dispatch to the exact version tag and commit",
        )
        binding = first_step["run"]
        self.assertIn('"$DISPATCH_REF_TYPE" != tag', binding)
        self.assertIn('"$DISPATCH_REF_NAME" != "$RELEASE_TAG"', binding)
        self.assertIn(
            '"$DISPATCH_REF" != "refs/tags/$RELEASE_TAG"', binding
        )
        self.assertIn('"$DISPATCH_SHA" != "$RELEASE_SHA"', binding)
        self.assertIn(
            '"$COMPATIBILITY_ALIAS" != none', binding
        )
        self.assertNotIn("publish-${{ inputs.release_tag }}", workflow_text)
        create_workflow = yaml.safe_load(
            (
                REPO_ROOT / ".github/workflows/create-release-tag.yml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            create_workflow["concurrency"]["group"],
            "boundver-release-promotion",
        )

    def test_release_lookup_and_asset_resume_fail_closed(self):
        workflow = (REPO_ROOT / ".github/workflows/publish.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("find_release_id()", workflow)
        self.assertIn("GitHub Release list lookup failed", workflow)
        self.assertIn("multiple GitHub Releases use the version tag", workflow)
        self.assertIn("GitHub Release state cannot be read by ID", workflow)
        self.assertEqual(workflow.count("releases/tags/$RELEASE_TAG"), 1)
        self.assertIn("GitHub Release contains unexpected assets", workflow)
        self.assertIn("GitHub Release asset conflicts with candidate bytes", workflow)
        self.assertIn('gh release upload "$RELEASE_TAG"', workflow)
        self.assertIn("Public GitHub Release is missing assets", workflow)
        self.assertIn(
            "Public GitHub Release must be immutable and have a publication timestamp",
            workflow,
        )
        self.assertIn(
            "Public immutable GitHub Release exactly matches the retained candidate",
            workflow,
        )
        self.assertIn(
            'value.replace("\\r\\n", "\\n").replace("\\r", "\\n")',
            workflow,
        )
        self.assertLess(
            workflow.index("cmp --silent"),
            workflow.index('gh release upload "$RELEASE_TAG"'),
        )
        self.assertNotIn('if ! gh release view "$RELEASE_TAG"', workflow)

    def test_public_release_checks_use_reviewed_control_code(self):
        import yaml

        jobs = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )["jobs"]
        control_checkouts = []
        for job_name, job in jobs.items():
            for step in job.get("steps", []):
                if step.get("name") == "Checkout the reviewed release-control commit":
                    control_checkouts.append(job_name)
        self.assertEqual(
            control_checkouts,
            [
                "testpypi-preflight",
                "verify-marketplace",
                "verify-public-surfaces",
            ],
        )

        for job_name in control_checkouts:
            steps = jobs[job_name]["steps"]
            checkout = next(
                step
                for step in steps
                if step.get("name") == "Checkout the reviewed release-control commit"
            )
            self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
            self.assertEqual(checkout["with"]["path"], ".release-control")
            verifier = next(
                step
                for step in steps
                if ".release-control/scripts/verify_release_surfaces.py"
                in str(step.get("run", ""))
            )
            self.assertIn(
                "python3 scripts/release_changelog.py",
                verifier["run"],
            )

    def test_public_release_conflicts_block_testpypi_mutation(self):
        import yaml

        jobs = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )["jobs"]
        steps = jobs["testpypi-preflight"]["steps"]
        public_gate = next(
            step
            for step in steps
            if step.get("name")
            == "Reject a conflicting public GitHub Release before registry mutation"
        )
        registry_gate = next(
            step
            for step in steps
            if step.get("name")
            == "Accept only a missing, exact, or exact-partial TestPyPI release"
        )
        self.assertLess(steps.index(public_gate), steps.index(registry_gate))
        self.assertIn("--phase github", public_gate["run"])
        self.assertIn("releases/tags/$RELEASE_TAG", public_gate["run"])
        self.assertIn('"$http_status" == 404', public_gate["run"])
        self.assertIn('"$api_exit" -ne 0', public_gate["run"])
        self.assertIn("testpypi-preflight", jobs["publish-testpypi"]["needs"])

    def test_release_list_selector_finds_drafts_and_rejects_ambiguity(self):
        workflow = (REPO_ROOT / ".github/workflows/publish.yml").read_text(
            encoding="utf-8"
        )
        match = re.search(
            r"# release-list-selector:start\n(?P<code>.*?)"
            r"\n\s*# release-list-selector:end",
            workflow,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        selector = textwrap.dedent(match.group("code"))

        def select(pages):
            with tempfile.TemporaryDirectory() as td:
                releases = Path(td) / "releases.json"
                releases.write_text(json.dumps(pages), encoding="utf-8")
                return subprocess.run(
                    [sys.executable, "-c", selector, "v0.11.0", str(releases)],
                    check=False,
                    capture_output=True,
                    text=True,
                )

        found = select(
            [
                [{"id": 7, "tag_name": "v0.10.0", "draft": False}],
                [{"id": 371554563, "tag_name": "v0.11.0", "draft": True}],
            ]
        )
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(found.stdout.strip(), "371554563")

        missing = select([[{"id": 7, "tag_name": "v0.10.0"}]])
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertEqual(missing.stdout, "")

        duplicate = select(
            [[
                {"id": 11, "tag_name": "v0.11.0"},
                {"id": 12, "tag_name": "v0.11.0"},
            ]]
        )
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("multiple GitHub Releases", duplicate.stderr)

        malformed = select([[{"id": True, "tag_name": "v0.11.0"}]])
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("malformed ID", malformed.stderr)

    def test_publish_promotes_one_artifact_id_through_testpypi(self):
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )
        jobs = workflow["jobs"]
        artifact_id = "${{ needs.verify-release.outputs.python-dist-artifact-id }}"
        upload = next(
            step
            for step in jobs["verify-release"]["steps"]
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        )
        self.assertEqual(upload["id"], "upload-python-dist")
        self.assertEqual(
            jobs["verify-release"]["outputs"]["python-dist-artifact-id"],
            "${{ steps.select-artifacts.outputs.python-dist-artifact-id }}",
        )
        self.assertEqual(
            jobs["verify-release"]["outputs"]["python-dist-artifact-digest"],
            "${{ steps.select-artifacts.outputs.python-dist-artifact-digest }}",
        )
        self.assertIn("dist/*.whl", upload["with"]["path"])
        self.assertIn("dist/*.tar.gz", upload["with"]["path"])
        self.assertNotIn("pyz", upload["with"]["path"])

        consuming_jobs = [
            "testpypi-preflight",
            "publish-testpypi",
            "verify-testpypi",
            "verify-marketplace",
            "pypi-preflight",
            "publish-pypi",
            "verify-pypi",
        ]
        for job_name in consuming_jobs:
            downloads = [
                step
                for step in jobs[job_name]["steps"]
                if str(step.get("uses", "")).startswith(
                    "actions/download-artifact@"
                )
            ]
            matching = [
                item
                for item in downloads
                if item["with"]["artifact-ids"] == artifact_id
            ]
            self.assertEqual(len(matching), 1, job_name)
            self.assertNotIn("name", matching[0]["with"])

        self.assertEqual(jobs["publish-testpypi"]["environment"], "testpypi")
        self.assertEqual(
            jobs["publish-testpypi"]["permissions"]["id-token"], "write"
        )
        test_publish = next(
            step
            for step in jobs["publish-testpypi"]["steps"]
            if str(step.get("uses", "")).startswith(
                "pypa/gh-action-pypi-publish@"
            )
        )
        self.assertEqual(
            test_publish["with"]["repository-url"],
            "https://test.pypi.org/legacy/",
        )
        self.assertIs(test_publish["with"]["skip-existing"], True)
        self.assertEqual(
            test_publish["if"],
            "needs.testpypi-preflight.outputs.upload-required == 'true'",
        )
        self.assertEqual(jobs["verify-marketplace"]["environment"], "marketplace")
        self.assertIn("prepare-release-draft", jobs["verify-marketplace"]["needs"])
        self.assertEqual(jobs["publish-pypi"]["environment"], "pypi")
        self.assertIn("pypi-preflight", jobs["publish-pypi"]["needs"])
        self.assertEqual(
            jobs["publish-pypi"]["permissions"]["id-token"], "write"
        )
        production_publish = next(
            step
            for step in jobs["publish-pypi"]["steps"]
            if str(step.get("uses", "")).startswith(
                "pypa/gh-action-pypi-publish@"
            )
        )
        self.assertNotIn("skip-existing", production_publish["with"])
        self.assertEqual(production_publish["with"]["packages-dir"], "upload-dist")
        self.assertEqual(
            production_publish["if"],
            "steps.fresh-preflight.outputs.upload-required == 'true'",
        )
        fresh = next(
            step
            for step in jobs["publish-pypi"]["steps"]
            if step.get("id") == "fresh-preflight"
        )
        self.assertIn("verify_testpypi_release.py preflight", fresh["run"])
        self.assertIn("https://pypi.org/pypi", fresh["run"])

    def test_publish_recovery_binds_current_main_before_exact_release_checkout(self):
        import yaml

        workflow_text = (
            REPO_ROOT / ".github/workflows/publish.yml"
        ).read_text(encoding="utf-8")
        workflow = yaml.safe_load(workflow_text)
        resume_input = workflow[True]["workflow_dispatch"]["inputs"][
            "resume_run_id"
        ]
        self.assertIs(resume_input["required"], False)
        self.assertEqual(resume_input["default"], "")
        self.assertEqual(resume_input["type"], "string")

        verify = workflow["jobs"]["verify-release"]
        self.assertEqual(verify["permissions"]["actions"], "read")
        self.assertEqual(verify["permissions"]["issues"], "read")
        self.assertEqual(verify["permissions"]["pull-requests"], "read")
        steps = verify["steps"]
        binding = steps[0]["run"]
        self.assertIn('if [[ -z "$RESUME_RUN_ID" ]]', binding)
        self.assertIn('"$DISPATCH_REF_TYPE" != tag', binding)
        self.assertIn('"$DISPATCH_REF" != "refs/tags/$RELEASE_TAG"', binding)
        self.assertIn('"$DISPATCH_SHA" != "$RELEASE_SHA"', binding)
        self.assertIn('"$DISPATCH_REF_TYPE" != branch', binding)
        self.assertIn('"$DISPATCH_REF_NAME" != main', binding)
        self.assertIn('"$DISPATCH_REF" != refs/heads/main', binding)
        self.assertIn("git/ref/heads/main", binding)
        self.assertIn('"$DISPATCH_SHA" != "$remote_main"', binding)

        by_name = {step["name"]: step for step in steps}
        recovery_checkout = by_name["Checkout the recovery control commit"]
        self.assertEqual(recovery_checkout["if"], "inputs.resume_run_id != ''")
        self.assertEqual(recovery_checkout["with"]["ref"], "${{ github.sha }}")
        ci_gate = by_name[
            "Require successful CI for the exact recovery control commit"
        ]
        self.assertEqual(ci_gate["if"], "inputs.resume_run_id != ''")
        self.assertIn("actions/workflows/ci.yml/runs", ci_gate["run"])
        self.assertIn("head_sha=$CONTROL_SHA&event=push", ci_gate["run"])
        self.assertIn('.head_branch == "main"', ci_gate["run"])
        review_gate = by_name[
            "Require current reviews for the recovery control commit"
        ]
        self.assertIn(
            'scripts/audit_release_reviews.sh "$CONTROL_SHA" "$RELEASE_TAG"',
            review_gate["run"],
        )
        rebind = by_name["Rebind recovery to current main after its control audit"]
        self.assertIn('"$current_main" != "$CONTROL_SHA"', rebind["run"])
        release_checkout = by_name["Checkout the released commit"]
        self.assertEqual(release_checkout["with"]["ref"], "${{ inputs.release_sha }}")
        self.assertLess(steps.index(recovery_checkout), steps.index(ci_gate))
        self.assertLess(steps.index(ci_gate), steps.index(review_gate))
        self.assertLess(steps.index(review_gate), steps.index(rebind))
        self.assertLess(steps.index(rebind), steps.index(release_checkout))

        fresh_only = {
            "Test source and exact distributions",
            "Upload verified distributions",
            "Assemble standalone and checksummed GitHub Release assets",
            "Upload verified GitHub Release assets",
        }
        self.assertEqual(
            {
                step["name"]
                for step in steps
                if step.get("if") == "inputs.resume_run_id == ''"
            },
            fresh_only,
        )
        for step in steps:
            if (
                str(step.get("uses", "")).startswith("actions/upload-artifact@")
                or "packaging_smoke.sh" in step.get("run", "")
            ):
                self.assertEqual(step.get("if"), "inputs.resume_run_id == ''")

    def test_publish_recovery_selects_only_exact_unexpired_source_artifacts(self):
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )
        verify = workflow["jobs"]["verify-release"]
        steps = verify["steps"]
        recover = next(step for step in steps if step.get("id") == "recover-artifacts")
        self.assertEqual(recover["if"], "inputs.resume_run_id != ''")
        recovery_query = recover["run"]
        for endpoint in (
            'actions/runs/$RESUME_RUN_ID"',
            "actions/runs/$RESUME_RUN_ID/jobs?filter=all&per_page=100",
            "actions/runs/$RESUME_RUN_ID/artifacts?per_page=100",
        ):
            self.assertIn(endpoint, recovery_query)
        for invariant in (
            '"event": "workflow_dispatch"',
            '"status": "completed"',
            '"conclusion": "failure"',
            '"path": ".github/workflows/publish.yml"',
            '"head_branch": release_tag',
            '"head_sha": release_sha',
            'job.get("name") == "verify-release"',
            'job.get("conclusion") == "success"',
            'verification.get("run_id") != source_run_id',
            'verification.get("head_sha") != release_sha',
            'type(attempt) is not int',
            'type(jobs_total) is not int',
            'type(artifact_attempt) is not int',
            'artifact_attempt > attempt',
            'type(artifacts_total) is not int',
            'artifacts_total != 2',
            'f"verification-job-id={verification[\'id\']}"',
            'f"{release_tag}-{source_run_id}-{artifact_attempt}"',
            'f"python-dist-{suffix}"',
            'f"release-assets-{suffix}"',
            'artifact.get("expired") is not False',
            'artifact["size_in_bytes"] < 1',
            're.fullmatch(r"sha256:[0-9a-f]{64}", digest)',
            'workflow_run.get("id") != source_run_id',
            'expiry <= now',
        ):
            self.assertIn(invariant, recovery_query)

        policy_gate = next(
            step
            for step in steps
            if step["name"] == "Bind recovery policy to the source verification log"
        )
        self.assertEqual(policy_gate["if"], "inputs.resume_run_id != ''")
        self.assertEqual(policy_gate["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertEqual(
            policy_gate["env"]["VERIFICATION_JOB_ID"],
            "${{ steps.recover-artifacts.outputs.verification-job-id }}",
        )
        policy_query = policy_gate["run"]
        for invariant in (
            'if [[ ! "$VERIFICATION_JOB_ID" =~ ^[1-9][0-9]*$ ]]',
            "gh_api_help=$(gh api --help)",
            "gh_api=(gh api)",
            'if [[ "$gh_api_help" == *"--allow-escape-sequences"* ]]',
            "--allow-escape-sequences",
            "actions/jobs/$VERIFICATION_JOB_ID/logs",
            '"RELEASE_TAG", release_tag',
            '"RELEASE_SHA", release_sha',
            '"COMPATIBILITY_ALIAS", compatibility_alias',
            "if not observed:",
            "len(observed) % len(expected) != 0",
            "observed[offset : offset + len(expected)] != expected",
            "does not bind the exact release-policy triple",
        ):
            self.assertIn(invariant, policy_query)

        select = next(step for step in steps if step.get("id") == "select-artifacts")
        self.assertNotIn("if", select)
        self.assertIn('if os.environ["RESUME_RUN_ID"]', select["run"])
        self.assertIn('"source-run-id": os.environ["CURRENT_RUN_ID"]', select["run"])
        self.assertEqual(
            verify["outputs"]["source-run-id"],
            "${{ steps.select-artifacts.outputs.source-run-id }}",
        )
        self.assertLess(steps.index(recover), steps.index(policy_gate))
        self.assertLess(steps.index(policy_gate), steps.index(select))
        for kind in ("python-dist", "release-assets"):
            self.assertEqual(
                verify["outputs"][f"{kind}-artifact-id"],
                f"${{{{ steps.select-artifacts.outputs.{kind}-artifact-id }}}}",
            )
            self.assertEqual(
                verify["outputs"][f"{kind}-artifact-digest"],
                f"${{{{ steps.select-artifacts.outputs.{kind}-artifact-digest }}}}",
            )

    @unittest.skipIf(os.name == "nt", "workflow recovery shell runs on Linux")
    def test_publish_recovery_reuses_artifacts_from_preceding_successful_attempt(self):
        import json
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )
        recover = next(
            step
            for step in workflow["jobs"]["verify-release"]["steps"]
            if step.get("id") == "recover-artifacts"
        )
        source_run_id = 321
        release_tag = "v0.11.0"
        release_sha = "a" * 40
        run = {
            "id": source_run_id,
            "run_attempt": 2,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "failure",
            "path": ".github/workflows/publish.yml",
            "head_branch": release_tag,
            "head_sha": release_sha,
        }
        jobs = {
            "total_count": 2,
            "jobs": [
                {
                    "id": 901,
                    "run_id": source_run_id,
                    "name": "verify-release",
                    "run_attempt": 1,
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": release_sha,
                },
                {
                    "id": 902,
                    "run_id": source_run_id,
                    "name": "Reject conflicting TestPyPI release state",
                    "run_attempt": 2,
                    "status": "completed",
                    "conclusion": "failure",
                    "head_sha": release_sha,
                },
            ],
        }

        def artifact(artifact_id: int, name: str, digest_char: str) -> dict:
            return {
                "id": artifact_id,
                "name": name,
                "expired": False,
                "size_in_bytes": 100,
                "digest": f"sha256:{digest_char * 64}",
                "expires_at": "2099-01-01T00:00:00Z",
                "workflow_run": {
                    "id": source_run_id,
                    "head_branch": release_tag,
                    "head_sha": release_sha,
                },
            }

        suffix = f"{release_tag}-{source_run_id}-1"
        artifacts = {
            "total_count": 2,
            "artifacts": [
                artifact(701, f"python-dist-{suffix}", "b"),
                artifact(702, f"release-assets-{suffix}", "c"),
            ],
        }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            (fixtures / "run.json").write_text(json.dumps(run), encoding="utf-8")
            (fixtures / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
            artifacts_path = fixtures / "artifacts.json"
            artifacts_path.write_text(json.dumps(artifacts), encoding="utf-8")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                """#!/usr/bin/env python3
import os
import pathlib
import sys

endpoint = sys.argv[2]
root = pathlib.Path(os.environ["FAKE_GH_FIXTURES"])
if "/jobs?" in endpoint:
    fixture = "jobs.json"
elif "/artifacts?" in endpoint:
    fixture = "artifacts.json"
else:
    fixture = "run.json"
sys.stdout.write((root / fixture).read_text(encoding="utf-8"))
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            output = root / "github-output"
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
                    "FAKE_GH_FIXTURES": str(fixtures),
                    "GH_TOKEN": "test-token",
                    "GITHUB_REPOSITORY": "yzm1/boundver",
                    "GITHUB_OUTPUT": str(output),
                    "RESUME_RUN_ID": str(source_run_id),
                    "RELEASE_TAG": release_tag,
                    "RELEASE_SHA": release_sha,
                }
            )
            result = subprocess.run(
                ["bash", "-c", recover["run"]],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            selected = dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(selected["source-run-id"], str(source_run_id))
            self.assertEqual(selected["verification-job-id"], "901")
            self.assertEqual(selected["python-dist-artifact-id"], "701")
            self.assertEqual(selected["release-assets-artifact-id"], "702")

            rerun_all_artifacts = artifacts["artifacts"] + [
                artifact(
                    703,
                    f"python-dist-{release_tag}-{source_run_id}-2",
                    "d",
                ),
                artifact(
                    704,
                    f"release-assets-{release_tag}-{source_run_id}-2",
                    "e",
                ),
            ]
            artifacts_path.write_text(
                json.dumps(
                    {
                        "total_count": len(rerun_all_artifacts),
                        "artifacts": rerun_all_artifacts,
                    }
                ),
                encoding="utf-8",
            )
            output.write_text("", encoding="utf-8")
            result = subprocess.run(
                ["bash", "-c", recover["run"]],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exactly two artifacts", result.stderr)

    @unittest.skipIf(os.name == "nt", "workflow recovery shell runs on Linux")
    def test_publish_recovery_rejects_unbound_or_spoofed_source_policy_logs(self):
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )
        policy_gate = next(
            step
            for step in workflow["jobs"]["verify-release"]["steps"]
            if step["name"] == "Bind recovery policy to the source verification log"
        )
        release_tag = "v0.11.0"
        release_sha = "a" * 40
        compatibility_alias = "v0.11"
        timestamp = "2026-08-14T09:20:21.1480329Z"

        def log_triple(tag: str, sha: str, alias: str) -> str:
            return "\n".join(
                [
                    f"{timestamp}   RELEASE_TAG: {tag}",
                    f"{timestamp}   RELEASE_SHA: {sha}",
                    f"{timestamp}   COMPATIBILITY_ALIAS: {alias}",
                ]
            ) + "\n"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            log_path = root / "verification.log"
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                """#!/usr/bin/env python3
import os
import pathlib
import sys

arguments = sys.argv[1:]
if arguments == ["api", "--help"]:
    if os.environ.get("FAKE_GH_SUPPORTS_ESCAPE") == "1":
        print("--allow-escape-sequences")
    raise SystemExit(0)
expected = ["api"]
if os.environ.get("FAKE_GH_SUPPORTS_ESCAPE") == "1":
    expected.append("--allow-escape-sequences")
expected.append("repos/yzm1/boundver/actions/jobs/901/logs")
if arguments != expected:
    raise SystemExit(f"unexpected gh invocation: {sys.argv[1:]!r}")
sys.stdout.write(
    pathlib.Path(os.environ["FAKE_VERIFICATION_LOG"]).read_text(encoding="utf-8")
)
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
                    "FAKE_VERIFICATION_LOG": str(log_path),
                    "GH_TOKEN": "test-token",
                    "GITHUB_REPOSITORY": "yzm1/boundver",
                    "RELEASE_TAG": release_tag,
                    "RELEASE_SHA": release_sha,
                    "COMPATIBILITY_ALIAS": compatibility_alias,
                    "VERIFICATION_JOB_ID": "901",
                }
            )

            def run_policy_gate(
                log: str,
                *,
                job_id: str = "901",
                expected_alias: str = compatibility_alias,
            ):
                log_path.write_text(log, encoding="utf-8")
                run_env = env.copy()
                run_env["VERIFICATION_JOB_ID"] = job_id
                run_env["COMPATIBILITY_ALIAS"] = expected_alias
                return subprocess.run(
                    ["bash", "-c", policy_gate["run"]],
                    cwd=root,
                    env=run_env,
                    capture_output=True,
                    text=True,
                )

            exact_log = log_triple(
                release_tag, release_sha, compatibility_alias
            ) * 2
            result = run_policy_gate(exact_log)
            self.assertEqual(result.returncode, 0, result.stderr)
            env["FAKE_GH_SUPPORTS_ESCAPE"] = "1"
            result = run_policy_gate(exact_log)
            self.assertEqual(result.returncode, 0, result.stderr)
            none_log = log_triple(release_tag, release_sha, "none")
            result = run_policy_gate(none_log, expected_alias="none")
            self.assertEqual(result.returncode, 0, result.stderr)

            cases = {
                "alias mismatch": (
                    log_triple(release_tag, release_sha, "none"),
                    "does not bind the exact release-policy triple",
                ),
                "alternate spoofed triple": (
                    exact_log + log_triple(release_tag, release_sha, "none"),
                    "does not bind the exact release-policy triple",
                ),
                "missing alias": (
                    "\n".join(exact_log.splitlines()[:2]) + "\n",
                    "incomplete or spoofed release-policy triple",
                ),
                "missing triple": (
                    f"{timestamp} unrelated output\n",
                    "has no release-policy environment triple",
                ),
            }
            for label, (log, error) in cases.items():
                with self.subTest(label=label):
                    result = run_policy_gate(log)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(error, result.stderr)

            result = run_policy_gate(exact_log, job_id="0")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be a positive integer", result.stderr)

    def test_publish_recovery_fails_closed_on_archive_or_payload_mismatch(self):
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )
        steps = workflow["jobs"]["verify-release"]["steps"]
        recovery_downloads = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("actions/download-artifact@")
        ]
        self.assertEqual(len(recovery_downloads), 2)
        for download in recovery_downloads:
            self.assertEqual(download["if"], "inputs.resume_run_id != ''")
            self.assertEqual(
                download["with"]["github-token"], "${{ github.token }}"
            )
            self.assertEqual(
                download["with"]["repository"], "${{ github.repository }}"
            )
            self.assertEqual(
                download["with"]["run-id"],
                "${{ steps.select-artifacts.outputs.source-run-id }}",
            )
            self.assertIs(download["with"]["merge-multiple"], True)
            self.assertIn(
                download["with"]["path"],
                {"recovered-python-dist", "recovered-release-assets"},
            )

        archive_gate = next(
            step
            for step in steps
            if step["name"] == "Fail on any recovered artifact archive digest mismatch"
        )["run"]
        for check in (
            "actions/artifacts/$artifact_id/zip",
            'actual_digest="sha256:$(sha256sum',
            '"$actual_digest" != "$expected_digest"',
            "artifact archive is not a unique flat file set",
            "download action output disagrees with artifact archive",
            "download action changed artifact bytes",
        ):
            self.assertIn(check, archive_gate)

        payload_gate = next(
            step
            for step in steps
            if step["name"] == "Validate the exact recovered release payload"
        )["run"]
        for check in (
            'require_exact("recovered-python-dist", [wheel, sdist])',
            'require_exact("recovered-release-assets", '
            '[wheel, sdist, pyz, "SHA256SUMS"])',
            "SHA256SUMS does not cover the exact release payload",
            "sha256sum --check --strict SHA256SUMS",
            "cmp --silent",
            "python3 -m twine check",
        ):
            self.assertIn(check, payload_gate)
        self.assertEqual(payload_gate.count("cmp --silent"), 2)

    def test_every_downstream_artifact_download_uses_selected_source_run(self):
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )
        jobs = workflow["jobs"]
        downstream = []
        for job_name, job in jobs.items():
            if job_name == "verify-release":
                continue
            downloads = [
                step
                for step in job.get("steps", [])
                if str(step.get("uses", "")).startswith(
                    "actions/download-artifact@"
                )
            ]
            if not downloads:
                continue
            self.assertEqual(job["permissions"]["actions"], "read", job_name)
            for download in downloads:
                options = download["with"]
                self.assertEqual(
                    options["run-id"],
                    "${{ needs.verify-release.outputs.source-run-id }}",
                    job_name,
                )
                self.assertEqual(
                    options["github-token"], "${{ github.token }}", job_name
                )
                self.assertEqual(
                    options["repository"], "${{ github.repository }}", job_name
                )
                self.assertIs(options["merge-multiple"], True, job_name)
                self.assertIn(options["path"], {"dist", "release-assets"})
                self.assertNotIn("name", options)
                self.assertIn(
                    options["artifact-ids"],
                    {
                        "${{ needs.verify-release.outputs.python-dist-artifact-id }}",
                        "${{ needs.verify-release.outputs."
                        "release-assets-artifact-id }}",
                    },
                )
                downstream.append((job_name, download))
        self.assertEqual(len(downstream), 12)
        self.assertEqual(jobs["publish-testpypi"]["environment"], "testpypi")
        self.assertEqual(jobs["publish-testpypi"]["permissions"]["id-token"], "write")
        self.assertEqual(jobs["verify-marketplace"]["environment"], "marketplace")
        self.assertEqual(jobs["publish-pypi"]["environment"], "pypi")
        self.assertEqual(jobs["publish-pypi"]["permissions"]["id-token"], "write")

    def test_testpypi_install_cannot_resolve_boundver_from_pypi(self):
        workflow = (REPO_ROOT / ".github/workflows/publish.yml").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(
            workflow.count("scripts/verify_testpypi_release.py"), 3
        )
        self.assertIn("https://test-files.pythonhosted.org/*#sha256=*", workflow)
        self.assertIn('--no-index --no-deps "$TESTPYPI_WHEEL_URL"', workflow)
        self.assertIn('PIP_NO_INDEX: "1"', workflow)
        self.assertIn("Reverify TestPyPI candidate", workflow)
        self.assertIn("https://files.pythonhosted.org/*#sha256=*", workflow)
        self.assertIn("pypi-attestations verify pypi", workflow)

    def test_release_candidate_is_tested_before_tag_job(self):
        import yaml

        workflow = (
            REPO_ROOT / ".github/workflows/create-release-tag.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("verify-candidate:", workflow)
        self.assertIn("needs: verify-candidate", workflow)
        self.assertIn("pytest -q", workflow)
        self.assertIn("bash scripts/packaging_smoke.sh", workflow)
        self.assertIn("uses: ./", workflow)
        self.assertIn("No successful CI push run found", workflow)
        self.assertIn("Require completed reviews since the previous release", workflow)
        self.assertGreaterEqual(
            workflow.count("scripts/audit_release_reviews.sh"), 2
        )
        self.assertIn("Main advanced before the final review audit", workflow)
        self.assertGreaterEqual(
            workflow.count("scripts/release_changelog.py"), 2
        )
        self.assertLess(
            workflow.rindex("scripts/audit_release_reviews.sh"),
            workflow.index('git push origin "refs/tags/$RELEASE_TAG"'),
        )
        final_audit = workflow.rindex("scripts/audit_release_reviews.sh")
        final_main_check = workflow.index(
            "Main advanced at the tag mutation boundary", final_audit
        )
        tag_mutation = workflow.index('git tag "$RELEASE_TAG" "$RELEASE_SHA"')
        self.assertLess(final_audit, final_main_check)
        self.assertLess(final_main_check, tag_mutation)
        self.assertIn(
            "Another release promotion became active", workflow[final_audit:tag_mutation]
        )
        self.assertIn('--ref "$RELEASE_TAG"', workflow)
        self.assertLess(workflow.index("verify-candidate:"), workflow.index("\n  tag:"))
        jobs = yaml.safe_load(workflow)["jobs"]
        for job_name in ("verify-candidate", "tag"):
            self.assertEqual(jobs[job_name]["permissions"]["issues"], "read")
            self.assertEqual(
                jobs[job_name]["permissions"]["pull-requests"], "read"
            )

    def test_action_and_container_install_complete_public_extras(self):
        action = (REPO_ROOT / "action.yml").read_text(encoding="utf-8")
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("[schema,yaml]", action)
        self.assertIn(".[schema,yaml]", dockerfile)
        self.assertNotIn("--no-deps", dockerfile)
        self.assertIn("git config --system --add safe.directory /repo", dockerfile)
        self.assertRegex(dockerfile, r"(?m)^USER\s+boundver\s*$")

    def test_container_and_sdist_exclude_repository_only_material(self):
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
        manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        smoke = (REPO_ROOT / "scripts/packaging_smoke.sh").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(dockerfile.count("FROM python:3.12-slim"), 2)
        self.assertNotIn("COPY . .", dockerfile)
        self.assertIn("COPY src ./src", dockerfile)
        for ignored in ("tests/", "scripts/", ".github/"):
            self.assertIn(ignored, dockerignore)
        self.assertNotIn("recursive-include tests", manifest)
        self.assertIn("exclude docs/PROJECT_REVIEW.md", manifest)
        self.assertIn("exclude docs/RELEASING.md", manifest)
        for excluded in ("tests", "scripts", ".github", "Dockerfile", "action.yml"):
            self.assertIn(f'sdist contains repository-only material', smoke)
            self.assertIn(excluded, smoke)

    def test_ci_executes_docker_and_both_published_pre_commit_hooks(self):
        import yaml

        jobs = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        )["jobs"]
        public = jobs["public-installations"]
        script = "\n".join(
            str(step.get("run", "")) for step in public["steps"]
        )
        self.assertIn("pre-commit try-repo . boundver-verify --all-files", script)
        self.assertIn("boundver-verify-push", script)
        self.assertIn("--hook-stage pre-push", script)
        self.assertIn("docker build", script)
        self.assertIn("verify --source head --facets exact", script)

    def test_compatibility_alias_update_is_monotonic_ancestral_and_leased(self):
        workflow = (REPO_ROOT / ".github/workflows/publish.yml").read_text(
            encoding="utf-8"
        )
        ancestry = workflow.index("merge-base --is-ancestor")
        monotonic = workflow.index("refusing compatibility alias rollback")
        mutation = workflow.index('git push "$lease" origin')
        self.assertLess(ancestry, mutation)
        self.assertLess(monotonic, mutation)
        self.assertIn('--force-with-lease=$alias_ref:$expected_current', workflow)
        self.assertIn('--force-with-lease=$alias_ref:', workflow)
        self.assertIn("expected_current=$(git ls-remote", workflow)
        self.assertNotRegex(workflow, r"git push[^\n]*\s--force(?:\s|$)")

    def test_action_honors_configured_facet_policy_by_default(self):
        import yaml

        action = yaml.safe_load((REPO_ROOT / "action.yml").read_text(encoding="utf-8"))
        self.assertEqual(action["inputs"]["facets"]["default"], "")
        self.assertEqual(action["inputs"]["transitive"]["default"], "false")
        script = action["runs"]["steps"][-1]["run"]
        self.assertIn('if [[ -n "$BOUNDVER_FACETS" ]]', script)
        self.assertIn('command+=(--facets "$BOUNDVER_FACETS")', script)
        self.assertIn('command+=(--transitive)', script)

    def test_pre_commit_and_pre_push_use_matching_snapshots_and_portable_exact_gate(self):
        import yaml

        hooks = yaml.safe_load(
            (REPO_ROOT / ".pre-commit-hooks.yaml").read_text(encoding="utf-8")
        )
        by_id = {hook["id"]: hook for hook in hooks}
        pre_commit = by_id["boundver-verify"]
        pre_push = by_id["boundver-verify-push"]
        self.assertEqual(pre_commit["stages"], ["pre-commit"])
        self.assertEqual(pre_push["stages"], ["pre-push"])
        self.assertIn(
            "boundver generate --source index", pre_commit["description"]
        )
        self.assertEqual(pre_commit["args"][:3], ["verify", "--source", "index"])
        self.assertEqual(pre_push["args"][:3], ["verify", "--source", "head"])
        for hook in (pre_commit, pre_push):
            self.assertIn("exact", hook["args"])
            self.assertNotIn("exact,behavior,boundary,compat", hook["args"])
            self.assertIn("jsonschema>=4", hook["additional_dependencies"])
            self.assertIn("PyYAML>=6", hook["additional_dependencies"])


class TestPyPIReleaseVerificationTests(unittest.TestCase):
    def setUp(self):
        self.verifier = _load_testpypi_verifier()

    def _candidate(self, root: Path):
        dist = root / "dist"
        _write_fake_distributions(dist)
        return dist, self.verifier._load_candidate(dist, "boundver", "0.11.0")

    def _remote(self, candidate):
        return {
            filename: self.verifier.DistributionFile(
                filename=filename,
                sha256=item.sha256,
                size=item.size,
                url=f"https://test-files.pythonhosted.org/packages/{filename}",
            )
            for filename, item in candidate.items()
        }

    def test_candidate_requires_exact_wheel_and_sdist_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            dist, candidate = self._candidate(Path(td))
            self.assertEqual(len(candidate), 2)
            (dist / "unexpected.txt").write_text("not publishable", encoding="utf-8")
            with self.assertRaisesRegex(
                self.verifier.ReleaseVerificationError,
                "exactly one wheel and one",
            ):
                self.verifier._load_candidate(dist, "boundver", "0.11.0")

    def test_remote_release_must_be_exact_or_an_exact_subset(self):
        with tempfile.TemporaryDirectory() as td:
            _, candidate = self._candidate(Path(td))
            remote = self._remote(candidate)
            self.assertTrue(self.verifier._compare_release(candidate, remote))
            partial = {next(iter(remote)): next(iter(remote.values()))}
            self.assertFalse(self.verifier._compare_release(candidate, partial))

            first_name = next(iter(remote))
            first = remote[first_name]
            conflicting = dict(remote)
            conflicting[first_name] = self.verifier.DistributionFile(
                first.filename,
                "0" * 64,
                first.size,
                first.url,
            )
            with self.assertRaisesRegex(
                self.verifier.ReleaseVerificationError,
                "does not match the candidate",
            ):
                self.verifier._compare_release(candidate, conflicting)

            unexpected = dict(remote)
            unexpected["extra.whl"] = self.verifier.DistributionFile(
                "extra.whl", "0" * 64, 0, "https://example.invalid/extra.whl"
            )
            with self.assertRaisesRegex(
                self.verifier.ReleaseVerificationError, "unexpected files"
            ):
                self.verifier._compare_release(candidate, unexpected)

    def test_preflight_is_idempotent_only_for_identical_existing_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dist, candidate = self._candidate(root)
            remote = self._remote(candidate)
            output = root / "github-output"
            args = Namespace(
                dist=dist,
                project="boundver",
                version="0.11.0",
                api_base=self.verifier.DEFAULT_API_BASE,
                download_origin=self.verifier.DEFAULT_DOWNLOAD_ORIGIN,
                github_output=output,
            )
            with mock.patch.object(
                self.verifier, "_query_release", return_value=remote
            ), mock.patch.object(self.verifier, "_download_and_verify"):
                self.verifier._preflight(args)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "upload-required=false\nmissing-files=\n",
            )

            output.write_text("", encoding="utf-8")
            partial = {next(iter(remote)): next(iter(remote.values()))}
            with mock.patch.object(
                self.verifier, "_query_release", return_value=partial
            ), mock.patch.object(self.verifier, "_download_and_verify"):
                self.verifier._preflight(args)
            self.assertRegex(
                output.read_text(encoding="utf-8"),
                r"^upload-required=true\nmissing-files=boundver-0\.11\.0\.(?:tar\.gz|.+\.whl)\n$",
            )

    def test_verifier_emits_only_hash_pinned_testpypi_wheel_url(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dist, candidate = self._candidate(root)
            remote = self._remote(candidate)
            output = root / "github-output"
            args = Namespace(
                dist=dist,
                project="boundver",
                version="0.11.0",
                api_base=self.verifier.DEFAULT_API_BASE,
                download_origin=self.verifier.DEFAULT_DOWNLOAD_ORIGIN,
                github_output=output,
                attempts=1,
                delay_seconds=0,
            )
            with mock.patch.object(
                self.verifier, "_query_release", return_value=remote
            ), mock.patch.object(self.verifier, "_download_and_verify"):
                self.verifier._verify(args)
            value = output.read_text(encoding="utf-8")
            self.assertRegex(
                value,
                r"(?m)^wheel-url=https://test-files\.pythonhosted\.org/.+"
                r"#sha256=[0-9a-f]{64}$",
            )
            self.assertRegex(
                value,
                r"(?m)^sdist-url=https://test-files\.pythonhosted\.org/.+"
                r"#sha256=[0-9a-f]{64}$",
            )
            with self.assertRaisesRegex(
                self.verifier.ReleaseVerificationError,
                "outside https://test-files.pythonhosted.org",
            ):
                self.verifier._validate_download_url(
                    "https://files.pythonhosted.org/boundver.whl",
                    self.verifier.DEFAULT_DOWNLOAD_ORIGIN,
                )


class ReleaseChangelogTests(unittest.TestCase):
    def _run(self, changelog: str, tag: str):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "CHANGELOG.md"
            output = Path(td) / "notes.md"
            path.write_text(changelog, encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "release_changelog.py"),
                    "--tag",
                    tag,
                    "--changelog",
                    str(path),
                    "--output",
                    str(output),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
            return result, output.read_text(encoding="utf-8") if output.exists() else None

    def test_extracts_exact_newest_release_section(self):
        result, notes = self._run(_release_changelog("0.11.0"), "v0.11.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(notes, "- Shipped safely.\n")

    def test_unreleased_only_version_intent_cannot_be_tagged(self):
        changelog = (
            "# Changelog\n\n## [Unreleased]\n\n"
            "These changes target 0.11.0.\n\n"
            "## [0.10.0] - 2026-08-11\n\n- Old.\n\n"
            "[0.10.0]: https://example.invalid/v0.10.0\n"
        )
        result, notes = self._run(changelog, "v0.11.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(notes)
        self.assertIn("release section for 0.11.0", result.stderr)

    def test_version_section_must_be_newest_and_nonempty(self):
        stale = _release_changelog("0.10.1").replace(
            "## [0.10.1] - 2026-08-12\n\n- Shipped safely.\n\n",
            "## [0.11.0] - 2026-08-12\n\n- Newer.\n\n"
            "## [0.10.1] - 2026-08-11\n\n- Shipped safely.\n\n",
        )
        result, _ = self._run(stale, "v0.10.1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("newest release is 0.11.0", result.stderr)

        result, _ = self._run(
            _release_changelog("0.11.0", notes="No changes yet.\n"),
            "v0.11.0",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is empty", result.stderr)

    def test_release_heading_and_date_must_be_exact(self):
        malformed = _release_changelog("0.11.0").replace(
            "## [0.11.0] - 2026-08-12",
            "## [0.11.0] upcoming",
        )
        result, _ = self._run(malformed, "v0.11.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must include an ISO date", result.stderr)

        impossible_date = _release_changelog("0.11.0").replace(
            "2026-08-12", "2026-99-99", 1
        )
        result, _ = self._run(impossible_date, "v0.11.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid ISO date", result.stderr)

    def test_release_and_unreleased_compare_links_are_exact(self):
        changelog = _release_changelog("0.11.0").replace(
            "https://github.com/yzm1/boundver/compare/v0.10.0...v0.11.0",
            "https://example.invalid/v0.11.0",
        )
        result, _ = self._run(changelog, "v0.11.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("previous release", result.stderr)

        changelog = _release_changelog("0.11.0").replace(
            "https://github.com/yzm1/boundver/compare/v0.11.0...HEAD",
            "https://example.invalid/HEAD",
        )
        result, _ = self._run(changelog, "v0.11.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unreleased link", result.stderr)


class ReleaseReviewAuditTests(unittest.TestCase):
    def _codex_comment(self, commit: str, *, duplicate: bool = False) -> str:
        marker = f"**Reviewed commit:** `{commit}`"
        lines = ["Codex Review: Didn't find any issues.", "", marker]
        if duplicate:
            lines.extend(("", marker))
        lines.extend(("", "<sub>About Codex reviews</sub>"))
        return "\n".join(lines)

    def _comment_record(
        self,
        body: str,
        *,
        actor_id: str = "199175422",
        login: str = "chatgpt-codex-connector[bot]",
        actor_type: str = "Bot",
    ) -> str:
        encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
        return f"{actor_id}|{login}|{actor_type}|{encoded}"

    @unittest.skipIf(os.name == "nt", "release audit runs on Linux")
    def _run_audit(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        script = (REPO_ROOT / "scripts" / "audit_release_reviews.sh").read_text(
            encoding="utf-8"
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "audit_release_reviews.sh").write_bytes(
                script.encode("utf-8")
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_gh = bin_dir / "gh"
            fake_gh.write_bytes(
                """#!/usr/bin/env bash
set -euo pipefail
args=" $* "
endpoint=
for argument in "$@"; do
  if [[ "$argument" == repos/* ]]; then endpoint=$argument; fi
done
if [[ "$endpoint" == */commits/*/pulls ]]; then
  if [[ "$FAKE_FAILURE" == associated ]]; then exit 73; fi
  echo 17
  exit 0
fi
if [[ "$endpoint" == "repos/$GITHUB_REPOSITORY" ]]; then
  if [[ "$FAKE_FAILURE" == owner ]]; then exit 73; fi
  echo "$FAKE_OWNER_ID|$FAKE_OWNER_LOGIN|$FAKE_OWNER_TYPE"
  exit 0
fi
if [[ "$endpoint" == */pulls/17 ]]; then
  if [[ "$FAKE_FAILURE" == metadata ]]; then exit 73; fi
  echo "$FAKE_AUTHOR_ID|$FAKE_AUTHOR_LOGIN|$FAKE_AUTHOR_TYPE|$FAKE_HEAD_SHA|$FAKE_MERGE_SHA|$FAKE_PENDING_REVIEWERS|$FAKE_PENDING_TEAMS"
  exit 0
fi
if [[ "$args" == *" graphql "* && "$args" != *" --paginate "* ]]; then
  if [[ "$FAKE_FAILURE" == decision ]]; then exit 73; fi
  printf '%s' "$FAKE_DECISION"
  exit 0
fi
if [[ "$args" == *" graphql "* && "$args" == *" --paginate "* ]]; then
  if [[ "$FAKE_FAILURE" == threads ]]; then exit 73; fi
  printf '%s' "$FAKE_UNRESOLVED"
  exit 0
fi
if [[ "$endpoint" == */pulls/17/reviews* ]]; then
  if [[ "$FAKE_FAILURE" == reviews ]]; then exit 73; fi
  printf '%s' "$FAKE_REVIEWS"
  exit 0
fi
if [[ "$endpoint" == */issues/17/comments* ]]; then
  if [[ "$FAKE_FAILURE" == comments ]]; then exit 73; fi
  printf '%s' "$FAKE_COMMENTS"
  exit 0
fi
if [[ "$endpoint" == */collaborators/*/permission ]]; then
  if [[ "$FAKE_FAILURE" == permission ]]; then exit 73; fi
  if [[ "$FAKE_DRAIN_PERMISSION_STDIN" == 1 ]]; then cat >/dev/null; fi
  printf '%s' "$FAKE_PERMISSION"
  exit 0
fi
if [[ "$endpoint" == */commits/* ]]; then
  if [[ "$FAKE_FAILURE" == resolve ]]; then exit 73; fi
  if [[ "$FAKE_DRAIN_RESOLVE_STDIN" == 1 ]]; then cat >/dev/null; fi
  candidate=${endpoint##*/commits/}
  if [[ "$FAKE_RESOLVE_SHA" != AUTO ]]; then
    echo "$FAKE_RESOLVE_SHA"
  elif [[ "$FAKE_HEAD_SHA" == "$candidate"* ]]; then
    echo "$FAKE_HEAD_SHA"
  elif [[ -n "$FAKE_MERGE_SHA" && "$FAKE_MERGE_SHA" == "$candidate"* ]]; then
    echo "$FAKE_MERGE_SHA"
  elif [[ -n "$FAKE_STALE_SHA" && "$FAKE_STALE_SHA" == "$candidate"* ]]; then
    echo "$FAKE_STALE_SHA"
  elif [[ "$candidate" =~ ^[0-9a-f]{40}$ ]]; then
    echo "$candidate"
  else
    exit 75
  fi
  exit 0
fi
exit 74
""".encode("utf-8")
            )
            fake_gh.chmod(0o755)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Release Test"],
                cwd=root,
                check=True,
            )
            (root / "file.txt").write_text("release\n", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "release"], cwd=root, check=True)
            release_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            subprocess.run(
                ["git", "remote", "add", "origin", str(root)],
                cwd=root,
                check=True,
            )
            environment = dict(os.environ)
            for name in tuple(environment):
                if name.startswith("FAKE_"):
                    del environment[name]
            environment.update(
                {
                    "PATH": f"{bin_dir}{os.pathsep}{environment['PATH']}",
                    "GH_TOKEN": "test-token",
                    "GITHUB_REPOSITORY": "owner/repository",
                    "FAKE_FAILURE": "none",
                    "FAKE_OWNER_ID": "101",
                    "FAKE_OWNER_LOGIN": "owner",
                    "FAKE_OWNER_TYPE": "User",
                    "FAKE_AUTHOR_ID": "101",
                    "FAKE_AUTHOR_LOGIN": "owner",
                    "FAKE_AUTHOR_TYPE": "User",
                    "FAKE_HEAD_SHA": release_sha,
                    "FAKE_MERGE_SHA": "",
                    "FAKE_PENDING_REVIEWERS": "0",
                    "FAKE_PENDING_TEAMS": "0",
                    "FAKE_DECISION": "REVIEW_REQUIRED",
                    "FAKE_UNRESOLVED": "0",
                    "FAKE_REVIEWS": (
                        "COMMENTED|199175422|chatgpt-codex-connector[bot]|"
                        f"Bot|{release_sha}"
                    ),
                    "FAKE_COMMENTS": "",
                    "FAKE_PERMISSION": "write",
                    "FAKE_RESOLVE_SHA": "AUTO",
                    "FAKE_STALE_SHA": "",
                    "FAKE_DRAIN_PERMISSION_STDIN": "0",
                    "FAKE_DRAIN_RESOLVE_STDIN": "0",
                }
            )
            environment.update(overrides)
            return subprocess.run(
                ["bash", "./audit_release_reviews.sh", release_sha, "v0.11.0"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
            )

    @unittest.skipIf(os.name == "nt", "release audit runs on Linux")
    def test_owner_authored_pr_accepts_exact_codex_review_or_clean_comment(self):
        result = self._run_audit()
        self.assertEqual(result.returncode, 0, result.stderr)

        head = "1" * 40
        comment = self._comment_record(self._codex_comment(head[:10]))
        result = self._run_audit(
            FAKE_HEAD_SHA=head,
            FAKE_REVIEWS="",
            FAKE_COMMENTS=comment,
            FAKE_RESOLVE_SHA=head,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        merge = "7" * 40
        comment = self._comment_record(self._codex_comment(merge[:10]))
        result = self._run_audit(
            FAKE_HEAD_SHA=head,
            FAKE_MERGE_SHA=merge,
            FAKE_REVIEWS="",
            FAKE_COMMENTS=comment,
            FAKE_RESOLVE_SHA=merge,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipIf(os.name == "nt", "release audit runs on Linux")
    def test_human_approval_requires_non_author_push_access_and_current_commit(self):
        head = "2" * 40
        approval = f"APPROVED|202|reviewer|User|{head[:12]}"
        result = self._run_audit(
            FAKE_HEAD_SHA=head,
            FAKE_REVIEWS=approval,
            FAKE_RESOLVE_SHA=head,
            FAKE_DECISION="APPROVED",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        for overrides in (
            {"FAKE_REVIEWS": f"APPROVED|101|owner|User|{head}"},
            {"FAKE_REVIEWS": approval, "FAKE_PERMISSION": "read"},
            {
                "FAKE_REVIEWS": f"APPROVED|202|reviewer|User|{'3' * 40}",
                "FAKE_RESOLVE_SHA": "3" * 40,
            },
        ):
            with self.subTest(overrides=overrides):
                result = self._run_audit(
                    FAKE_HEAD_SHA=head,
                    FAKE_DECISION="APPROVED",
                    **overrides,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("no current exact-commit review evidence", result.stderr)

    @unittest.skipIf(os.name == "nt", "release audit runs on Linux")
    def test_codex_evidence_rejects_spoofed_stale_or_ambiguous_markers(self):
        head = "4" * 40
        stale = "5" * 40
        cases = (
            {
                "FAKE_REVIEWS": (
                    f"COMMENTED|999|chatgpt-codex-connector[bot]|Bot|{head}"
                )
            },
            {
                "FAKE_REVIEWS": (
                    f"COMMENTED|199175422|chatgpt-codex-connector[bot]|Bot|{stale}"
                ),
                "FAKE_RESOLVE_SHA": stale,
            },
            {
                "FAKE_REVIEWS": "",
                "FAKE_COMMENTS": self._comment_record(
                    self._codex_comment(head[:10]), actor_id="999"
                ),
                "FAKE_RESOLVE_SHA": head,
            },
            {
                "FAKE_REVIEWS": "",
                "FAKE_COMMENTS": self._comment_record(
                    self._codex_comment(stale[:10])
                ),
                "FAKE_RESOLVE_SHA": stale,
            },
            {
                "FAKE_REVIEWS": "",
                "FAKE_COMMENTS": self._comment_record(
                    self._codex_comment(head[:10], duplicate=True)
                ),
                "FAKE_RESOLVE_SHA": head,
            },
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                result = self._run_audit(FAKE_HEAD_SHA=head, **overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("no current exact-commit review evidence", result.stderr)

    @unittest.skipIf(os.name == "nt", "release audit runs on Linux")
    def test_nested_gh_calls_cannot_consume_later_review_evidence(self):
        head = "8" * 40
        stale = "9" * 40
        comments = "\n".join(
            (
                self._comment_record(self._codex_comment(stale[:10])),
                self._comment_record(self._codex_comment(head[:10])),
            )
        )
        result = self._run_audit(
            FAKE_HEAD_SHA=head,
            FAKE_REVIEWS="",
            FAKE_COMMENTS=comments,
            FAKE_STALE_SHA=stale,
            FAKE_DRAIN_RESOLVE_STDIN="1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        reviews = "\n".join(
            (
                f"APPROVED|202|reviewer|User|{head}",
                (
                    "COMMENTED|199175422|chatgpt-codex-connector[bot]|"
                    f"Bot|{head}"
                ),
            )
        )
        result = self._run_audit(
            FAKE_HEAD_SHA=head,
            FAKE_REVIEWS=reviews,
            FAKE_PERMISSION="read",
            FAKE_DRAIN_PERMISSION_STDIN="1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipIf(os.name == "nt", "release audit runs on Linux")
    def test_review_gate_fails_closed_on_api_pagination_and_blocking_state(self):
        script = (REPO_ROOT / "scripts" / "audit_release_reviews.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("< <(\n    gh api", script)
        self.assertIn("if ! reviews_output=$(gh api --paginate", script)
        self.assertIn("if ! comments_output=$(gh api --paginate", script)
        self.assertIn("if ! unresolved_output=$(gh api graphql --paginate", script)
        self.assertIn("trusted_codex_bot_id=199175422", script)

        for failure in (
            "associated",
            "owner",
            "metadata",
            "decision",
            "threads",
            "reviews",
            "comments",
            "resolve",
        ):
            with self.subTest(failure=failure):
                result = self._run_audit(FAKE_FAILURE=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("GitHub API failed", result.stderr)

        human = "APPROVED|202|reviewer|User|6" + "6" * 39
        result = self._run_audit(
            FAKE_FAILURE="permission",
            FAKE_HEAD_SHA="6" * 40,
            FAKE_REVIEWS=human,
            FAKE_DECISION="APPROVED",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GitHub API failed", result.stderr)

        for overrides, message in (
            ({"FAKE_DECISION": "CHANGES_REQUESTED"}, "CHANGES_REQUESTED"),
            ({"FAKE_UNRESOLVED": "1"}, "unresolvedThreads=1"),
            ({"FAKE_PENDING_REVIEWERS": "1"}, "pending human review"),
            ({"FAKE_UNRESOLVED": ""}, "no review-thread page"),
        ):
            with self.subTest(overrides=overrides):
                result = self._run_audit(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
