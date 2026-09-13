"""Which facets an edit moves, and which endpoint a finding points at.

"Moves exactly these facets" is only assertable when a facet's inputs can be
edited without touching another's. A version read from a file cannot do that -
the file lives in the component tree, so bumping it moves exact as well - but a
version read from a Git tag can, which is what makes the negative half of this
obligation testable at all.

The anchoring question is the same care applied to a location: a document that
was removed exists only at the base, so pointing a reader at the target would
send them to a file that is not there.

Covers OBL-FACETS-008 and OBL-REVIEW-001.
"""

from __future__ import annotations

import json
import unittest

from tests._parity import run_cli
from tests._scenarios import Scenario

OPENAPI = "openapi: 3.1.0\ninfo:\n  title: t\n  version: '1'\npaths: {}\n"

#: Every facet a component entry records.
FACETS = ("exact", "behavior", "boundary", "compat")


def _lock(scene, message: str) -> None:
    result = run_cli(scene.root, "generate", "--source", "head")
    assert result.returncode == 0, result.stderr
    scene.commit(message)


def _fingerprints(scene) -> dict:
    lock = json.loads((scene.root / "boundary.lock.json").read_text(encoding="utf-8"))
    return dict(lock["components"]["svc"]["fingerprints"])


class FacetAttributionTests(unittest.TestCase):
    """OBL-FACETS-008: exactly these facets, and no others."""

    #: Each edit, and the complete set of facets it may move.
    EDITS = {
        "a version tag": (lambda scene: scene.git("tag", "svc-v2.0.0"), {"compat"}),
        "a behavior file": (
            lambda scene: scene.append_line("svc/impl.py", "y = 2\n"),
            {"behavior", "exact"},
        ),
        "a boundary file": (
            lambda scene: scene.append_line("svc/api/v1.yaml", "x-note: a\n"),
            {"behavior", "boundary", "exact"},
        ),
        "an unselected file": (
            lambda scene: scene.append_line("svc/notes.md", "more\n"),
            {"exact"},
        ),
        "nothing": (lambda scene: None, set()),
    }

    def _build(self, scene) -> None:
        scene.config["components"] = {
            "svc": {
                "path": "svc",
                "boundary": {
                    "provider": "openapi-canonical", "paths": ["api/v1.yaml"]
                },
                "behavior": {"paths": ["api/v1.yaml", "impl.py"]},
                "version_source": {"git_tag_prefix": "svc-v"},
            }
        }
        scene.file("svc/api/v1.yaml", OPENAPI)
        scene.file("svc/impl.py", "x = 1\n")
        scene.file("svc/notes.md", "hello\n")

    def _moved_by(self, edit) -> set:
        with Scenario() as scene:
            self._build(scene)
            scene.commit()
            scene.git("tag", "svc-v1.0.0")
            _lock(scene, "lock")
            before = _fingerprints(scene)
            edit(scene)
            scene.commit("edit")
            _lock(scene, "relock")
            after = _fingerprints(scene)
            return {facet for facet in FACETS if before[facet] != after[facet]}

    def test_the_version_comes_from_a_tag_so_it_can_move_alone(self):
        """The premise: this fixture separates compat from the file tree."""
        with Scenario() as scene:
            self._build(scene)
            scene.commit()
            scene.git("tag", "svc-v1.0.0")
            _lock(scene, "lock")
            lock = json.loads(
                (scene.root / "boundary.lock.json").read_text(encoding="utf-8")
            )
            self.assertEqual(lock["components"]["svc"]["version"], "1.0.0")
            for facet in FACETS:
                self.assertIsNotNone(_fingerprints(scene)[facet], facet)

    def test_each_edit_moves_exactly_its_facets(self):
        for label, (edit, expected) in self.EDITS.items():
            with self.subTest(edit=label):
                self.assertEqual(self._moved_by(edit), expected)

    def test_a_version_bump_moves_compat_and_nothing_else(self):
        """The clause the gap said no fixture could support."""
        moved = self._moved_by(lambda scene: scene.git("tag", "svc-v2.0.0"))
        self.assertEqual(moved, {"compat"})

    def test_a_boundary_edit_reaches_behavior_because_coverage_requires_it(self):
        """Not a leak: validation refuses a behavior set that omits the artifact."""
        with Scenario() as scene:
            self._build(scene)
            scene.config["components"]["svc"]["behavior"] = {"paths": ["impl.py"]}
            scene.commit()
            scene.git("tag", "svc-v1.0.0")
            result = run_cli(scene.root, "generate", "--source", "head")
            self.assertEqual(result.returncode, 2)
            self.assertIn("must cover every boundary artifact", result.stderr)


class SourceLocationAnchorTests(unittest.TestCase):
    """OBL-REVIEW-001: a location points at the endpoint that has the file."""

    def _plan(self, edit, *, selector="api/*.yaml", extra_component=False) -> dict:
        with Scenario() as scene:
            scene.config["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {
                        "provider": "openapi-canonical", "paths": [selector]
                    },
                }
            }
            scene.file("svc/api/v1.yaml", OPENAPI)
            scene.file("svc/api/v2.yaml", OPENAPI.replace("'1'", "'2'"))
            scene.commit()
            _lock(scene, "lock")
            base = scene.head()
            edit(scene)
            scene.commit("edit")
            _lock(scene, "relock")
            result = run_cli(
                scene.root, "review", "--base", base, "--target", scene.head(),
                "--format", "plan",
            )
            assert result.returncode == 0, result.stderr
            return base, scene.head(), json.loads(result.stdout)

    def _statuses(self, plan: dict) -> dict:
        return {
            document["label"]: document["status"]
            for report in plan["structural_changes"]["reports"]
            for document in report.get("documents", [])
        }

    def test_a_removed_document_anchors_to_the_base(self):
        base, _target, plan = self._plan(
            lambda scene: (scene.root / "svc" / "api" / "v2.yaml").unlink()
        )
        self.assertEqual(
            self._statuses(plan), {"canonical:api/v2.yaml": "removed"}
        )
        self.assertEqual(len(plan["source_locations"]), 1)
        location = plan["source_locations"][0]
        self.assertEqual(location["endpoint"], "base")
        self.assertEqual(location["commit"], base)
        self.assertEqual(location["path"], "svc/api/v2.yaml")

    def test_an_added_document_anchors_to_the_target(self):
        def edit(scene):
            scene.file("svc/api/v3.yaml", OPENAPI.replace("'1'", "'3'"))

        _base, target, plan = self._plan(edit)
        self.assertEqual(self._statuses(plan), {"canonical:api/v3.yaml": "added"})
        location = plan["source_locations"][0]
        self.assertEqual(location["endpoint"], "target")
        self.assertEqual(location["commit"], target)
        self.assertEqual(location["path"], "svc/api/v3.yaml")

    def test_a_changed_document_anchors_to_the_target(self):
        def edit(scene):
            scene.append_line("svc/api/v2.yaml", "x-note: a\n")

        _base, target, plan = self._plan(edit)
        self.assertEqual(self._statuses(plan), {"canonical:api/v2.yaml": "changed"})
        location = plan["source_locations"][0]
        self.assertEqual(location["endpoint"], "target")
        self.assertEqual(location["commit"], target)

    def test_an_absent_endpoint_produces_no_location(self):
        """A component that exists at only one end has nowhere to point."""
        with Scenario() as scene:
            scene.config["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {
                        "provider": "openapi-canonical", "paths": ["api/v1.yaml"]
                    },
                }
            }
            scene.file("svc/api/v1.yaml", OPENAPI)
            scene.commit()
            _lock(scene, "lock")
            base = scene.head()
            scene.config["components"]["added"] = {
                "path": "added",
                "boundary": {
                    "provider": "openapi-canonical", "paths": ["api/v1.yaml"]
                },
            }
            scene.write_config()
            scene.file("added/api/v1.yaml", OPENAPI)
            scene.commit("add a component")
            _lock(scene, "relock")
            plan = json.loads(run_cli(
                scene.root, "review", "--base", base, "--target", scene.head(),
                "--format", "plan",
            ).stdout)
            reports = {
                report["component"]: report
                for report in plan["structural_changes"]["reports"]
            }
            self.assertEqual(reports["added"]["reason"], "component-absent")
            self.assertFalse(reports["added"]["inputs"]["base"]["present"])
            # The empty location list follows from the empty document list,
            # not from the presence check: an unavailable report carries no
            # documents for the anchoring loop to walk. Asserting both keeps
            # the reason visible.
            self.assertEqual(reports["added"]["documents"], [])
            self.assertEqual(plan["source_locations"], [])


if __name__ == "__main__":
    unittest.main()
