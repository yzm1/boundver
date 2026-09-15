"""Repairing a lock, and capturing a baseline from a degenerate one.

`migrate-lock` rewrites a lock in place, so what it refuses matters as much as
what it converts: a document it cannot actually repair must not be written back
under a success code. Baseline capture reads the same document for its context
strings, so it meets the same degenerate inputs from the other side.

Covers OBL-LOCKFILE-004 and OBL-LOCKFILE-010.
"""

from __future__ import annotations

import json
import unittest

from boundver._baseline import (
    MAX_BASELINE_TEXT,
    BaselineError,
    baseline_context,
    create_baseline,
)
from boundver._config import validate_config

from tests._parity import run_cli
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2

#: One character past the baseline text limit. The project name is the
#: shortest route to a capture-time field that config validation accepts and
#: the baseline document refuses, which is what makes it a useful probe.
OVER_LONG_PROJECT = "p" * (MAX_BASELINE_TEXT + 1)


def _locked() -> Scenario:
    scene = Scenario()
    scene.component("svc", path="svc", boundary=["api"])
    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
    scene.commit()
    result = run_cli(scene.root, "generate", "--source", "head")
    assert result.returncode == 0, result.stderr
    return scene


def _locked_with_project(project: str) -> Scenario:
    """A repository with a working, committed lock under a chosen project name.

    `_locked` leaves the generated lock uncommitted, because the migrate-lock
    tests rewrite it. Baseline capture instead needs the lock committed, since
    it runs against ``--source head``, so this helper commits it as well.
    """
    scene = Scenario(project=project)
    scene.component("svc", path="svc", boundary=["api"])
    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
    scene.commit()
    generated = run_cli(scene.root, "generate", "--source", "head")
    assert generated.returncode == 0, generated.stderr
    scene.commit("lock")
    return scene


class MigrateLockTests(unittest.TestCase):
    """OBL-LOCKFILE-004: a lock that cannot be repaired must not be rewritten."""

    def _lock_path(self, scene: Scenario):
        return scene.root / "boundary.lock.json"

    def _write(self, scene: Scenario, document: dict) -> None:
        self._lock_path(scene).write_text(
            json.dumps(document, indent=2), encoding="utf-8"
        )

    def _read(self, scene: Scenario) -> dict:
        return json.loads(self._lock_path(scene).read_text(encoding="utf-8"))

    def test_an_intact_lock_verifies_before_anything_else(self):
        """The premise: the fixture starts from a working lock."""
        with _locked() as scene:
            self.assertEqual(
                run_cli(scene.root, "verify", "--source", "working-tree").returncode, 0
            )

    def test_a_lock_without_a_schema_is_refused(self):
        with _locked() as scene:
            document = self._read(scene)
            document.pop("schema")
            self._write(scene, document)
            result = run_cli(scene.root, "migrate-lock")
            self.assertEqual(result.returncode, COULD_NOT_CHECK)
            self.assertIn("schema", result.stderr)
            self.assertEqual(self._read(scene), document)

    def test_a_non_object_components_value_is_left_alone(self):
        with _locked() as scene:
            document = self._read(scene)
            document["components"] = "not an object"
            self._write(scene, document)
            run_cli(scene.root, "migrate-lock")
            self.assertEqual(self._read(scene), document)

    def test_a_lock_missing_its_components_key_is_not_normalised(self):
        """Migration cannot recover fingerprints from an absent component map.

        The command must fail without writing because no migration can infer
        the missing component fingerprints.
        """
        with _locked() as scene:
            document = self._read(scene)
            document.pop("components")
            self._write(scene, document)
            result = run_cli(scene.root, "migrate-lock")
            self.assertEqual(result.returncode, COULD_NOT_CHECK)
            self.assertIn("`boundver generate`", result.stderr)
            self.assertEqual(self._read(scene), document)

    def test_an_unrepairable_lock_stays_unmodified_and_unverifiable(self):
        with _locked() as scene:
            document = self._read(scene)
            document.pop("components")
            self._write(scene, document)

            before = run_cli(scene.root, "verify", "--source", "working-tree")
            self.assertEqual(before.returncode, COULD_NOT_CHECK)

            migrated = run_cli(scene.root, "migrate-lock")
            self.assertEqual(migrated.returncode, COULD_NOT_CHECK)
            self.assertEqual(self._read(scene), document)

            after = run_cli(scene.root, "verify", "--source", "working-tree")
            self.assertEqual(after.returncode, COULD_NOT_CHECK)


class BaselineCaptureTests(unittest.TestCase):
    """OBL-LOCKFILE-010: an actionable diagnostic, and an empty baseline."""

    def test_a_clean_run_writes_a_baseline_with_no_violations(self):
        with _locked() as scene:
            scene.commit("lock")
            written = run_cli(
                scene.root, "verify", "--source", "head",
                "--write-baseline", "clean.json",
            )
            self.assertEqual(written.returncode, 0, written.stderr)
            document = json.loads(
                (scene.root / "clean.json").read_text(encoding="utf-8")
            )
            self.assertEqual(document["violations"], [])

    def test_that_empty_baseline_loads_and_applies(self):
        """An empty baseline is the ordinary starting point for a ratchet."""
        with _locked() as scene:
            scene.commit("lock")
            run_cli(
                scene.root, "verify", "--source", "head",
                "--write-baseline", "clean.json",
            )
            scene.git("add", "--all")
            scene.git("commit", "-m", "baseline")
            applied = run_cli(
                scene.root, "verify", "--source", "head", "--baseline", "clean.json"
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)

    def test_a_degenerate_project_is_refused_with_an_actionable_message(self):
        """Not an internal field-type error from deep inside capture."""
        for label, value in (
            ("absent", None), ("empty", ""), ("non-string", 123),
        ):
            with self.subTest(project=label):
                scene = Scenario()
                try:
                    scene.component("svc", path="svc", boundary=["api"])
                    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
                    if value is None:
                        scene.config.pop("project")
                    else:
                        scene.config["project"] = value
                    scene.commit()
                    result = run_cli(
                        scene.root, "verify", "--source", "head",
                        "--write-baseline", "b.json",
                    )
                    self.assertEqual(result.returncode, COULD_NOT_CHECK)
                    self.assertIn("Config is invalid", result.stderr)
                    self.assertFalse((scene.root / "b.json").exists())
                finally:
                    scene.close()

    def test_an_over_long_project_is_valid_configuration(self):
        """The premise for MUT-LOCKFILE-451: config validation lets this pass.

        `validate_config` asks only that 'project' is a non-empty string with
        no surrounding whitespace, and the config JSON schema declares
        minLength 1 with no maximum, so a project name one character past the
        baseline text limit is a perfectly valid configuration. This premise is
        what separates the capture test below from the degenerate-project
        subtests above: those are refused by the config layer and would still
        be refused if baseline validation disappeared entirely, so without this
        assertion the next test could pass without exercising capture at all.
        """
        with _locked_with_project(OVER_LONG_PROJECT) as scene:
            self.assertEqual(validate_config(scene.config, scene.root), [])

    def test_capture_validates_the_baseline_document_it_returns(self):
        """MUT-LOCKFILE-451: `create_baseline` must validate its own payload.

        `create_baseline` assembles the baseline document and then hands it to
        `validate_baseline` before returning it. Nothing asserted that final
        call, so deleting it left every existing subtest green. It is
        load-bearing because the project string travels from the config into
        the baseline context unchecked, and only `validate_baseline` enforces
        the 4096-character text limit that `load_baseline` will later apply on
        the way back in. Without the call, `verify --write-baseline` writes a
        file that boundver refuses to read on the very next run.

        The command is asserted first, because that is the surface a user
        meets, and the direct call to `create_baseline` is asserted second, so
        that a refactor which drops the validation fails at the layer that owns
        it rather than only in the CLI.
        """
        with _locked_with_project(OVER_LONG_PROJECT) as scene:
            result = run_cli(
                scene.root, "verify", "--source", "head",
                "--write-baseline", "b.json",
            )
            self.assertEqual(result.returncode, COULD_NOT_CHECK, result.stdout)
            self.assertIn("exceeds the text limit", result.stderr)
            self.assertFalse((scene.root / "b.json").exists())

            lockfile = json.loads(
                (scene.root / "boundary.lock.json").read_text(encoding="utf-8")
            )
            context = baseline_context(
                config=scene.config,
                lockfile=lockfile,
                source="head",
                components_filter=[],
                facets=None,
                transitive=False,
                facet_policy={},
            )
            with self.assertRaises(BaselineError) as refusal:
                create_baseline(context, [])
            self.assertIn("baseline field 'project'", str(refusal.exception))

    def test_a_project_at_the_text_limit_is_still_captured(self):
        """The contrast for MUT-LOCKFILE-451: capture accepts the legal case.

        A validation step that refused every long project name would satisfy
        the test above while breaking real repositories, so this pins the other
        side of the boundary. A project of exactly MAX_BASELINE_TEXT characters
        is within the limit, and capture writes the baseline as usual.
        """
        with _locked_with_project("p" * MAX_BASELINE_TEXT) as scene:
            result = run_cli(
                scene.root, "verify", "--source", "head",
                "--write-baseline", "at-limit.json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(
                (scene.root / "at-limit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(document["project"]), MAX_BASELINE_TEXT)
            self.assertEqual(document["violations"], [])


if __name__ == "__main__":
    unittest.main()
