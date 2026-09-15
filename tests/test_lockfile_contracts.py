"""Three contracts around the lockfile: purity, isolation, and classification.

An exit code says what happened, a JSON stream says it to a machine, and a
partial regeneration must not damage the document it was handed. All three
must remain stable at the public API and CLI boundaries.

Covers OBL-LOCKFILE-002, OBL-LOCKFILE-005 and OBL-LOCKFILE-012.
"""

from __future__ import annotations

import copy
import json
import unittest

from boundver._lockfile import (
    generate_lockfile,
    generate_lockfile_for_components,
    verify_lockfile,
)
from boundver._utils import ConfigError
from boundver.core import _drift_exit_code

from tests._parity import run_cli
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2

#: Facet severities, from the documented exit-code contract.
FACET_CODES = {"exact": 1, "behavior": 3, "boundary": 4, "compat": 5}


def _locked() -> Scenario:
    scene = Scenario()
    scene.component("svc", path="svc", boundary=["api"])
    scene.component("other", path="other", provider="leaf")
    scene.slice("all", mode="exact", components=["other", "svc"])
    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
    scene.file("other/x.py", "x = 1\n")
    scene.commit()
    run_cli(scene.root, "generate", "--source", "head")
    scene.commit("lock")
    return scene


class SourceLockDetachmentTests(unittest.TestCase):
    """Composing a new lock must not reach into the caller's dict.

    generate_lockfile_for_components round-trips an existing lock through
    JSON before composing on top of it, so the caller's parsed object is
    never written to. A shallow copy would satisfy every test that only
    looks at the returned lock while sharing every nested structure with
    the input, and that is what MUT-LOCKFILE-212 did without being
    noticed. A library caller holding its own lock would see it change
    underneath.
    """

    def test_the_existing_lockfile_argument_is_not_mutated(self):
        with Scenario() as scene:
            scene.component("svc", path="svc", boundary=["api"])
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.commit()
            first = generate_lockfile(scene.config, scene.root, source="head")

            snapshot = json.loads(json.dumps(first))
            generate_lockfile_for_components(
                scene.config,
                scene.root,
                ["svc"],
                scene.root / "boundary.lock.json",
                source="head",
                existing_lockfile=first,
            )
            self.assertEqual(first, snapshot)

    def test_the_returned_lock_shares_no_nested_object_with_the_input(self):
        """The substance: equality would survive a shared sub-dict."""
        with Scenario() as scene:
            scene.component("svc", path="svc", boundary=["api"])
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.commit()
            first = generate_lockfile(scene.config, scene.root, source="head")
            produced = generate_lockfile_for_components(
                scene.config,
                scene.root,
                ["svc"],
                scene.root / "boundary.lock.json",
                source="head",
                existing_lockfile=first,
            )
            for name, entry in first.get("components", {}).items():
                with self.subTest(component=name):
                    self.assertIsNot(
                        produced.get("components", {}).get(name), entry
                    )


class StdoutPurityTests(unittest.TestCase):
    """OBL-LOCKFILE-002: one JSON document and nothing else, every exit path."""

    #: Commands that succeed, and the failing invocations worth checking.
    SUCCEEDING = (
        ("status", ["status"]),
        ("verify", ["verify", "--source", "head"]),
        ("verify with empty facet separators", ["verify", "--source", "head",
                                                 "--facets", ","]),
        ("why", ["why", "svc", "--source", "head"]),
        ("slice", ["slice", "all"]),
        ("discover", ["discover"]),
    )
    FAILING = (
        ("why with an unknown component", ["why", "nope", "--source", "head"]),
        ("verify with an unavailable explicit facet", ["verify", "--source", "head",
                                                        "--facets", "behavior"]),
    )
    UNKNOWN_SLICE = ("slice with an unknown name", ["slice", "nope"])

    def _stdout(self, scene: Scenario, arguments):
        result = run_cli(scene.root, *arguments, "--format", "json")
        return result.returncode, (result.stdout or "").strip()

    def test_every_succeeding_command_emits_one_document(self):
        with _locked() as scene:
            for label, arguments in self.SUCCEEDING:
                with self.subTest(command=label):
                    code, body = self._stdout(scene, arguments)
                    self.assertEqual(code, 0, body)
                    self.assertIsInstance(json.loads(body), dict)

    def test_a_failing_command_leaves_stdout_parseable(self):
        with _locked() as scene:
            for label, arguments in self.FAILING:
                with self.subTest(command=label):
                    _code, body = self._stdout(scene, arguments)
                    if body:
                        json.loads(body)

    def test_an_unavailable_facet_error_can_still_carry_a_document(self):
        """A semantic preflight refusal still preserves machine output."""
        with _locked() as scene:
            code, body = self._stdout(
                scene, ["verify", "--source", "head", "--facets", "behavior"]
            )
            self.assertEqual(code, COULD_NOT_CHECK)
            self.assertIsInstance(json.loads(body), dict)

    def test_no_command_writes_prose_to_stdout(self):
        """An unknown slice leaves the machine-output channel empty."""
        with _locked() as scene:
            _code, body = self._stdout(scene, self.UNKNOWN_SLICE[1])
            if body:
                json.loads(body)

    def test_an_unknown_slice_emits_no_stdout(self):
        with _locked() as scene:
            _code, body = self._stdout(scene, self.UNKNOWN_SLICE[1])
            self.assertEqual(body, "")
            for label, arguments in self.SUCCEEDING + self.FAILING:
                with self.subTest(command=label):
                    _code, other = self._stdout(scene, arguments)
                    if other:
                        json.loads(other)


class PartialGenerationIsolationTests(unittest.TestCase):
    """OBL-LOCKFILE-005: the caller's document must come back untouched."""

    def _fixture(self):
        scene = Scenario()
        scene.component("a", path="a", boundary=["api"])
        scene.component("b", path="b", provider="leaf")
        scene.file("a/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("b/x.py", "x = 1\n")
        scene.commit()
        return scene, scene.generate()

    def _unchanged(self, scene, selected, existing) -> bool:
        before = json.dumps(existing, sort_keys=True)
        try:
            generate_lockfile_for_components(
                scene.config, scene.root, selected,
                scene.root / "boundary.lock.json",
                source="head", existing_lockfile=existing,
            )
        except ConfigError:
            pass
        return json.dumps(existing, sort_keys=True) == before

    def test_a_successful_partial_generation_leaves_the_input_alone(self):
        scene, full = self._fixture()
        try:
            self.assertTrue(self._unchanged(scene, ["a"], copy.deepcopy(full)))
        finally:
            scene.close()

    def test_an_unknown_component_leaves_the_input_alone(self):
        scene, full = self._fixture()
        try:
            self.assertTrue(self._unchanged(scene, ["nope"], copy.deepcopy(full)))
        finally:
            scene.close()

    def test_a_stale_unselected_component_leaves_the_input_alone(self):
        """The error path the obligation names, where a rewrite is tempting."""
        scene, full = self._fixture()
        try:
            existing = copy.deepcopy(full)
            existing["components"].pop("b")
            self.assertTrue(self._unchanged(scene, ["a"], existing))
        finally:
            scene.close()

    def test_the_error_paths_really_do_raise(self):
        """Without this the isolation above could hold trivially."""
        scene, full = self._fixture()
        try:
            for selected, existing in (
                (["nope"], copy.deepcopy(full)),
                (["a"], {k: v for k, v in copy.deepcopy(full).items()
                         if k != "components"}),
            ):
                with self.subTest(selected=selected):
                    with self.assertRaises(ConfigError):
                        generate_lockfile_for_components(
                            scene.config, scene.root, selected,
                            scene.root / "boundary.lock.json",
                            source="head", existing_lockfile=existing,
                        )
        finally:
            scene.close()


class ExitCodeClassificationTests(unittest.TestCase):
    """OBL-LOCKFILE-012: a fail-closed condition is not drift."""

    def test_each_facet_mismatch_maps_to_its_severity(self):
        for facet, code in FACET_CODES.items():
            with self.subTest(facet=facet):
                issue = f"MISMATCH svc.{facet}: lockfile=a current=b"
                self.assertEqual(_drift_exit_code([issue]), code)

    def test_an_unavailable_facet_is_a_could_not_check(self):
        """The contrast: this fail-closed string is classified correctly."""
        self.assertEqual(
            _drift_exit_code(["UNAVAILABLE FACET svc.compat: no version source"]),
            COULD_NOT_CHECK,
        )

    def test_verify_returns_the_malformed_config_string_alone(self):
        """The premise: this really is a sole issue verify_lockfile produces."""
        scene = Scenario()
        try:
            scene.component("svc", path="svc", boundary=["api"])
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.commit()
            lock = scene.generate()
            for broken in ({"project": "p", "components": "nope"},
                           {"project": "p", "components": {}}):
                with self.subTest(config=broken["components"]):
                    issues = verify_lockfile(broken, lock, scene.root, source="head")
                    self.assertEqual(
                        issues, ["Config malformed: components must be a non-empty object"]
                    )
        finally:
            scene.close()

    def test_a_malformed_config_is_a_could_not_check(self):
        """Malformed inputs are usage failures rather than repository drift.

        A broken config means boundver could not check, not that the contract
        moved. A job routing on exit 1 treats a typo in the config as a
        boundary change.
        """
        for issue in (
            "Config malformed: components must be a non-empty object",
            "Config malformed: slices must be an object",
            "Lockfile schema mismatch: expected boundary-lock/v3",
        ):
            self.assertEqual(_drift_exit_code([issue]), COULD_NOT_CHECK, issue)

    def test_each_malformed_string_maps_to_could_not_check(self):
        for issue in (
            "Config malformed: components must be a non-empty object",
            "Config malformed: slices must be an object",
            "Lockfile schema mismatch: expected boundary-lock/v3",
        ):
            with self.subTest(issue=issue[:32]):
                self.assertEqual(_drift_exit_code([issue]), COULD_NOT_CHECK)


if __name__ == "__main__":
    unittest.main()
