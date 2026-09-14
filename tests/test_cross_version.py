"""Artifacts written by an older release, read by this one.

A baseline and a lockfile both carry a version pin, and both are meant to fail
loudly rather than quietly when the release that wrote them is not the one
reading them. The baseline half needs nothing but a document with an older tag
in it. The lockfile half needs a lock a previous release actually produced, so
it lives in a CI job rather than here.

Covers OBL-BASELINE-004.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from boundver._baseline import (
    BASELINE_SCHEMA,
    BASELINE_SCHEMA_URL,
    BaselineError,
    validate_baseline,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

SPEC = Path(__file__).resolve().parents[1] / "spec" / "verify-baseline.schema.json"

#: The tag the URL carries, e.g. v0.15.0.
_TAG = re.compile(r"/(v\d+\.\d+\.\d+)/")


class PreviousReleaseHelperTests(unittest.TestCase):
    """The two scripts the previous-release CI job runs.

    They cannot check the cross-version contract from here, because the
    boundver on this machine is this build. What can be checked from here is
    that they select the right version and find the right examples, so the job
    fails on a real disagreement rather than on its own plumbing.
    """

    def _module(self, name: str):
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_previous_release_precedes_this_one(self):
        module = self._module("previous_release")
        current = module.current_version()
        previous = module.previous_release()
        self.assertLess(previous, current)
        self.assertIn(previous, module.released_versions())

    def test_shortened_alias_tags_are_not_installable_versions(self):
        """The project publishes v0.14 alongside v0.14.0; only one installs."""
        module = self._module("previous_release")
        for version in module.released_versions():
            with self.subTest(version=version):
                self.assertEqual(len(version), 3)

    def test_every_example_with_a_config_is_covered(self):
        module = self._module("previous_release_locks")
        found = {path.name for path in module.example_directories()}
        expected = {
            path.parent.name
            for path in (Path(__file__).resolve().parents[1] / "examples").glob(
                "*/boundary.config.json"
            )
        }
        self.assertEqual(found, expected)
        self.assertTrue(found)

    def test_a_refusal_must_name_a_contract_axis(self):
        module = self._module("previous_release_locks")
        self.assertTrue(module._names_an_axis("boundary-lock schema changed"))
        self.assertTrue(module._names_an_axis("boundver-semantic-config/v1"))
        self.assertFalse(module._names_an_axis("something went wrong"))

    def test_previous_release_job_fetches_the_release_tags(self):
        workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        checkout = next(
            step
            for step in workflow["jobs"]["previous-release"]["steps"]
            if step.get("name") == "Checkout"
        )

        self.assertEqual(checkout["with"]["fetch-depth"], 0)
        self.assertIs(checkout["with"]["persist-credentials"], False)

    def test_downloaded_locks_are_staged_inside_the_repository(self):
        module = self._module("previous_release_locks")
        with tempfile.TemporaryDirectory() as repository_dir:
            root = Path(repository_dir)
            examples = root / "examples"
            (examples / "hello").mkdir(parents=True)
            (examples / "hello" / "boundary.config.json").write_text(
                "{}\n", encoding="utf-8"
            )
            with tempfile.TemporaryDirectory() as download_dir:
                downloaded = Path(download_dir) / "hello.lock.json"
                downloaded.write_text("{}\n", encoding="utf-8")

                def inspect_run(command, cwd):
                    lock = Path(command[command.index("--lock") + 1])
                    self.assertFalse(lock.is_absolute())
                    self.assertTrue((cwd / lock).is_file())
                    self.assertEqual((cwd / lock).read_bytes(), downloaded.read_bytes())
                    return subprocess.CompletedProcess(command, 0, "", "")

                with (
                    mock.patch.object(module, "ROOT", root),
                    mock.patch.object(module, "EXAMPLES", examples),
                    mock.patch.object(module, "_run", side_effect=inspect_run),
                ):
                    self.assertEqual(module.check(Path(download_dir)), module.OK)

            self.assertEqual(list(root.glob(".previous-release-check-*")), [])


class BaselineSchemaPinTests(unittest.TestCase):
    """OBL-BASELINE-004, first half: the pin must agree with the schema file."""

    def test_the_constant_equals_the_published_id(self):
        published = json.loads(io.open(SPEC, encoding="utf-8").read())
        self.assertEqual(published["$id"], BASELINE_SCHEMA_URL)

    def test_the_url_carries_a_release_tag(self):
        match = _TAG.search(BASELINE_SCHEMA_URL)
        self.assertIsNotNone(match, BASELINE_SCHEMA_URL)
        self.assertTrue(match.group(1).startswith("v"))


def _older(url: str, tag: str = "v0.0.1") -> str:
    return _TAG.sub(f"/{tag}/", url, count=1)


class BaselineUpgradePathTests(unittest.TestCase):
    """OBL-BASELINE-004, second half: an older baseline must say what to do.

    A baseline records which violations a team has already reviewed. Reading
    one written by a different release and silently trusting it would let a
    stale review suppress a real finding, so the refusal has to be loud and it
    has to tell the user the remedy.
    """

    def _baseline_document(self, scene: Scenario) -> dict:
        scene.component("svc", path="svc", boundary=["api"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.git("add", "--all")
        scene.git("commit", "-m", "lock")
        result = run_cli(
            scene.root, "verify", "--source", "head",
            "--write-baseline", "bv.baseline.json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(
            (scene.root / "bv.baseline.json").read_text(encoding="utf-8")
        )

    def test_a_freshly_written_baseline_validates(self):
        """The contrast, without which the refusals below prove nothing."""
        with Scenario() as scene:
            document = self._baseline_document(scene)
            self.assertEqual(document["$schema"], BASELINE_SCHEMA_URL)
            self.assertEqual(document["schema"], BASELINE_SCHEMA)
            self.assertEqual(validate_baseline(dict(document)), document)

    def test_a_baseline_from_another_release_is_refused(self):
        with Scenario() as scene:
            document = self._baseline_document(scene)
            for tag in ("v0.14.1", "v0.13.0", "v9.9.9"):
                with self.subTest(tag=tag):
                    older = dict(document)
                    older["$schema"] = _older(BASELINE_SCHEMA_URL, tag)
                    with self.assertRaises(BaselineError) as caught:
                        validate_baseline(older)
                    message = str(caught.exception)
                    self.assertIn("unsupported", message)
                    self.assertIn("regenerate", message)

    def test_the_refusal_reaches_the_user_as_a_could_not_check(self):
        """Exit 2 is boundver's 'could not check', not 'drift found'."""
        with Scenario() as scene:
            document = self._baseline_document(scene)
            document["$schema"] = _older(BASELINE_SCHEMA_URL, "v0.14.1")
            (scene.root / "bv.baseline.json").write_text(
                json.dumps(document, indent=2), encoding="utf-8"
            )
            scene.git("add", "--all")
            scene.git("commit", "-m", "baseline from an older release")
            result = run_cli(
                scene.root, "verify", "--source", "head",
                "--baseline", "bv.baseline.json",
            )
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("unsupported", result.stderr)
            self.assertIn("regenerate", result.stderr)

    def test_a_missing_or_empty_schema_field_is_refused_too(self):
        with Scenario() as scene:
            document = self._baseline_document(scene)
            for label, value in (("empty", ""), ("wrong host", "https://example.invalid/x")):
                with self.subTest(case=label):
                    broken = dict(document)
                    broken["$schema"] = value
                    with self.assertRaises(BaselineError):
                        validate_baseline(broken)


if __name__ == "__main__":
    unittest.main()
