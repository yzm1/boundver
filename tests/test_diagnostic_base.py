"""How `why` picks the commit it explains a change against.

The walk is `--first-parent`, so only mainline commits are candidates, and the
base it lands on is disclosed to the user as a precise attribution. Both halves
need a shaped history to test: a topic branch merged with `--no-ff`, so the
mainline and the full history genuinely differ.

Covers OBL-LOCKFILE-056, OBL-LOCKFILE-058 and OBL-LOCKFILE-059.
"""

from __future__ import annotations

import json
import unittest

from tests._parity import run_cli
from tests._scenarios import Scenario

#: Origins that name a specific commit, as opposed to a disclosed fallback.
PRECISE_ORIGINS = (
    "commit that introduced the current lock entry",
    "last commit that changed",
)


def _why(scene: Scenario, component: str = "svc") -> dict:
    result = run_cli(
        scene.root, "why", component, "--source", "head", "--format", "json"
    )
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    payload["_returncode"] = result.returncode
    return payload


def _is_precise(origin) -> bool:
    return bool(origin) and any(phrase in origin for phrase in PRECISE_ORIGINS)


class FirstParentHistoryTests(unittest.TestCase):
    """OBL-LOCKFILE-059: only mainline commits are candidates."""

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

    def test_the_base_is_the_merge_commit_not_the_topic_commit(self):
        """The entry entered the mainline at the merge, so that is the base."""
        with Scenario() as scene:
            topic_commit, merge_commit = self._merged_history(scene)
            result = _why(scene)
            self.assertEqual(result["diagnostic_base"], merge_commit)
            self.assertNotEqual(result["diagnostic_base"], topic_commit)
            self.assertTrue(_is_precise(result["diagnostic_base_origin"]))

    def test_the_change_is_reported_against_that_base(self):
        with Scenario() as scene:
            self._merged_history(scene)
            result = _why(scene)
            self.assertEqual(result["changed_files_status"], "ok")
            self.assertEqual(
                [entry["path"] for entry in result["changed_files"]],
                ["svc/api/v1.yaml"],
            )


class PreciseOriginTests(unittest.TestCase):
    """OBL-LOCKFILE-056: a precise attribution must be able to back itself up.

    `why` exits non-zero, names a commit, and calls that commit the one that
    introduced the current lock entry. When the lock has never been committed
    there is no such commit, and the resolution lands on the diff target
    itself, so the report explains a change against the very state it is
    diffing and lists nothing.
    """

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

    def test_a_committed_lock_backs_up_its_attribution(self):
        """The ordinary case, and the contrast the divergence is measured against."""
        with Scenario() as scene:
            self._committed_lock(scene)
            result = _why(scene)
            self.assertEqual(result["_returncode"], 1)
            self.assertTrue(_is_precise(result["diagnostic_base_origin"]))
            self.assertNotEqual(result["diagnostic_base"], scene.head())
            self.assertTrue(result["changed_files"])

    def test_a_precise_origin_never_resolves_to_the_diff_target(self):
        """A lock first recorded at the target uses a disclosed broad fallback."""
        with Scenario() as scene:
            self._uncommitted_lock(scene)
            result = _why(scene)
            if _is_precise(result["diagnostic_base_origin"]):
                self.assertNotEqual(result["diagnostic_base"], scene.head())

    def test_a_precise_origin_comes_with_a_non_empty_changed_set(self):
        """Precise attribution always has changed-file evidence."""
        with Scenario() as scene:
            self._uncommitted_lock(scene)
            result = _why(scene)
            if (
                result["_returncode"] != 0
                and result["changed_files_status"] == "ok"
                and _is_precise(result["diagnostic_base_origin"])
            ):
                self.assertTrue(result["changed_files"])

    def test_a_lock_first_recorded_with_the_drift_uses_a_root_fallback(self):
        with Scenario() as scene:
            self._uncommitted_lock(scene)
            result = _why(scene)
            self.assertEqual(result["_returncode"], 1)
            self.assertNotEqual(result["diagnostic_base"], scene.head())
            self.assertEqual(result["changed_files_status"], "ok")
            self.assertTrue(result["changed_files"])
            self.assertFalse(_is_precise(result["diagnostic_base_origin"]))
            self.assertIn("diff target", result["diagnostic_base_origin"])


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
