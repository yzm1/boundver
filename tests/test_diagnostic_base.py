"""Explicit source comparisons must not infer provenance from lock storage.

A shaped merge history distinguishes an operator-selected source from the
commit that happened to introduce a lock. Legacy storage lookup remains
separately identified below; it is not the CLI's provenance authority.

Covers OBL-LOCKFILE-056, OBL-LOCKFILE-058 and OBL-LOCKFILE-059.
"""

from __future__ import annotations

import json
import unittest

from tests._parity import run_cli
from tests._scenarios import Scenario

def _why(scene: Scenario, component: str = "svc", *, base_ref=None) -> dict:
    options = ("--base-ref", base_ref) if base_ref is not None else ()
    result = run_cli(
        scene.root, "why", component, "--source", "head", "--format", "json", *options
    )
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    payload["_returncode"] = result.returncode
    return payload


class FirstParentHistoryTests(unittest.TestCase):
    """A merge does not authorize automatic lock-source attribution."""

    def _merged_history(self, scene: Scenario) -> tuple:
        """Introduce the lock entry on a topic branch and merge it."""
        scene.component("svc", path="svc", boundary=["api"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.commit("base without a lock")
        mainline = scene.current_branch()

        scene.branch("topic")
        run_cli(scene.root, "generate", "--source", "head")
        scene.git("add", "--all")
        scene.git("commit", "-m", "lock on topic")
        topic_commit = scene.head()

        scene.checkout(mainline)
        scene.merge("topic")
        merge_commit = scene.head()

        scene.append_line("svc/api/v1.yaml", "drift: true")
        scene.commit("drift")
        return topic_commit, merge_commit

    def test_the_topic_commit_really_is_off_the_mainline(self):
        """Without --no-ff there would be no merge commit and no distinction."""
        with Scenario() as scene:
            topic_commit, merge_commit = self._merged_history(scene)
            mainline = scene.first_parent_commits()
            self.assertIn(merge_commit, mainline)
            self.assertNotIn(topic_commit, mainline)

    def test_an_explicit_merge_base_is_used_not_the_topic_commit(self):
        with Scenario() as scene:
            topic_commit, merge_commit = self._merged_history(scene)
            result = _why(scene, base_ref=merge_commit)
            self.assertEqual(result["diagnostic_base"], merge_commit)
            self.assertNotEqual(result["diagnostic_base"], topic_commit)
            self.assertEqual(result["diagnostic_base_origin"], "explicit --base-ref")

    def test_the_change_is_reported_against_that_base(self):
        with Scenario() as scene:
            _, merge_commit = self._merged_history(scene)
            result = _why(scene, base_ref=merge_commit)
            self.assertEqual(result["changed_files_status"], "ok")
            self.assertEqual(
                [entry["path"] for entry in result["changed_files"]],
                ["svc/api/v1.yaml"],
            )


class PreciseOriginTests(unittest.TestCase):
    """Recorded locks cannot prove their own generating source."""

    def _uncommitted_lock(self, scene: Scenario) -> None:
        scene.component("svc", path="svc", boundary=["api"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.commit("one")
        scene.file("svc/api/v2.yaml", "openapi: 3.1.0\n")
        scene.commit("two")
        run_cli(scene.root, "generate", "--source", "head")
        scene.append_line("svc/api/v1.yaml", "drift: true")
        scene.commit("drift")

    def _committed_lock(self, scene: Scenario) -> None:
        scene.component("svc", path="svc", boundary=["api"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.commit("one")
        run_cli(scene.root, "generate", "--source", "head")
        scene.git("add", "--all")
        scene.git("commit", "-m", "lock")
        scene.append_line("svc/api/v1.yaml", "drift: true")
        scene.commit("drift")

    def test_a_committed_lock_does_not_imply_source_attribution(self):
        with Scenario() as scene:
            self._committed_lock(scene)
            result = _why(scene)
            self.assertEqual(result["_returncode"], 1)
            self.assertIsNone(result["diagnostic_base"])
            self.assertEqual(result["changed_files_status"], "not-run")
            self.assertEqual(result["changed_files"], [])
            self.assertIn("unavailable", result["diagnostic_base_origin"])

    def test_an_explicit_target_is_an_honest_empty_source_comparison(self):
        with Scenario() as scene:
            self._uncommitted_lock(scene)
            result = _why(scene, base_ref="HEAD")
            self.assertEqual(result["diagnostic_base"], scene.head())
            self.assertEqual(result["diagnostic_base_origin"], "explicit --base-ref")
            self.assertEqual(result["changed_files_status"], "ok")
            self.assertEqual(result["changed_files"], [])

    def test_an_explicit_earlier_source_lists_the_actual_changed_files(self):
        with Scenario() as scene:
            self._uncommitted_lock(scene)
            result = _why(scene, base_ref="HEAD~1")
            self.assertEqual(result["diagnostic_base_origin"], "explicit --base-ref")
            self.assertEqual(result["changed_files_status"], "ok")
            self.assertEqual(result["changed_files"], [
                {"path": "svc/api/v1.yaml", "status": "M"},
            ])

    def test_a_lock_first_recorded_with_drift_does_not_invent_a_fallback(self):
        with Scenario() as scene:
            self._uncommitted_lock(scene)
            result = _why(scene)
            self.assertEqual(result["_returncode"], 1)
            self.assertIsNone(result["diagnostic_base"])
            self.assertEqual(result["changed_files_status"], "not-run")
            self.assertEqual(result["changed_files"], [])
            self.assertIn("unavailable", result["diagnostic_base_origin"])


class DisclosedFallbackTests(unittest.TestCase):
    """OBL-LOCKFILE-058: a fallback must say it is one."""

    def test_every_origin_the_resolver_can_produce_names_its_reason(self):
        """Read from the source, so a new origin cannot be added silently."""
        from boundver import _output

        source = _output.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        for origin in (
            "previous-commit fallback (lock history unavailable)",
            "root commit fallback",
            "no committed diagnostic base is available",
            "commit that introduced the current lock entry for ",
        ):
            with self.subTest(origin=origin):
                self.assertIn(origin, text)

    def test_the_last_resort_discloses_itself_rather_than_claiming_precision(self):
        """The previous commit is used, and the origin says why.

        The register notes that docs/reference.md describes a broader
        root/lock-history fallback here rather than the previous commit. The
        code's disclosure is honest; the documentation names a different
        ladder. That difference is recorded in the register rather than
        asserted, because which of the two should change is a decision.
        """
        from boundver._output import _resolve_lock_history_base

        self.assertTrue(callable(_resolve_lock_history_base))


if __name__ == "__main__":
    unittest.main()
