"""Focused tests for shipped installation and release interfaces."""

from __future__ import annotations

import ast
import base64
import email.parser
import importlib.util
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock
from zipfile import ZipFile

import yaml

from tests._project_metadata import CURRENT_TAG, CURRENT_VERSION


REPO_ROOT = Path(__file__).resolve().parents[1]


def _inline_toml_helpers(workflow_path: Path, job_name: str, step_name: str) -> dict:
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    step = next(
        item
        for item in workflow["jobs"][job_name]["steps"]
        if item.get("name") == step_name
    )
    source = step["run"].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    tree = ast.parse(source)
    selected = []
    assignments = {"MAX_TOML_INTEGER_DIGITS"}
    functions = {"toml_has_oversized_numeric_token", "strict_toml_loads"}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import) and any(
                alias.name == "tomllib" for alias in node.names
            ):
                continue
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in assignments
            for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in functions:
            selected.append(node)
    try:
        toml_parser = __import__("tomllib")
    except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10
        toml_parser = __import__("tomli")
    namespace: dict = {"tomllib": toml_parser}
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, "<workflow-toml-helpers>", "exec"), namespace)
    return namespace


def _recovered_archive_helpers() -> dict:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    )
    step = next(
        item
        for item in workflow["jobs"]["verify-release"]["steps"]
        if item.get("name") == "Fail on any recovered artifact archive digest mismatch"
    )
    source = step["run"].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    tree = ast.parse(source)
    functions = {"exact_file_read", "valid_flat_name", "preflight_zip", "hash_exact"}
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and (
                target.id.startswith("MAX_ARCHIVE_") or target.id == "READ_CHUNK_BYTES"
            )
            for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in functions:
            selected.append(node)
    namespace: dict = {}
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    exec(compile(module, "<recovered-archive-helpers>", "exec"), namespace)
    return namespace


def _load_testpypi_verifier():
    path = REPO_ROOT / "scripts" / "verify_testpypi_release.py"
    spec = importlib.util.spec_from_file_location("testpypi_verifier", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import invariant
        raise AssertionError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_fake_distributions(dist: Path, version: str = CURRENT_VERSION) -> None:
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


class _FakeHTTPHeaders:
    def __init__(self, content_lengths: list[str] | None):
        self._content_lengths = content_lengths

    def get_all(self, name: str):
        if name != "Content-Length":  # pragma: no cover - verifier invariant
            raise AssertionError(name)
        return self._content_lengths


class _FakeHTTPResponse:
    def __init__(
        self,
        payload: bytes | int,
        url: str,
        content_lengths: list[str] | None = None,
    ):
        self._stream = io.BytesIO(payload) if isinstance(payload, bytes) else None
        self._remaining = payload if isinstance(payload, int) else 0
        self._url = url
        self.headers = _FakeHTTPHeaders(content_lengths)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self._url

    def read(self, size=-1):
        if self._stream is not None:
            return self._stream.read(size)
        requested = self._remaining if size < 0 else min(size, self._remaining)
        self._remaining -= requested
        return b"x" * requested


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


class StandaloneDistributionTests(unittest.TestCase):
    def test_zipapp_has_version_metadata_license_and_unique_staging(self):
        expected_version = CURRENT_VERSION
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
    @unittest.skipUnless(
        hasattr(sys, "set_int_max_str_digits"),
        "runtime integer digit limit requires Python 3.11+",
    )
    def test_inline_project_toml_parsing_is_digit_limit_independent(self):
        locations = (
            (
                REPO_ROOT / ".github/workflows/publish.yml",
                "verify-release",
                "Validate main, tag, commit, and package version",
            ),
            (
                REPO_ROOT / ".github/workflows/create-release-tag.yml",
                "verify-candidate",
                "Validate inputs, version, and current main commit",
            ),
        )
        ignored_digits = "7" * 1000
        valid_document = (
            f"{ignored_digits} = 'numeric bare key'\n"
            f"note = '{ignored_digits}' # {ignored_digits}\n"
            "[project]\nname = 'boundver'\nversion = '0.11.0'\n"
        )
        oversized_value = valid_document + f"ignored = {ignored_digits}\n"
        original_limit = sys.get_int_max_str_digits()
        try:
            for workflow_path, job_name, step_name in locations:
                helpers = _inline_toml_helpers(workflow_path, job_name, step_name)
                strict_loads = helpers["strict_toml_loads"]
                for limit in (640, 4300, 0):
                    with self.subTest(
                        workflow=workflow_path.name,
                        limit=limit,
                    ):
                        sys.set_int_max_str_digits(limit)
                        parsed = strict_loads(valid_document)
                        self.assertEqual(parsed["project"]["version"], "0.11.0")
                        with self.assertRaisesRegex(
                            SystemExit,
                            "TOML numeric token exceeds the 640-digit safety limit",
                        ):
                            strict_loads(oversized_value)
        finally:
            sys.set_int_max_str_digits(original_limit)

    def test_release_runs_bind_deduplication_to_tag_sha_alias_and_resume_identity(self):
        create = (REPO_ROOT / ".github/workflows/create-release-tag.yml").read_text(
            encoding="utf-8"
        )
        publish = (REPO_ROOT / ".github/workflows/publish.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "run-name: release-tag:${{ inputs.release_tag }}@${{ inputs.release_sha }}:alias=${{ inputs.compatibility_alias }}",
            create,
        )
        self.assertIn(
            "run-name: publish:${{ inputs.release_tag }}@${{ inputs.release_sha }}:alias=${{ inputs.compatibility_alias }}:resume=${{ inputs.resume_run_id }}",
            publish,
        )
        self.assertIn("EXPECTED_RUN_TITLE:", create)
        self.assertIn('run.get("display_title") == expected_title', create)
        self.assertIn('run.get("event") == "workflow_dispatch"', create)
        self.assertIn("PUBLICATION_RUN_STATE_PROGRAM: |", create)
        self.assertIn("MAX_PAGES = 100", create)
        self.assertIn("MAX_ITEMS = 10_000", create)
        self.assertIn("def iter_runs(endpoint):", create)
        self.assertIn(
            'actions/workflows/publish.yml/runs?per_page=100', create
        )
        self.assertNotIn("gh api --paginate --slurp", create)

    def test_packaged_config_schema_matches_the_root_contract(self):
        self.assertEqual(
            (REPO_ROOT / "boundary.config.schema.json").read_bytes(),
            (REPO_ROOT / "src" / "boundver" / "boundary.config.schema.json").read_bytes(),
        )

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
        self.assertEqual(
            jobs["pypi-preflight"]["needs"],
            ["verify-release", "verify-marketplace"],
        )
        self.assertIn("pypi-preflight", jobs["publish-pypi"]["needs"])
        self.assertIn("verify-marketplace", jobs["publish-pypi"]["needs"])
        self.assertIn("publish-pypi", jobs["verify-pypi"]["needs"])
        self.assertEqual(
            jobs["validate-compatibility-alias"]["needs"], "verify-pypi"
        )
        self.assertEqual(
            jobs["advance-compatibility-alias"]["needs"],
            ["verify-pypi", "validate-compatibility-alias"],
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
        self.assertIn(
            "Require current reviews for the exact fresh release commit", workflow
        )
        self.assertIn(
            'bash scripts/audit_release_reviews.sh "$RELEASE_SHA" "$RELEASE_TAG"',
            workflow,
        )
        self.assertIn("scripts/verify_release_candidate.py", workflow)
        self.assertNotIn("bash scripts/packaging_smoke.sh", workflow)
        self.assertIn("dist/*.whl", workflow)
        self.assertIn("dist/*.tar.gz", workflow)
        self.assertGreaterEqual(
            workflow.count("scripts/release_changelog.py"), 2
        )
        # Recovery skips the fresh-build verifier, so readiness remains an
        # explicit release-source gate for both fresh and resumed promotion.
        self.assertIn("scripts/verify_release_readiness.py", workflow)
        self.assertIn('--notes-file "$notes_file"', workflow)
        self.assertIn("find_release_id()", workflow)
        self.assertIn("RELEASE_DRAFT_API_PROGRAM:", workflow)
        self.assertIn("MAX_PAGES = 100", workflow)
        self.assertIn("MAX_ITEMS = 10_000", workflow)
        self.assertIn("GitHub Release list page is malformed", workflow)
        self.assertNotIn("gh api --paginate --slurp", workflow)
        self.assertIn("--draft", workflow)
        self.assertIn("SHA256SUMS", workflow)
        self.assertIn("environment: marketplace", workflow)
        self.assertIn("pypi-attestations verify pypi", workflow)
        self.assertNotIn("--generate-notes", workflow)
        self.assertNotIn("--clobber", workflow)
        self.assertNotIn("git tag --force v0 ", workflow)
        self.assertEqual(workflow.count("retention-days: 90"), 3)

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
        fresh_review = next(
            step
            for step in workflow["jobs"]["verify-release"]["steps"]
            if step.get("name")
            == "Require current reviews for the exact fresh release commit"
        )
        self.assertEqual(fresh_review["if"], "inputs.resume_run_id == ''")
        self.assertEqual(fresh_review["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertIn(
            '"$RELEASE_SHA" "$RELEASE_TAG"', fresh_review["run"]
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

    def test_release_reconciliation_by_numeric_id_and_asset_resume_fail_closed(self):
        workflow = (REPO_ROOT / ".github/workflows/publish.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("find_release_id()", workflow)
        self.assertIn("RELEASE_DRAFT_API_PROGRAM:", workflow)
        self.assertIn("for page_number in range(1, MAX_PAGES + 1):", workflow)
        self.assertIn("MAX_PAGE_BYTES = 8 * 1024 * 1024", workflow)
        self.assertIn("MAX_DETAIL_BYTES = 32 * 1024 * 1024", workflow)
        self.assertIn("MAX_TOTAL_BYTES = 32 * 1024 * 1024", workflow)
        self.assertIn("subprocess.Popen(", workflow)
        self.assertIn("kill_process(process)", workflow)
        self.assertNotIn("gh api --paginate --slurp", workflow)
        self.assertNotIn("release_json_path).read_text", workflow)
        self.assertIn(
            'f"repos/{repository}/releases/{release_id}"', workflow
        )
        self.assertIn("GitHub Release contains unexpected assets", workflow)
        self.assertIn("GitHub Release asset conflicts with candidate bytes", workflow)
        self.assertIn('gh release upload "$RELEASE_TAG"', workflow)
        self.assertIn("Public GitHub Release is missing assets", workflow)
        self.assertIn(
            "Public GitHub Release is not immutable",
            workflow,
        )
        self.assertIn(
            "GitHub Release API failed during final draft verification",
            workflow,
        )
        self.assertLess(
            workflow.index("cmp --silent"),
            workflow.index('gh release upload "$RELEASE_TAG"'),
        )
        self.assertNotIn('gh release view "$RELEASE_TAG"', workflow)

    def test_elevated_publish_jobs_never_execute_candidate_code(self):
        import yaml

        jobs = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )["jobs"]
        elevated = {
            name: job
            for name, job in jobs.items()
            if job.get("permissions", {}).get("contents") == "write"
            or job.get("permissions", {}).get("id-token") == "write"
        }
        self.assertEqual(
            set(elevated),
            {
                "publish-testpypi",
                "prepare-release-draft",
                "publish-pypi",
                "advance-compatibility-alias",
            },
        )
        for job_name, job in elevated.items():
            steps = job["steps"]
            scripts = "\n".join(str(step.get("run", "")) for step in steps)
            self.assertFalse(
                any(
                    str(step.get("uses", "")).startswith("actions/checkout@")
                    for step in steps
                ),
                job_name,
            )
            self.assertNotIn("scripts/", scripts, job_name)
            self.assertNotIn("python3 - ", scripts, job_name)
            self.assertNotIn("python3 -c ", scripts, job_name)

        draft_script = next(
            step["run"]
            for step in jobs["prepare-release-draft"]["steps"]
            if step.get("name") == "Create or reconcile the exact release draft"
        )
        self.assertIn('clean_python_cwd=$(mktemp -d)', draft_script)
        self.assertIn('(cd "$clean_python_cwd" && python3 -I "$@")', draft_script)
        self.assertNotIn("git ls-remote origin", draft_script)

        for job_name in ("publish-testpypi", "publish-pypi"):
            steps = jobs[job_name]["steps"]
            self.assertTrue(steps)
            self.assertTrue(all("run" not in step for step in steps), job_name)
            self.assertEqual(
                sum(
                    str(step.get("uses", "")).startswith(
                        "pypa/gh-action-pypi-publish@"
                    )
                    for step in steps
                ),
                1,
                job_name,
            )

    def test_token_scoped_steps_cannot_import_from_the_candidate_checkout(self):
        import re
        import yaml

        jobs = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )["jobs"]
        token_python_steps = []
        for job_name, job in jobs.items():
            for step in job.get("steps", []):
                if "GH_TOKEN" not in (step.get("env") or {}):
                    continue
                script = str(step.get("run", ""))
                self.assertIsNone(
                    re.search(r"\bpython(?:3)?\s+-(?:c|\s)", script),
                    f"{job_name}: {step.get('name')}",
                )
                self.assertNotIn("python3 scripts/", script, job_name)
                self.assertNotIn("python3 .release-control/", script, job_name)
                if "python" in script:
                    token_python_steps.append((job_name, step["name"]))
                    self.assertIn("python3 -I", script, step["name"])
                    self.assertIn("clean_python_cwd=$(mktemp -d)", script, step["name"])
        self.assertEqual(
            token_python_steps,
            [
                ("verify-release", "Locate the exact source-run artifacts for recovery"),
                ("verify-release", "Bind recovery policy to the source verification log"),
                (
                    "verify-release",
                    "Fail on any recovered artifact archive digest mismatch",
                ),
                ("prepare-release-draft", "Create or reconcile the exact release draft"),
            ],
        )

        testpypi_steps = jobs["testpypi-preflight"]["steps"]
        fetch = next(step for step in testpypi_steps if step.get("id") == "fetch-github-release")
        parse = next(step for step in testpypi_steps if step.get("id") == "github-release-probe")
        public_verify = next(
            step
            for step in testpypi_steps
            if step.get("name") == "Verify an existing public Release without a token"
        )
        self.assertIn("GH_TOKEN", fetch["env"])
        self.assertNotIn("python", fetch["run"])
        for step in (parse, public_verify):
            self.assertNotIn("GH_TOKEN", step.get("env", {}))
            self.assertIn("python3 -I", step["run"])

    def test_publish_python_and_pip_startup_is_always_isolated(self):
        import re
        import yaml

        jobs = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )["jobs"]
        scripts = "\n".join(
            str(step.get("run", ""))
            for job in jobs.values()
            for step in job.get("steps", [])
        )
        forbidden = (
            r"\bpython(?:3)?\s+scripts/install_locked_tools\.py\b",
            r"\bpython(?:3)?\s+-m\s+(?:pip|twine|venv)\b",
            r"/python\"?\s+-m\s+(?:pip|boundver)\b",
            r"/python\"?\s+-c\b",
            r"\bpython(?:3)?\s+-(?:c|\s)",
        )
        for pattern in forbidden:
            self.assertIsNone(re.search(pattern, scripts), pattern)
        self.assertIsNone(
            re.search(
                r"(?<![/\w-])python(?:3)?[ \t]+(?!-I(?:[ \t]|$))",
                scripts,
            ),
            "every literal workflow Python startup must enter isolated mode",
        )
        self.assertGreaterEqual(
            scripts.count("-I scripts/install_locked_tools.py"), 4
        )
        self.assertGreaterEqual(scripts.count("-I -m pip --isolated"), 3)

    def test_release_notes_and_alias_validation_are_read_only_handoffs(self):
        import yaml

        jobs = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )["jobs"]
        notes = jobs["prepare-release-notes"]
        self.assertEqual(notes["permissions"], {"contents": "read"})
        upload_notes = next(
            step
            for step in notes["steps"]
            if step.get("id") == "upload-release-notes"
        )
        self.assertEqual(
            upload_notes["with"]["name"],
            "release-notes-${{ inputs.release_sha }}-"
            "${{ github.run_id }}-${{ github.run_attempt }}",
        )
        self.assertIn("prepare-release-notes", jobs["prepare-release-draft"]["needs"])
        self.assertEqual(
            jobs["prepare-release-draft"]["steps"][1]["with"]["artifact-ids"],
            "${{ needs.prepare-release-notes.outputs.release-notes-artifact-id }}",
        )
        alias_validation = jobs["validate-compatibility-alias"]
        self.assertEqual(alias_validation["permissions"], {"contents": "read"})
        self.assertEqual(
            set(alias_validation["outputs"]),
            {"update-required", "expected-current"},
        )
        validation_script = "\n".join(
            str(step.get("run", "")) for step in alias_validation["steps"]
        )
        control_checkout = next(
            step
            for step in alias_validation["steps"]
            if step.get("name")
            == "Checkout the reviewed release-control implementation"
        )
        self.assertEqual(control_checkout["with"]["ref"], "${{ github.sha }}")
        self.assertEqual(control_checkout["with"]["path"], ".release-control")
        self.assertIn(
            ".release-control/scripts/validate_compatibility_alias.py",
            validation_script,
        )
        self.assertNotIn("python3 -I -", validation_script)
        self.assertNotIn("alias-tag-list-bounded-runner", validation_script)
        alias_mutation = jobs["advance-compatibility-alias"]
        mutation_script = "\n".join(
            str(step.get("run", "")) for step in alias_mutation["steps"]
        )
        self.assertNotIn("python", mutation_script)
        self.assertIn("mutation_repo=$(mktemp -d)", mutation_script)
        push_step = next(
            step
            for step in alias_mutation["steps"]
            if step.get("name") == "Push only the validated compatibility-alias update"
        )
        self.assertIn(
            "${{ needs.validate-compatibility-alias.outputs.expected-current }}",
            push_step["env"].values(),
        )

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
            set(jobs["verify-release"]["outputs"]),
            {
                "source-run-id",
                "python-dist-artifact-id",
                "release-assets-artifact-id",
            },
        )
        self.assertEqual(
            set(jobs["testpypi-preflight"]["outputs"]),
            {"upload-required"},
        )
        self.assertEqual(
            set(jobs["pypi-preflight"]["outputs"]),
            {"upload-required"},
        )
        selector = next(
            step
            for step in jobs["verify-release"]["steps"]
            if step.get("id") == "select-artifacts"
        )
        self.assertIn("RECOVERED_PYTHON_ID", selector["env"])
        self.assertIn("FRESH_PYTHON_ID", selector["env"])
        self.assertIn("dist/*.whl", upload["with"]["path"])
        self.assertIn("dist/*.tar.gz", upload["with"]["path"])
        self.assertNotIn("pyz", upload["with"]["path"])

        consuming_jobs = [
            "testpypi-preflight",
            "publish-testpypi",
            "verify-testpypi",
            "verify-marketplace",
            "pypi-preflight",
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
        self.assertIn("verify-release", jobs["publish-pypi"]["needs"])
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
        # A failed publisher job may already have uploaded one immutable file.
        # Retrying the failed job must reuse the complete bound artifact and
        # tolerate that exact filename; verify-pypi checks all public bytes.
        self.assertIs(production_publish["with"]["skip-existing"], True)
        self.assertEqual(production_publish["with"]["packages-dir"], "dist")
        self.assertEqual(
            production_publish["if"],
            "needs.pypi-preflight.outputs.upload-required == 'true'",
        )
        production_steps = jobs["publish-pypi"]["steps"]
        production_download = next(
            step
            for step in production_steps
            if str(step.get("uses", "")).startswith("actions/download-artifact@")
        )
        self.assertEqual(
            production_download["with"]["artifact-ids"],
            "${{ needs.verify-release.outputs.python-dist-artifact-id }}",
        )
        self.assertEqual(production_download["with"]["path"], "dist")
        self.assertEqual(
            production_download["with"]["run-id"],
            "${{ needs.verify-release.outputs.source-run-id }}",
        )
        self.assertFalse(
            any(
                str(step.get("uses", "")).startswith("actions/upload-artifact@")
                for step in jobs["pypi-preflight"]["steps"]
            ),
            "the production preflight must not freeze a stale missing-file subset",
        )
        production_script = "\n".join(
            str(step.get("run", ""))
            for step in production_steps
        )
        self.assertEqual(production_script, "\n")
        testpypi_script = "\n".join(
            str(step.get("run", ""))
            for step in jobs["publish-testpypi"]["steps"]
        )
        self.assertEqual(testpypi_script, "\n")
        self.assertFalse(
            any(str(step.get("uses", "")).startswith("actions/checkout@")
                for step in jobs["publish-testpypi"]["steps"])
        )

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
        self.assertIn("verify_testpypi_release.py provenance", workflow)
        self.assertNotIn("MAX_PROVENANCE_BYTES =", workflow)

    def test_every_downstream_artifact_download_uses_selected_source_run(self):
        import yaml

        jobs = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )["jobs"]
        downstream = []
        allowed_ids = {
            "${{ needs.verify-release.outputs.python-dist-artifact-id }}",
            "${{ needs.verify-release.outputs.release-assets-artifact-id }}",
        }
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
                if options.get("artifact-ids") not in allowed_ids:
                    continue
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
                self.assertIn(options["artifact-ids"], allowed_ids)
                downstream.append((job_name, download))
        self.assertEqual(len(downstream), 12)

    def test_public_release_checks_use_reviewed_control_code(self):
        import yaml

        jobs = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )["jobs"]
        control_jobs = [
            job_name
            for job_name, job in jobs.items()
            if any(
                step.get("name")
                == "Checkout the reviewed release-control commit"
                for step in job.get("steps", [])
            )
        ]
        self.assertEqual(
            control_jobs,
            [
                "testpypi-preflight",
                "verify-marketplace",
                "verify-public-surfaces",
            ],
        )
        for job_name in control_jobs:
            checkout = next(
                step
                for step in jobs[job_name]["steps"]
                if step.get("name")
                == "Checkout the reviewed release-control commit"
            )
            self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
            self.assertEqual(checkout["with"]["path"], ".release-control")
            self.assertIs(checkout["with"]["persist-credentials"], False)

        for job_name in (
            "testpypi-preflight",
            "verify-marketplace",
            "verify-public-surfaces",
        ):
            scripts = "\n".join(
                str(step.get("run", "")) for step in jobs[job_name]["steps"]
            )
            self.assertIn(
                ".release-control/scripts/verify_release_surfaces.py",
                scripts,
            )
            self.assertIn(
                ".release-control/scripts/release_changelog.py",
                scripts,
            )

    def test_publish_recovery_fails_closed_on_archive_or_payload_mismatch(self):
        import yaml

        steps = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/publish.yml").read_text(
                encoding="utf-8"
            )
        )["jobs"]["verify-release"]["steps"]
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

        recovery_lookup = next(
            step
            for step in steps
            if step["name"] == "Locate the exact source-run artifacts for recovery"
        )["run"]
        for check in (
            "MAX_GH_RESPONSE_BYTES = 8 * 1024 * 1024",
            "MAX_GH_TOTAL_BYTES = 16 * 1024 * 1024",
            "subprocess.Popen(",
            "remaining_with_sentinel",
            "kill_process(process)",
            "len(jobs) > 100",
            "not 2 <= artifacts_total <= 100",
            "release_notes_re = re.compile(",
            "not set(expected_names).issubset(artifact_names)",
        ):
            self.assertIn(check, recovery_lookup)

        log_lookup = next(
            step
            for step in steps
            if step["name"] == "Bind recovery policy to the source verification log"
        )["run"]
        for check in (
            "MAX_LOG_BYTES = 32 * 1024 * 1024",
            "MAX_LOG_LINES = 500_000",
            "MAX_POLICY_TRIPLES = 10_000",
            "subprocess.Popen(",
            "kill_process(process)",
        ):
            self.assertIn(check, log_lookup)

        archive_gate = next(
            step
            for step in steps
            if step["name"]
            == "Fail on any recovered artifact archive digest mismatch"
        )["run"]
        for check in (
            "actions/artifacts/$artifact_id/zip",
            'actual_digest="sha256:$(sha256sum',
            '"$actual_digest" != "$expected_digest"',
            "artifact archive is not a unique flat file set",
            "MAX_ARCHIVE_METADATA_BYTES = 1024 * 1024",
            "MAX_ARCHIVE_MEMBERS = 16",
            "MAX_ARCHIVE_PATH_BYTES = 1024",
            "MAX_ARCHIVE_MEMBER_BYTES = 128 * 1024 * 1024",
            "MAX_ARCHIVE_TOTAL_BYTES = 256 * 1024 * 1024",
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
            "python3 -I -m twine check",
        ):
            self.assertIn(check, payload_gate)
        self.assertEqual(payload_gate.count("cmp --silent"), 2)

    def test_recovered_archive_preflight_rejects_count_path_and_size_bombs(self):
        helpers = _recovered_archive_helpers()
        preflight_zip = helpers["preflight_zip"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            too_many = root / "too-many.zip"
            with ZipFile(too_many, "w") as archive:
                archive.writestr("one", b"1")
                archive.writestr("two", b"2")
            helpers["MAX_ARCHIVE_MEMBERS"] = 1
            with self.assertRaisesRegex(SystemExit, "member-count limit"):
                preflight_zip(too_many)

            helpers["MAX_ARCHIVE_MEMBERS"] = 16
            helpers["MAX_ARCHIVE_PATH_BYTES"] = 8
            long_path = root / "long-path.zip"
            with ZipFile(long_path, "w") as archive:
                archive.writestr("ninebytes", b"1")
            with self.assertRaisesRegex(SystemExit, "metadata is unsupported"):
                preflight_zip(long_path)

            helpers["MAX_ARCHIVE_PATH_BYTES"] = 1024
            helpers["MAX_ARCHIVE_MEMBER_BYTES"] = 8
            size_bomb = root / "size-bomb.zip"
            with ZipFile(size_bomb, "w") as archive:
                archive.writestr("payload", b"x" * 9)
            with self.assertRaisesRegex(SystemExit, "member exceeds the size limit"):
                preflight_zip(size_bomb)

        with self.assertRaisesRegex(SystemExit, "exceeds its advertised size"):
            helpers["hash_exact"](io.BytesIO(b"xy"), 1, "test member")

    def test_release_candidate_is_tested_before_tag_job(self):
        import yaml

        workflow = (
            REPO_ROOT / ".github/workflows/create-release-tag.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("verify-candidate:", workflow)
        self.assertIn("needs: verify-candidate", workflow)
        self.assertIn("scripts/verify_release_candidate.py", workflow)
        self.assertIn('--release-sha "$RELEASE_SHA"', workflow)
        self.assertNotIn("pytest -q", workflow)
        self.assertNotIn("bash scripts/packaging_smoke.sh", workflow)
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
        self.assertIn('--ref "$RELEASE_TAG"', workflow)
        self.assertLess(workflow.index("verify-candidate:"), workflow.index("\n  tag:"))
        jobs = yaml.safe_load(workflow)["jobs"]
        for job_name in ("verify-candidate", "revalidate-release-state"):
            self.assertEqual(jobs[job_name]["permissions"]["issues"], "read")
            self.assertEqual(
                jobs[job_name]["permissions"]["pull-requests"], "read"
            )
        self.assertEqual(
            jobs["tag"]["permissions"],
            {
                "actions": "read",
                "contents": "write",
                "issues": "read",
                "pull-requests": "read",
            },
        )
        self.assertEqual(
            jobs["dispatch-publication"]["permissions"],
            {"actions": "write", "contents": "read"},
        )
        tag_script = "\n".join(
            str(step.get("run", "")) for step in jobs["tag"]["steps"]
        )
        snapshot_program = yaml.safe_load(workflow)["env"]["REVIEW_STATE_PROGRAM"]
        compile(snapshot_program, "<review-state-program>", "exec")
        self.assertIn("reviewThreads(first:100,after:$endCursor)", snapshot_program)
        self.assertIn("collaborators/{encoded_login}/permission", snapshot_program)
        self.assertEqual(
            jobs["revalidate-release-state"]["outputs"]["review-state-digest"],
            "${{ steps.mutable-state.outputs.review-state-digest }}",
        )
        mutable_state = next(
            step
            for step in jobs["revalidate-release-state"]["steps"]
            if step.get("id") == "mutable-state"
        )["run"]
        snapshot_before = mutable_state.index("review_state_before=")
        semantic_audit = mutable_state.index("bash scripts/audit_release_reviews.sh")
        snapshot_after = mutable_state.index("review_state_digest=")
        idempotent_branch = mutable_state.index('if [[ -z "$existing_tag" ]]')
        self.assertLess(snapshot_before, semantic_audit)
        self.assertLess(semantic_audit, snapshot_after)
        self.assertLess(snapshot_after, idempotent_branch)
        self.assertIn("changed during the semantic audit", mutable_state)
        self.assertNotIn("scripts/", tag_script)
        self.assertNotIn("python3 scripts/", tag_script)
        self.assertGreaterEqual(tag_script.count('"$REVIEW_STATE_PROGRAM"'), 3)
        self.assertGreaterEqual(
            tag_script.count('cd "$clean_python_cwd" && python3 -I -c'),
            3,
        )
        first_snapshot = tag_script.index("current_review_state_digest=")
        idempotent_return = tag_script.index('if [[ -n "$existing_tag" ]]')
        final_snapshot = tag_script.rindex("current_review_state_digest=")
        tag_mutation = tag_script.index('git tag "$RELEASE_TAG" "$RELEASE_SHA"')
        self.assertLess(first_snapshot, idempotent_return)
        self.assertLess(idempotent_return, final_snapshot)
        self.assertLess(final_snapshot, tag_mutation)
        self.assertIn("gh auth setup-git", tag_script)
        self.assertIn("credential.https://github.com.helper", tag_script)

    @unittest.skipIf(os.name == "nt", "workflow snapshot harness is POSIX-only")
    def test_release_review_snapshot_is_stable_and_sensitive_to_review_state(self):
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/create-release-tag.yml").read_text(
                encoding="utf-8"
            )
        )
        program = workflow["env"]["REVIEW_STATE_PROGRAM"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repository = root / "repository"
            repository.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Review Snapshot Test"],
                cwd=repository,
                check=True,
            )
            tracked = repository / "tracked.txt"
            tracked.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=repository, check=True)
            subprocess.run(["git", "tag", "v0.10.0"], cwd=repository, check=True)
            tracked.write_text("release\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-qam", "release"], cwd=repository, check=True)
            release_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repository, text=True
            ).strip()

            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                """#!/usr/bin/env python3
import json
import os
import sys

endpoint = sys.argv[2]
sha = os.environ["FAKE_RELEASE_SHA"]
actor = {"id": 199175422, "login": "codex[bot]", "type": "Bot"}
if endpoint == "graphql":
    payload = {"data": {"repository": {"pullRequest": {
        "reviewDecision": "REVIEW_REQUIRED",
        "reviewThreads": {"nodes": [], "pageInfo": {
            "hasNextPage": False, "endCursor": None,
        }},
    }}}}
elif endpoint.startswith("repos/owner/repository/commits/") and "/pulls?per_page=100" in endpoint:
    payload = [{"number": 17}]
elif endpoint.startswith("repos/owner/repository/commits/"):
    payload = {"sha": sha}
elif endpoint == "repos/owner/repository":
    payload = {"owner": {"id": 101, "login": "owner", "type": "User"}}
elif endpoint == "repos/owner/repository/pulls/17":
    payload = {
        "number": 17,
        "state": "closed",
        "merged_at": "2026-08-18T00:00:00Z",
        "user": {"id": 101, "login": "owner", "type": "User"},
        "head": {"sha": sha},
        "base": {
            "ref": "main",
            "repo": {"full_name": "owner/repository"},
        },
        "merge_commit_sha": sha,
        "requested_reviewers": [],
        "requested_teams": [],
    }
elif endpoint.startswith("repos/owner/repository/pulls/17/reviews"):
    payload = [{
        "id": 301,
        "state": "COMMENTED",
        "commit_id": sha,
        "body": os.environ["FAKE_REVIEW_BODY"],
        "user": actor,
    }]
elif endpoint.startswith("repos/owner/repository/issues/17/comments"):
    payload = []
else:
    raise SystemExit(f"unexpected endpoint: {endpoint}")
print(json.dumps(payload, separators=(",", ":")))
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            environment = dict(os.environ)
            environment.update(
                {
                    "PATH": f"{bin_dir}{os.pathsep}{environment['PATH']}",
                    "GITHUB_WORKSPACE": str(repository),
                    "RUNNER_TEMP": str(root),
                    "FAKE_RELEASE_SHA": release_sha,
                    "FAKE_REVIEW_BODY": "Codex Review: Didn't find any major issues.",
                }
            )

            command = [
                sys.executable,
                "-I",
                "-c",
                program,
                release_sha,
                CURRENT_TAG,
                "owner/repository",
            ]
            first = subprocess.run(
                command,
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            second = subprocess.run(
                command,
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertRegex(first, r"^[0-9a-f]{64}$")
            self.assertEqual(first, second)

            environment["FAKE_REVIEW_BODY"] = "Codex Review: blocking issue."
            changed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertNotEqual(changed, first)

    def test_action_and_container_install_complete_public_extras(self):
        action = (REPO_ROOT / "action.yml").read_text(encoding="utf-8")
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("[schema,yaml]", action)
        self.assertNotIn(".[schema,yaml]", dockerfile)
        self.assertIn("COPY scripts/requirements/action.lock", dockerfile)
        self.assertNotIn("python -m pip", dockerfile)
        self.assertEqual(dockerfile.count("python -I -m pip"), 5)
        self.assertIn("python -I -m pip download", dockerfile)
        self.assertIn("--index-url https://pypi.org/simple", dockerfile)
        self.assertGreaterEqual(dockerfile.count("--isolated"), 5)
        self.assertGreaterEqual(dockerfile.count("--require-hashes"), 2)
        self.assertGreaterEqual(dockerfile.count("--only-binary=:all:"), 2)
        self.assertIn("--requirement /locks/action.lock", dockerfile)
        self.assertIn("--no-index", dockerfile)
        self.assertIn("--no-deps", dockerfile)
        self.assertIn("--no-build-isolation", dockerfile)
        self.assertIn("ENV SOURCE_DATE_EPOCH=1785715200", dockerfile)
        self.assertIn("git config --system --add safe.directory /repo", dockerfile)
        self.assertRegex(dockerfile, r"(?m)^USER\s+boundver\s*$")
        self.assertIn("scripts/install_locked_tools.py\" action", action)
        self.assertIn("--no-index --no-deps", action)
        self.assertIn("--no-build-isolation", action)
        lock = (REPO_ROOT / "scripts/requirements/action.lock").read_text(
            encoding="utf-8"
        )
        for dependency in (
            "attrs",
            "jsonschema",
            "jsonschema-specifications",
            "PyYAML",
            "referencing",
            "rpds-py",
            "tomli",
            "typing-extensions",
            "pip",
            "setuptools",
            "wheel",
        ):
            self.assertRegex(lock, rf"(?mi)^{dependency}==")
        self.assertIn("--require-hashes", lock)
        self.assertIn("--only-binary :all:", lock)
        self.assertRegex(lock, r"--hash=sha256:[0-9a-f]{64}")

        releasing = (REPO_ROOT / "docs/RELEASING.md").read_text(encoding="utf-8")
        self.assertIn("pip's secure-install model", releasing)
        self.assertIn("checked-in SHA-256", releasing)
        self.assertIn("immutable Debian snapshot", releasing)

    def test_container_and_sdist_exclude_repository_only_material(self):
        import datetime
        import re

        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
        manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        smoke = (REPO_ROOT / "scripts/packaging_smoke.sh").read_text(
            encoding="utf-8"
        )
        base = (
            "python:3.12.14-slim-trixie@"
            "sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a"
        )
        self.assertEqual(dockerfile.count(f"FROM {base}"), 2)
        self.assertIn("snapshot.debian.org/archive/debian/20260803T000000Z", dockerfile)
        snapshot_stamps = set(
            re.findall(
                r"snapshot\.debian\.org/archive/(?:debian|debian-security)/"
                r"(\d{8}T\d{6}Z)",
                dockerfile,
            )
        )
        self.assertEqual(snapshot_stamps, {"20260803T000000Z"})
        snapshot_time = datetime.datetime.strptime(
            snapshot_stamps.pop(), "%Y%m%dT%H%M%SZ"
        ).replace(tzinfo=datetime.timezone.utc)
        epoch = re.search(r"(?m)^ENV SOURCE_DATE_EPOCH=(\d+)$", dockerfile)
        self.assertIsNotNone(epoch)
        self.assertEqual(int(epoch.group(1)), int(snapshot_time.timestamp()))
        self.assertGreaterEqual(dockerfile.count("grep -Fqx"), 2)
        self.assertIn('Acquire::Check-Valid-Until "false"', dockerfile)
        self.assertIn("git=1:2.47.3-0+deb13u1", dockerfile)
        self.assertNotIn("COPY . .", dockerfile)
        self.assertIn("COPY src ./src", dockerfile)
        for ignored in ("tests/", "scripts/*", ".github/"):
            self.assertIn(ignored, dockerignore)
        self.assertIn("!scripts/requirements/action.lock", dockerignore)
        self.assertNotIn("recursive-include tests", manifest)
        self.assertIn("exclude docs/PROJECT_REVIEW.md", manifest)
        self.assertIn("exclude docs/RELEASING.md", manifest)
        self.assertIn("sdist contains repository-only material", smoke)
        for excluded in ("tests", "scripts", ".github", "Dockerfile", "action.yml"):
            self.assertIn(excluded, smoke)

    def test_ci_executes_docker_and_all_published_pre_commit_hooks(self):
        import yaml

        jobs = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        )["jobs"]
        public = jobs["public-installations"]
        self.assertEqual(
            public["strategy"]["matrix"]["os"],
            ["ubuntu-latest", "windows-latest", "macos-15"],
        )
        script = "\n".join(
            str(step.get("run", "")) for step in public["steps"]
        )
        self.assertIn(
            "python -I -m pre_commit try-repo . boundver-verify --all-files",
            script,
        )
        self.assertIn("boundver-verify-push", script)
        self.assertIn("--hook-stage pre-push", script)
        self.assertIn(
            "python -I -m pre_commit try-repo . boundver-generate --all-files",
            script,
        )
        self.assertIn("git diff --exit-code -- boundary.lock.json", script)
        self.assertIn("docker build", script)
        self.assertIn("verify --source head --facets exact", script)

    def test_ci_covers_both_macos_architectures_without_full_version_cross_product(self):
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        )
        matrix = workflow["jobs"]["test"]["strategy"]["matrix"]
        self.assertEqual(matrix["os"], ["ubuntu-latest", "windows-latest"])
        self.assertEqual(
            matrix["include"],
            [
                {"os": "macos-15-intel", "python-version": "3.12"},
                {"os": "macos-15", "python-version": "3.12"},
            ],
        )
        self.assertEqual(
            workflow["jobs"]["action"]["strategy"]["matrix"]["include"],
            [
                {"os": "ubuntu-latest", "python-version": "3.9"},
                {"os": "ubuntu-latest", "python-version": "3.12"},
                {"os": "windows-latest", "python-version": "3.12"},
                {"os": "macos-15", "python-version": "3.12"},
            ],
        )

    def test_ci_lints_undefined_names_and_unused_code_without_auto_fixing(self):
        workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python -I -m ruff check src tests scripts", workflow)
        self.assertNotIn("ruff check --fix", workflow)
        project = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('ruff==0.16.3', project)
        self.assertIn('select = ["F"]', project)

    def test_workflows_do_not_persist_checkout_credentials(self):
        import yaml

        for relative in (
            ".github/workflows/ci.yml",
            ".github/workflows/create-release-tag.yml",
            ".github/workflows/publish.yml",
        ):
            with self.subTest(workflow=relative):
                workflow = yaml.safe_load(
                    (REPO_ROOT / relative).read_text(encoding="utf-8")
                )
                if relative.endswith("ci.yml"):
                    self.assertEqual(workflow["permissions"], {"contents": "read"})
                checkout_count = 0
                for job in workflow["jobs"].values():
                    for step in job.get("steps", []):
                        if str(step.get("uses", "")).startswith("actions/checkout@"):
                            checkout_count += 1
                            self.assertIs(
                                step.get("with", {}).get("persist-credentials"),
                                False,
                            )
                self.assertGreater(checkout_count, 0)

    def test_packaging_smoke_resolves_posix_and_windows_venv_layouts(self):
        smoke = (REPO_ROOT / "scripts/packaging_smoke.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('"$environment/bin/python"', smoke)
        self.assertIn('"$environment/Scripts/python.exe"', smoke)

    def test_gitlab_slim_recipe_installs_git(self):
        cookbook = (REPO_ROOT / "docs/ci-cookbook.md").read_text(encoding="utf-8")
        gitlab = cookbook[cookbook.index("## GitLab CI") :]
        self.assertIn("apt-get install", gitlab)
        self.assertIn("git", gitlab)

    def test_compatibility_alias_update_is_monotonic_ancestral_and_leased(self):
        workflow = (REPO_ROOT / ".github/workflows/publish.yml").read_text(
            encoding="utf-8"
        )
        validator = (
            REPO_ROOT / "scripts" / "validate_compatibility_alias.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            ".release-control/scripts/validate_compatibility_alias.py",
            workflow,
        )
        self.assertIn('("merge-base", "--is-ancestor"', validator)
        self.assertIn("refusing compatibility alias rollback", validator)
        self.assertIn('--force-with-lease=$alias_ref:$EXPECTED_CURRENT', workflow)
        self.assertIn('--force-with-lease=$alias_ref:', workflow)
        self.assertIn("expected-current={expected_current}", validator)
        self.assertIn("MAX_TAG_LIST_BYTES = 1024 * 1024", validator)
        self.assertIn("MAX_TAG_RECORDS = 10_000", validator)
        self.assertNotIn("alias-tag-list-bounded-runner", workflow)
        self.assertNotIn(
            '"refs/tags/$COMPATIBILITY_ALIAS.*" > "$same_line_tags"',
            workflow,
        )
        self.assertIn("gh auth setup-git", workflow)
        self.assertIn("credential.https://github.com.helper", workflow)
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
            self.assertIn("jsonschema==4.25.1", hook["additional_dependencies"])
            self.assertIn("PyYAML==6.0.3", hook["additional_dependencies"])


class TestPyPIReleaseVerificationTests(unittest.TestCase):
    def setUp(self):
        self.verifier = _load_testpypi_verifier()

    def _candidate(self, root: Path):
        dist = root / "dist"
        _write_fake_distributions(dist)
        return dist, self.verifier._load_candidate(
            dist, "boundver", CURRENT_VERSION
        )

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
                self.verifier._load_candidate(dist, "boundver", CURRENT_VERSION)

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
                version=CURRENT_VERSION,
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
                "upload-required=false\n",
            )

            output.write_text("", encoding="utf-8")
            partial = {next(iter(remote)): next(iter(remote.values()))}
            with mock.patch.object(
                self.verifier, "_query_release", return_value=partial
            ), mock.patch.object(self.verifier, "_download_and_verify"):
                self.verifier._preflight(args)
            self.assertRegex(
                output.read_text(encoding="utf-8"),
                r"^upload-required=true\n$",
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
                version=CURRENT_VERSION,
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

    def test_provenance_download_is_bounded_exact_and_exclusive(self):
        filename = f"boundver-{CURRENT_VERSION}-py3-none-any.whl"
        url = self.verifier._provenance_url(
            "boundver", CURRENT_VERSION, filename
        )
        payload = b'{"version":1}'
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "statement.json"
            response = _FakeHTTPResponse(
                payload, url, [str(len(payload))]
            )
            with mock.patch.object(
                self.verifier.urllib.request,
                "urlopen",
                return_value=response,
            ) as urlopen:
                self.verifier._fetch_provenance(
                    "boundver", CURRENT_VERSION, filename, output
                )
            self.assertEqual(output.read_bytes(), payload)
            request = urlopen.call_args.args[0]
            self.assertEqual(request.full_url, url)
            self.assertEqual(
                request.get_header("Accept"),
                "application/vnd.pypi.integrity.v1+json",
            )

            with self.assertRaisesRegex(
                self.verifier.ReleaseVerificationError, "already exists"
            ):
                self.verifier._fetch_provenance(
                    "boundver", CURRENT_VERSION, filename, output
                )

    def test_provenance_download_rejects_unsafe_responses(self):
        filename = f"boundver-{CURRENT_VERSION}.tar.gz"
        url = self.verifier._provenance_url(
            "boundver", CURRENT_VERSION, filename
        )
        cases = (
            (
                _FakeHTTPResponse(
                    b"",
                    url,
                    [str(self.verifier.MAX_PROVENANCE_BYTES + 1)],
                ),
                "exceeds",
            ),
            (
                _FakeHTTPResponse(
                    self.verifier.MAX_PROVENANCE_BYTES + 1, url
                ),
                "exceeds",
            ),
            (_FakeHTTPResponse(b"{}", url, ["3"]), "Content-Length"),
            (
                _FakeHTTPResponse(b"{}", url + "?redirected", ["2"]),
                "redirected",
            ),
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index, (response, message) in enumerate(cases):
                output = root / f"statement-{index}.json"
                with mock.patch.object(
                    self.verifier.urllib.request,
                    "urlopen",
                    return_value=response,
                ), self.assertRaises(
                    (
                        self.verifier.ReleaseVerificationError,
                        self.verifier.ReleaseNetworkError,
                    )
                ) as raised:
                    self.verifier._fetch_provenance(
                        "boundver", CURRENT_VERSION, filename, output
                    )
                self.assertRegex(str(raised.exception), message)
                self.assertFalse(output.exists())

        with self.assertRaisesRegex(
            self.verifier.ReleaseVerificationError, "provenance filename"
        ):
            self.verifier._provenance_url(
                "boundver", CURRENT_VERSION, "../boundver.whl"
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
        result, notes = self._run(
            _release_changelog(CURRENT_VERSION), CURRENT_TAG
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(notes, "- Shipped safely.\n")

    def test_unreleased_only_version_intent_cannot_be_tagged(self):
        changelog = (
            "# Changelog\n\n## [Unreleased]\n\n"
            f"These changes target {CURRENT_VERSION}.\n\n"
            "## [0.10.0] - 2026-08-11\n\n- Old.\n\n"
            "[0.10.0]: https://example.invalid/v0.10.0\n"
        )
        result, notes = self._run(changelog, CURRENT_TAG)
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(notes)
        self.assertIn(
            f"release section for {CURRENT_VERSION}", result.stderr
        )

    def test_version_section_must_be_newest_and_nonempty(self):
        stale = _release_changelog("0.10.1").replace(
            "## [0.10.1] - 2026-08-12\n\n- Shipped safely.\n\n",
            f"## [{CURRENT_VERSION}] - 2026-08-12\n\n- Newer.\n\n"
            "## [0.10.1] - 2026-08-11\n\n- Shipped safely.\n\n",
        )
        result, _ = self._run(stale, "v0.10.1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"newest release is {CURRENT_VERSION}", result.stderr
        )

        result, _ = self._run(
            _release_changelog(CURRENT_VERSION, notes="No changes yet.\n"),
            CURRENT_TAG,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is empty", result.stderr)

    def test_release_heading_and_date_must_be_exact(self):
        malformed = _release_changelog(CURRENT_VERSION).replace(
            f"## [{CURRENT_VERSION}] - 2026-08-12",
            f"## [{CURRENT_VERSION}] upcoming",
        )
        result, _ = self._run(malformed, CURRENT_TAG)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must include an ISO date", result.stderr)

        impossible_date = _release_changelog(CURRENT_VERSION).replace(
            "2026-08-12", "2026-99-99", 1
        )
        result, _ = self._run(impossible_date, CURRENT_TAG)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid ISO date", result.stderr)

    def test_release_and_unreleased_compare_links_are_exact(self):
        changelog = _release_changelog(CURRENT_VERSION).replace(
            f"https://github.com/yzm1/boundver/compare/v0.10.0...{CURRENT_TAG}",
            f"https://example.invalid/{CURRENT_TAG}",
        )
        result, _ = self._run(changelog, CURRENT_TAG)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("previous release", result.stderr)

        changelog = _release_changelog(CURRENT_VERSION).replace(
            f"https://github.com/yzm1/boundver/compare/{CURRENT_TAG}...HEAD",
            "https://example.invalid/HEAD",
        )
        result, _ = self._run(changelog, CURRENT_TAG)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unreleased link", result.stderr)


class ReleaseReviewAuditTests(unittest.TestCase):
    def _codex_comment(
        self,
        commit: str,
        *,
        duplicate: bool = False,
        verdict: str = "Codex Review: Didn't find any major issues. Breezy!",
    ) -> str:
        marker = f"**Reviewed commit:** `{commit}`"
        lines = [
            verdict,
            "",
            marker,
        ]
        if duplicate:
            lines.extend(("", marker))
        lines.extend(
            (
                "",
                "<details> <summary>ℹ️ About Codex in GitHub</summary>",
                "<br/>",
                "",
                "Codex review guidance belongs inside this recognized footer.",
                "</details>",
            )
        )
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

    def _review_record(
        self,
        commit: str,
        body: str | None = None,
        *,
        state: str = "COMMENTED",
        actor_id: str = "199175422",
        login: str = "chatgpt-codex-connector[bot]",
        actor_type: str = "Bot",
    ) -> str:
        if body is None:
            body = (
                "Codex Review: Didn't find any major issues. Hooray!\n\n"
                "<details> <summary>ℹ️ About Codex in GitHub</summary>\n"
                "review guidance\n</details>"
            )
        encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
        return (
            f"{state}|{actor_id}|{login}|{actor_type}|{commit}|{encoded}"
        )

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
  echo "$FAKE_AUTHOR_ID|$FAKE_AUTHOR_LOGIN|$FAKE_AUTHOR_TYPE|$FAKE_HEAD_SHA|$FAKE_MERGE_SHA|$FAKE_PENDING_REVIEWERS|$FAKE_PENDING_TEAMS|$FAKE_PR_STATE|$FAKE_MERGED_AT|$FAKE_BASE_REPOSITORY|$FAKE_BASE_REF"
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
  printf '%s' "$FAKE_PERMISSION"
  exit 0
fi
if [[ "$endpoint" == */commits/* ]]; then
  if [[ "$FAKE_FAILURE" == resolve ]]; then exit 73; fi
  candidate=${endpoint##*/commits/}
  if [[ "$FAKE_HEAD_SHA" == "$candidate"* ]]; then
    echo "$FAKE_HEAD_SHA"
  elif [[ -n "$FAKE_MERGE_SHA" && "$FAKE_MERGE_SHA" == "$candidate"* ]]; then
    echo "$FAKE_MERGE_SHA"
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
                    "FAKE_PR_STATE": "closed",
                    "FAKE_MERGED_AT": "2026-08-18T12:00:00Z",
                    "FAKE_BASE_REPOSITORY": "owner/repository",
                    "FAKE_BASE_REF": "main",
                    "FAKE_DECISION": "REVIEW_REQUIRED",
                    "FAKE_UNRESOLVED": "0",
                    "FAKE_REVIEWS": (
                        self._review_record(release_sha)
                    ),
                    "FAKE_COMMENTS": "",
                    "FAKE_PERMISSION": "write",
                }
            )
            environment.update(overrides)
            return subprocess.run(
                ["bash", "./audit_release_reviews.sh", release_sha, CURRENT_TAG],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                input="",
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
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        merge = "7" * 40
        comment = self._comment_record(self._codex_comment(merge[:10]))
        result = self._run_audit(
            FAKE_HEAD_SHA=head,
            FAKE_MERGE_SHA=merge,
            FAKE_REVIEWS="",
            FAKE_COMMENTS=comment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipIf(os.name == "nt", "release audit runs on Linux")
    def test_codex_evidence_accepts_observed_clean_verdict_flourishes(self):
        head = "a" * 40
        verdicts = (
            "Codex Review: Didn't find any major issues. Swish!",
            "Codex Review: Didn't find any major issues. Keep it up!",
            "Codex Review: Didn't find any major issues. Keep them coming!",
            "Codex Review: Didn't find any major issues. "
            "Already looking forward to the next diff.",
            "Codex Review: Didn't find any major issues. "
            "More of your lovely PRs please.",
        )
        for verdict in verdicts:
            with self.subTest(verdict=verdict):
                comment = self._comment_record(
                    self._codex_comment(head[:10], verdict=verdict)
                )
                result = self._run_audit(
                    FAKE_HEAD_SHA=head,
                    FAKE_REVIEWS="",
                    FAKE_COMMENTS=comment,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipIf(os.name == "nt", "release audit runs on Linux")
    def test_human_approval_requires_non_author_push_access_and_current_commit(self):
        head = "2" * 40
        approval = self._review_record(
            head[:12], "", state="APPROVED", actor_id="202", login="reviewer",
            actor_type="User",
        )
        result = self._run_audit(
            FAKE_HEAD_SHA=head,
            FAKE_REVIEWS=approval,
            FAKE_DECISION="APPROVED",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        for overrides in (
            {
                "FAKE_REVIEWS": self._review_record(
                    head, "", state="APPROVED", actor_id="101", login="owner",
                    actor_type="User",
                )
            },
            {"FAKE_REVIEWS": approval, "FAKE_PERMISSION": "read"},
            {
                "FAKE_REVIEWS": self._review_record(
                    "3" * 40, "", state="APPROVED", actor_id="202",
                    login="reviewer", actor_type="User",
                ),
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
                "FAKE_REVIEWS": self._review_record(head, actor_id="999")
            },
            {
                "FAKE_REVIEWS": self._review_record(stale),
            },
            {
                "FAKE_REVIEWS": self._review_record(
                    head, "Codex Review: found a release-blocking issue."
                )
            },
            {
                "FAKE_REVIEWS": self._review_record(
                    head,
                    "Codex Review: Didn't find any major issues. "
                    "Found a release-blocking issue.",
                )
            },
            {
                "FAKE_REVIEWS": self._review_record(
                    head,
                    "Codex Review: Didn't find any major issues. "
                    "However, one risk remains.",
                )
            },
            {
                "FAKE_REVIEWS": self._review_record(
                    head,
                    "Codex Review: Didn't find any major issues. "
                    + ("A" * 160)
                    + "!",
                )
            },
            {
                "FAKE_REVIEWS": self._review_record(
                    head,
                    "Codex Review: Didn't find any issues.\n\n"
                    "Found a release-blocking issue.",
                )
            },
            {
                "FAKE_REVIEWS": "",
                "FAKE_COMMENTS": self._comment_record(
                    self._codex_comment(head), actor_id="999"
                ),
            },
            {
                "FAKE_REVIEWS": "",
                "FAKE_COMMENTS": self._comment_record(
                    self._codex_comment(head[:9])
                ),
            },
            {
                "FAKE_REVIEWS": "",
                "FAKE_COMMENTS": self._comment_record(
                    self._codex_comment(head)
                    + "\n\nFound a release-blocking issue."
                ),
            },
            {
                "FAKE_REVIEWS": "",
                "FAKE_COMMENTS": self._comment_record(
                    "Codex Review: Didn't find any major issues. "
                    "Found a release-blocking issue.\n\n"
                    f"**Reviewed commit:** `{head[:10]}`"
                ),
            },
            {
                "FAKE_REVIEWS": "",
                "FAKE_COMMENTS": self._comment_record(
                    "Codex Review: found a release-blocking issue.\n\n"
                    f"**Reviewed commit:** `{head[:10]}`"
                ),
            },
            {
                "FAKE_REVIEWS": "",
                "FAKE_COMMENTS": self._comment_record(
                    self._codex_comment(stale)
                ),
            },
            {
                "FAKE_REVIEWS": "",
                "FAKE_COMMENTS": self._comment_record(
                    self._codex_comment(head, duplicate=True)
                ),
            },
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                result = self._run_audit(FAKE_HEAD_SHA=head, **overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("no current exact-commit review evidence", result.stderr)

    @unittest.skipIf(os.name == "nt", "release audit runs on Linux")
    def test_codex_evidence_rejects_multiple_current_contradictory_records(self):
        head = "6" * 40
        clean = self._review_record(head)
        adverse = self._review_record(
            head,
            "Codex Review: found a release-blocking issue.",
        )
        result = self._run_audit(
            FAKE_HEAD_SHA=head,
            FAKE_REVIEWS=f"{clean}\n{adverse}",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicting or ambiguous", result.stderr)

        empty = self._review_record(head, "")
        result = self._run_audit(
            FAKE_HEAD_SHA=head,
            FAKE_REVIEWS=f"{clean}\n{empty}",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicting or ambiguous", result.stderr)

    @unittest.skipIf(os.name == "nt", "release audit runs on Linux")
    def test_stale_evidence_cannot_consume_later_current_comment(self):
        head = "8" * 40
        stale = "9" * 40
        comments = "\n".join(
            (
                self._comment_record(self._codex_comment(stale)),
                self._comment_record(self._codex_comment(head)),
            )
        )
        result = self._run_audit(
            FAKE_HEAD_SHA=head,
            FAKE_REVIEWS="",
            FAKE_COMMENTS=comments,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipIf(os.name == "nt", "release audit runs on Linux")
    def test_review_gate_fails_closed_on_api_pagination_and_blocking_state(self):
        script = (REPO_ROOT / "scripts" / "audit_release_reviews.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("< <(\n    gh api", script)
        self.assertIn("capture_bounded reviews_output", script)
        self.assertIn("capture_bounded comments_output", script)
        self.assertIn("capture_bounded unresolved_output", script)
        self.assertIn("gh api --paginate", script)
        self.assertIn("gh api graphql --paginate", script)
        self.assertNotRegex(script, r"\w+_output=\$\(gh api")
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

        human = self._review_record(
            "6" * 40, "", state="APPROVED", actor_id="202", login="reviewer",
            actor_type="User",
        )
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
