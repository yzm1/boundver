"""Config-level contracts with nothing asserting them.

`config` was the thinnest subsystem in the register, three asserted of
fifty-five. These are the entries whose obligations need no oracle and no
fixture beyond a repository: what a facet list means, where a hint is printed,
and what a component may declare about itself.

The remaining divergence is isolated behind direct use of a graph helper with
a configuration the public validation path refuses.

Covers OBL-CONFIG-001, OBL-CONFIG-005, OBL-CONFIG-007 and OBL-CONFIG-009.
"""

from __future__ import annotations

import json
import unittest

from boundver._config import validate_config
from boundver._consumer_graph import affected_consumer_groups

from tests._parity import run_cli
from tests._scenarios import Scenario

FACETS = ("exact", "behavior", "boundary", "compat")


def _locked(defaults=None, slices=()) -> Scenario:
    """A committed repository with a committed lock."""
    scene = Scenario()
    scene.component("svc", path="svc", boundary=["api"])
    scene.component("sdk", path="sdk", provider="leaf")
    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
    scene.file("sdk/index.ts", "export const x = 1;\n")
    for name, mode, members in slices:
        scene.slice(name, mode=mode, components=members)
    if defaults is not None:
        scene.config["defaults"] = defaults
    scene.commit()
    run_cli(scene.root, "generate", "--source", "head")
    scene.commit("lock")
    return scene


def _capable(defaults=None) -> Scenario:
    """One component that can produce all four facets, so no gate is impossible."""
    scene = Scenario()
    scene.component(
        "svc", path="svc", boundary=["api"], behavior=["api"],
        version_source={"file": "version.json", "field": "version"},
    )
    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
    scene.json_file("svc/version.json", {"version": "1.2.3"})
    if defaults is not None:
        scene.config["defaults"] = defaults
    scene.commit()
    run_cli(scene.root, "generate", "--source", "head")
    scene.commit("lock")
    return scene


class FacetSelectionTests(unittest.TestCase):
    """OBL-CONFIG-001 and OBL-CONFIG-009: what a facet list may say."""

    def test_an_empty_facet_list_is_refused_rather_than_meaning_anything(self):
        """The obligation asks what an empty list gates. The answer is that
        the schema refuses it, so neither reading arises.

        That is a stronger contract than either alternative the obligation
        weighs, and it is the one to pin: an empty list is a config the user
        did not mean, and guessing which of "gate nothing" or "gate
        everything" they wanted would be worse than saying so.
        """
        with _locked({"verify_facets": []}) as scene:
            result = run_cli(scene.root, "verify", "--source", "head")
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("non-empty", result.stderr)

    def test_a_facet_is_accepted_when_every_component_can_produce_it(self):
        for facet in FACETS:
            with self.subTest(facet=facet), _capable({"verify_facets": [facet]}) as scene:
                result = run_cli(scene.root, "verify", "--source", "head")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_gating_a_facet_a_component_cannot_produce_names_the_component(self):
        """The refusal must say which component and which missing input.

        A leaf has no boundary paths and no behavior paths, and neither
        component here declares a version source, so three of the four facets
        are impossible for a reason the user can act on.
        """
        for facet, missing in (
            ("behavior", "has no behavior.paths"),
            ("boundary", "has no boundary paths"),
            ("compat", "has no version_source"),
        ):
            with self.subTest(facet=facet), _locked({"verify_facets": [facet]}) as scene:
                result = run_cli(scene.root, "verify", "--source", "head")
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn(f"explicitly gates '{facet}'", result.stderr)
                self.assertIn(missing, result.stderr)

    def test_exact_is_the_one_facet_every_component_can_always_produce(self):
        with _locked({"verify_facets": ["exact"]}) as scene:
            result = run_cli(scene.root, "verify", "--source", "head")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_gated_facet_makes_drift_fail(self):
        with _locked({"verify_facets": ["exact"]}) as scene:
            scene.append_line("svc/api/v1.yaml", "drift: true")
            scene.commit("drift")
            result = run_cli(scene.root, "verify", "--source", "head")
            self.assertEqual(result.returncode, 1, result.stdout)

    def test_an_unknown_facet_in_the_config_names_the_config(self):
        """OBL-CONFIG-009: the user must learn where the bad name came from."""
        with _locked({"verify_facets": ["not-a-facet"]}) as scene:
            result = run_cli(scene.root, "verify", "--source", "head")
            self.assertEqual(result.returncode, 2)
            self.assertIn("defaults.verify_facets", result.stderr)
            self.assertIn("not-a-facet", result.stderr)
            for facet in FACETS:
                self.assertIn(facet, result.stderr)


class StdoutDisciplineTests(unittest.TestCase):
    """OBL-CONFIG-005: `--format json` must leave stdout parseable.

    A caller pipes stdout to a JSON parser. If a failure puts prose there, the
    parser crashes on the hint instead of reporting the failure, which is the
    worst of both outcomes.
    """

    SLICES = (("everything", "exact", ["sdk", "svc"]), ("api-only", "boundary", ["svc"]))

    def test_a_known_slice_prints_one_json_document(self):
        with _locked(slices=self.SLICES) as scene:
            result = run_cli(scene.root, "slice", "everything", "--format", "json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsInstance(json.loads(result.stdout), dict)

    def test_the_error_itself_goes_to_stderr(self):
        with _locked(slices=self.SLICES) as scene:
            result = run_cli(scene.root, "slice", "no-such-slice", "--format", "json")
            self.assertEqual(result.returncode, 2)
            self.assertIn("not found", result.stderr)

    def test_an_unknown_slice_leaves_stdout_parseable(self):
        """A failed JSON request emits no non-JSON data on stdout."""
        with _locked(slices=self.SLICES) as scene:
            result = run_cli(scene.root, "slice", "no-such-slice", "--format", "json")
            self.assertEqual(result.stdout, "")

    def test_the_available_hint_accompanies_the_error_on_stderr(self):
        with _locked(slices=self.SLICES) as scene:
            for arguments in (
                ["slice", "no-such-slice"],
                ["slice", "no-such-slice", "--format", "json"],
                ["slice", "no-such-slice", "--format", "text"],
            ):
                with self.subTest(arguments=" ".join(arguments)):
                    result = run_cli(scene.root, *arguments)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("Available:", result.stderr)
                    self.assertIn("everything", result.stderr)


class ConsumerSelfEdgeTests(unittest.TestCase):
    """OBL-CONFIG-007: a component cannot be its own consumer."""

    def _config(self) -> dict:
        return {
            "project": "self-edge",
            "components": {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "path-hash", "paths": ["api"]},
                    "consumers": ["svc", "sdk"],
                },
                "sdk": {"path": "sdk", "boundary": {"provider": "leaf", "paths": []}},
            },
        }

    def test_validation_refuses_the_declaration(self):
        with Scenario() as scene:
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.file("sdk/index.ts", "export const x = 1;\n")
            scene.config = self._config()
            scene.commit()
            errors = validate_config(scene.config, scene.root)
            self.assertTrue(errors)
            self.assertTrue(
                any("cannot consume its own boundary" in error for error in errors),
                errors,
            )

    def test_the_graph_does_not_report_a_component_as_its_own_consumer(self):
        """The public helper remains safe when called before validation."""
        groups = affected_consumer_groups(self._config()["components"], "svc")
        self.assertNotIn("svc", groups["components"])

    def test_an_ordinary_consumer_is_reported(self):
        groups = affected_consumer_groups(self._config()["components"], "svc")
        self.assertIn("sdk", groups["components"])


if __name__ == "__main__":
    unittest.main()
