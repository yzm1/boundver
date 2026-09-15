"""Two published contracts: a bounded CI summary, and the baseline schema.

The summary is a presentation artifact with a hard byte budget, so it has to
refuse rather than truncate when the fixed parts alone will not fit. The
baseline schema is the document a third-party tool would generate against, so
anything the loader enforces beyond it is a constraint nobody outside can see.

Covers OBL-GRAPH-004 and OBL-FACETS-005.
"""

from __future__ import annotations

import copy
import io
import json
import unittest
from pathlib import Path

from boundver._baseline import BaselineError, validate_baseline
from boundver._review_plan import (
    MAX_PLAN_SUMMARY_BYTES,
    MAX_PLAN_SUMMARY_ROWS,
    render_review_plan_markdown,
)
from boundver._utils import GuardrailError

from tests._parity import run_cli
from tests._scenarios import Scenario

try:
    import jsonschema
except ImportError:  # pragma: no cover - jsonschema is a declared dev extra
    jsonschema = None

BASELINE_SCHEMA = (
    Path(__file__).resolve().parents[1] / "spec" / "verify-baseline.schema.json"
)

#: The floor the renderer refuses to go below, so a caller cannot ask for a
#: budget that could not hold the fixed parts under any input.
MINIMUM_BUDGET = 2048


def _review_plan(scene: Scenario, base: str, target: str) -> dict:
    result = run_cli(
        scene.root, "review", "--base", base, "--target", target, "--format", "plan"
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _reviewable() -> tuple:
    scene = Scenario()
    scene.component("svc", path="svc", boundary=["api"], consumers=["sdk"])
    scene.component("sdk", path="sdk", provider="leaf")
    scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
    scene.file("sdk/index.ts", "export const x = 1;\n")
    scene.commit()
    run_cli(scene.root, "generate", "--source", "head")
    scene.commit("lock")
    base = scene.head()
    scene.append_line("svc/api/v1.yaml", "change: true")
    scene.commit("edit")
    run_cli(scene.root, "generate", "--source", "head")
    scene.commit("relock")
    return scene, base, scene.head()


class PlanSummaryBudgetTests(unittest.TestCase):
    """OBL-GRAPH-004: the summary refuses rather than overflowing."""

    def test_an_ordinary_summary_fits_its_budget(self):
        scene, base, target = _reviewable()
        try:
            plan = _review_plan(scene, base, target)
            rendered = render_review_plan_markdown(plan)
            self.assertLessEqual(
                len(rendered.encode("utf-8")), MAX_PLAN_SUMMARY_BYTES
            )
            self.assertTrue(rendered.endswith("\n"))
        finally:
            scene.close()

    def test_the_declared_defaults_are_the_ones_used(self):
        self.assertGreaterEqual(MAX_PLAN_SUMMARY_BYTES, MINIMUM_BUDGET)
        self.assertGreater(MAX_PLAN_SUMMARY_ROWS, 0)

    def test_an_impossible_budget_is_refused_before_rendering(self):
        """`max_bytes` below the floor and a negative `max_rows` are usage
        errors, not something to discover halfway through a render."""
        scene, base, target = _reviewable()
        try:
            plan = _review_plan(scene, base, target)
            for arguments, expected in (
                ({"max_rows": -1}, "row limit must not be negative"),
                ({"max_bytes": MINIMUM_BUDGET - 1}, "at least 2048"),
                ({"max_bytes": 0}, "at least 2048"),
            ):
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError) as caught:
                        render_review_plan_markdown(plan, **arguments)
                    self.assertIn(expected, str(caught.exception))
        finally:
            scene.close()

    def test_a_summary_that_cannot_fit_raises_rather_than_truncating(self):
        """Truncating a size contract would produce a summary that lies."""
        scene, base, target = _reviewable()
        try:
            plan = _review_plan(scene, base, target)
            oversized = copy.deepcopy(plan)
            oversized["policy"] = dict(
                plan.get("policy", {}), impact="X" * 4000
            )
            with self.assertRaises(GuardrailError) as caught:
                render_review_plan_markdown(oversized, max_bytes=MINIMUM_BUDGET)
            self.assertIn("bounded presentation contract", str(caught.exception))
        finally:
            scene.close()

    def test_the_same_plan_fits_at_the_default_budget(self):
        """The contrast: the refusal is about the budget, not the plan."""
        scene, base, target = _reviewable()
        try:
            plan = _review_plan(scene, base, target)
            oversized = copy.deepcopy(plan)
            oversized["policy"] = dict(plan.get("policy", {}), impact="X" * 4000)
            rendered = render_review_plan_markdown(oversized)
            self.assertLessEqual(
                len(rendered.encode("utf-8")), MAX_PLAN_SUMMARY_BYTES
            )
        finally:
            scene.close()

    def test_an_incomplete_plan_is_refused(self):
        scene, base, target = _reviewable()
        try:
            plan = _review_plan(scene, base, target)
            for key, value in (("complete", False), ("schema", "not-a-plan")):
                with self.subTest(field=key):
                    broken = copy.deepcopy(plan)
                    broken[key] = value
                    with self.assertRaises(Exception) as caught:
                        render_review_plan_markdown(broken)
                    self.assertNotIsInstance(caught.exception, GuardrailError)
        finally:
            scene.close()


@unittest.skipIf(jsonschema is None, "jsonschema is not installed")
class BaselineSchemaParityTests(unittest.TestCase):
    """OBL-FACETS-005: the published schema is what a third party builds to.

    A generator outside this repository has the schema and nothing else. Every
    constraint the loader enforces beyond it is one that tool cannot know
    about, and it learns by being rejected.
    """

    def _baseline(self) -> dict:
        scene = Scenario()
        try:
            scene.component("svc", path="svc", boundary=["api"])
            scene.component("zz", path="zz", provider="leaf")
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.file("zz/x.py", "x = 1\n")
            scene.commit()
            run_cli(scene.root, "generate", "--source", "head")
            scene.commit("lock")
            scene.append_line("svc/api/v1.yaml", "drift: true")
            scene.commit("drift")
            result = run_cli(
                scene.root, "verify", "--source", "head",
                "--write-baseline", "bv.baseline.json",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(
                (scene.root / "bv.baseline.json").read_text(encoding="utf-8")
            )
        finally:
            scene.close()

    @staticmethod
    def _schema_accepts(document: dict) -> bool:
        schema = json.loads(io.open(BASELINE_SCHEMA, encoding="utf-8").read())
        try:
            jsonschema.validate(document, schema)
        except jsonschema.ValidationError:
            return False
        return True

    @staticmethod
    def _loader_accepts(document: dict) -> bool:
        try:
            validate_baseline(copy.deepcopy(document))
        except BaselineError:
            return False
        return True

    def test_a_written_baseline_satisfies_both(self):
        document = self._baseline()
        self.assertTrue(self._schema_accepts(document))
        self.assertTrue(self._loader_accepts(document))

    def test_the_fixture_has_enough_violations_to_reorder(self):
        """Without two, the ordering case below would prove nothing."""
        self.assertGreater(len(self._baseline()["violations"]), 1)

    def test_the_gap_is_ordering_and_id_derivation(self):
        """Pin the scope, so a partial fix cannot pass unnoticed."""
        document = self._baseline()

        unsorted = copy.deepcopy(document)
        unsorted["violations"] = list(reversed(unsorted["violations"]))
        self.assertTrue(self._schema_accepts(unsorted))
        self.assertFalse(self._loader_accepts(unsorted))

        forged = copy.deepcopy(document)
        forged["violations"][0]["id"] = "0" * 64
        self.assertTrue(self._schema_accepts(forged))
        self.assertFalse(self._loader_accepts(forged))

    def test_a_document_the_schema_rejects_is_rejected_by_the_loader_too(self):
        """The other direction holds, which is the half that matters most."""
        document = self._baseline()
        for field in ("project", "schema", "violations"):
            with self.subTest(missing=field):
                broken = copy.deepcopy(document)
                del broken[field]
                self.assertFalse(self._schema_accepts(broken))
                self.assertFalse(self._loader_accepts(broken))


if __name__ == "__main__":
    unittest.main()
