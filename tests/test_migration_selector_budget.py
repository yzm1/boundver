"""The migration selector budget, at the boundary rather than past it.

`analyze_selector_migration` charges every matching operation against one
operation-wide ceiling, because the work is driven by a repository's own
selectors and a missing bound is a way to make boundver expensive. The refusal
is asserted elsewhere by building an analysis far over the limit. What that
cannot see is the arithmetic: loosening the comparison by a single evaluation
leaves the refusal working and the limit one step higher than it says, and no
test noticed (MUT-CONFIG-205).

An off-by-one in a charge is only visible against an exact requirement, so this
records what one fixed analysis costs and asserts the budget binds exactly
there. The number is a measurement of this fixture, not a contract: if a
legitimate change adds or removes a charge site, it moves, and updating it in
the same commit is the point. A change nobody intended moves it too, which is
what the assertion is for.

The neighbouring guard - that a charge may not be negative - has no reachable
caller. `spend_evaluations` is only ever handed to matching helpers as their
step consumer, and every one of them charges a counted number of steps, so no
public entry point can drive it negative. It is recorded as such in the mutant
catalog rather than given a test that would have to reach past the API.

Covers OBL-CONFIG-036.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import boundver._migration_analysis as migration
from boundver._git import _capture_git_source_snapshot
from boundver._utils import GuardrailError

from tests._scenarios import Scenario

#: What the fixture below costs, measured. See the module docstring: a change
#: here is either a deliberate change to what gets charged, which belongs in
#: the same commit, or a bug in the charging.
REQUIRED_EVALUATIONS = 248


class SelectorBudgetBoundaryTests(unittest.TestCase):
    """One fixed analysis, and the exact budget it needs to complete."""

    CONFIG = {
        "project": "p",
        "components": {
            "svc": {
                "path": "svc",
                "boundary": {"provider": "openapi", "paths": ["api/*.yaml"]},
            }
        },
        "slices": {},
    }

    def _analyze(self, scene, snapshot, limit):
        with patch.object(
            migration, "MAX_SELECTOR_MATCH_EVALUATIONS", limit
        ):
            return migration.analyze_selector_migration(
                self.CONFIG,
                scene.root,
                source="head",
                snapshot=snapshot,
                lock_path="old.lock.json",
                lock_schema="boundary-lock/v3",
                migration_action="regenerate",
                migration_reason="regeneration required",
            )

    def _scene(self):
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api/*.yaml"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("svc/api/v2.yaml", "openapi: 3.1.0\n")
        scene.commit()
        return scene, _capture_git_source_snapshot(scene.root, "head")

    def test_the_analysis_completes_on_exactly_its_required_budget(self):
        scene, snapshot = self._scene()
        try:
            analysis = self._analyze(scene, snapshot, REQUIRED_EVALUATIONS)
            self.assertEqual(len(analysis["declarations"]), 1)
        finally:
            scene.close()

    def test_one_evaluation_short_is_refused(self):
        """The boundary: the budget must bind at the step it claims."""
        scene, snapshot = self._scene()
        try:
            with self.assertRaises(GuardrailError) as raised:
                self._analyze(scene, snapshot, REQUIRED_EVALUATIONS - 1)
            self.assertIn("aggregate matching-work", str(raised.exception))
        finally:
            scene.close()

    def test_the_refusal_names_the_limit_it_enforced(self):
        """A user told only 'too much work' cannot act on it."""
        scene, snapshot = self._scene()
        try:
            with self.assertRaises(GuardrailError) as raised:
                self._analyze(scene, snapshot, 1)
            self.assertIn("1-step", str(raised.exception))
        finally:
            scene.close()

    def test_a_generous_budget_is_not_what_makes_it_pass(self):
        """The contrast: the fixture is nowhere near the shipped ceiling."""
        self.assertLess(
            REQUIRED_EVALUATIONS, migration.MAX_SELECTOR_MATCH_EVALUATIONS
        )
        scene, snapshot = self._scene()
        try:
            analysis = self._analyze(
                scene, snapshot, migration.MAX_SELECTOR_MATCH_EVALUATIONS
            )
            self.assertEqual(len(analysis["declarations"]), 1)
        finally:
            scene.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
