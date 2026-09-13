"""Differential tests that use Git itself as the oracle.

Several obligations are of the form "boundver must agree with Git". Git is a
hard runtime dependency, so the comparison costs nothing and is the only
authority that settles them. `_git_oracle` shells out; nothing here restates a
Git rule in Python except `git_mode_for_permissions`, which quotes one.

Covers OBL-GIT-SOURCE-008, OBL-GIT-SOURCE-056 and OBL-GIT-SOURCE-102.
"""

from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path
from typing import List

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from boundver._git import _GitignoreRules

from tests import _git_oracle as oracle
from tests._scenarios import Scenario

PROFILE = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)

#: Permission sets Git and boundver classify alike: the owner execute bit
#: either decides the mode or is absent from every candidate.
AGREEING_PERMISSIONS = (0o644, 0o600, 0o400, 0o744, 0o755, 0o700, 0o111)

#: Permission sets where some execute bit is set but the owner's is not.
#: Git and boundver both classify these as ordinary files.
GROUP_OR_OTHER_EXECUTE_ONLY = (0o654, 0o645, 0o611, 0o050, 0o005, 0o010, 0o001)


def _regular_file_stat(permissions: int) -> os.stat_result:
    """A stat result for a regular file, so no filesystem is required.

    Windows keeps no execute bit, so a test that chmods a real file could only
    ever run on two of the three CI legs. `_working_tree_mode` accepts an
    injected stat, which makes the classifier testable everywhere.
    """
    return os.stat_result(
        (stat.S_IFREG | permissions, 1, 1, 1, 0, 0, 10, 0, 0, 0)
    )


def _classify(permissions: int) -> str:
    from boundver._git import _working_tree_mode

    mode, _object_type = _working_tree_mode(
        Path("."), "sample", path_stat=_regular_file_stat(permissions)
    )
    return mode


class WorkingTreeModeTests(unittest.TestCase):
    """OBL-GIT-SOURCE-008: the classifier must agree with Git's index."""

    def test_the_owner_execute_bit_decides_the_mode(self):
        for permissions in AGREEING_PERMISSIONS:
            with self.subTest(permissions=oct(permissions)):
                self.assertEqual(
                    _classify(permissions),
                    oracle.git_mode_for_permissions(permissions),
                )

    def test_group_and_other_execute_do_not_make_a_file_executable(self):
        """Group/other execute bits alone retain Git's ordinary-file mode."""
        for permissions in GROUP_OR_OTHER_EXECUTE_ONLY:
            with self.subTest(permissions=oct(permissions)):
                self.assertEqual(
                    _classify(permissions),
                    oracle.git_mode_for_permissions(permissions),
                )

    def test_group_and_other_execute_only_remain_mode_100644(self):
        """Pin the canonical mode independently of the shared oracle helper."""
        for permissions in GROUP_OR_OTHER_EXECUTE_ONLY:
            with self.subTest(permissions=oct(permissions)):
                self.assertEqual(oracle.git_mode_for_permissions(permissions), "100644")
                self.assertEqual(_classify(permissions), "100644")

    @unittest.skipIf(os.name == "nt", "Windows records no execute bit")
    def test_live_git_agrees_with_the_stated_rule(self):
        """The oracle quotes Git's rule; this checks the quote against Git."""
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            for permissions in AGREEING_PERMISSIONS + GROUP_OR_OTHER_EXECUTE_ONLY:
                scene.file(f"svc/p{permissions:o}.sh", "#!/bin/sh\n", mode=permissions)
            scene.commit()
            recorded = oracle.index_modes(scene.root)
            for permissions in AGREEING_PERMISSIONS + GROUP_OR_OTHER_EXECUTE_ONLY:
                with self.subTest(permissions=oct(permissions)):
                    self.assertEqual(
                        recorded[f"svc/p{permissions:o}.sh"],
                        oracle.git_mode_for_permissions(permissions),
                    )


def _verdicts(rules_text: str, paths: List[str], on_disk: List[str]):
    """Run one rule set past both boundver and Git, returning both answers."""
    with Scenario() as scene:
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")
        scene.file(".gitignore", rules_text)
        scene.commit()
        for path in on_disk:
            scene.file(path, "content\n")
        theirs = oracle.ignored_paths(scene.root, paths)
        rules = _GitignoreRules()
        for line in rules_text.splitlines():
            rules.add(line)
        return {path: rules.is_ignored(path) for path in paths}, {
            path: path in theirs for path in paths
        }


class GitignoreParityTests(unittest.TestCase):
    """OBL-GIT-SOURCE-056 and OBL-GIT-SOURCE-102: the fallback matcher."""

    def assertParity(self, rules_text, paths, on_disk=()):
        ours, theirs = _verdicts(rules_text, list(paths), list(on_disk))
        self.assertEqual(ours, theirs, f"rules: {rules_text.splitlines()}")

    def test_a_plain_name_matches_at_any_depth(self):
        self.assertParity("build\n", ["build", "a/build", "a/b/build", "builds"],
                          on_disk=["build", "a/build", "a/b/build", "builds"])

    def test_a_leading_slash_anchors_at_the_repository_root(self):
        self.assertParity("/root\n", ["root", "a/root"], on_disk=["root", "a/root"])

    def test_an_interior_slash_is_root_relative(self):
        self.assertParity("a/b\n", ["a/b", "x/a/b", "a/b/c"],
                          on_disk=["a/b/c", "x/a/b"])

    def test_a_trailing_double_star_matches_inside_but_not_the_entry(self):
        self.assertParity("a/**\n", ["a", "a/b", "a/b/c"], on_disk=["a/b/c"])

    def test_a_leading_double_star_matches_at_any_depth(self):
        self.assertParity("**/c\n", ["c", "a/c", "a/b/c"],
                          on_disk=["c", "a/c", "a/b/c"])

    def test_the_last_matching_rule_wins(self):
        self.assertParity("*.log\n!keep.log\n", ["a.log", "keep.log"],
                          on_disk=["a.log", "keep.log"])
        self.assertParity("!keep.log\n*.log\n", ["a.log", "keep.log"],
                          on_disk=["a.log", "keep.log"])

    def test_a_wildcard_rule_agrees(self):
        self.assertParity("*.o\nx*\n?.txt\n[ab].md\n",
                          ["f.o", "x1", "y.txt", "a.md", "c.md", "a/f.o"],
                          on_disk=["f.o", "x1", "y.txt", "a.md", "c.md", "a/f.o"])

    @PROFILE
    @given(
        rules=st.lists(
            st.sampled_from([
                "build", "*.log", "/root", "a/b", "a/**", "**/c", "*.o",
                "a/**/b", "x*", "?.txt", "[ab].md", "g/*", "**/e/**", "/a/b",
            ]),
            min_size=1,
            max_size=4,
            unique=True,
        ),
        path=st.sampled_from([
            "build", "build/x", "a/build/y", "x.log", "root", "a/root", "a/b",
            "a/b/c", "a/x/b", "c", "a/c", "f.o", "x1", "y.txt", "a.md",
            "g/h", "g/h/i", "e/z", "a/e/z/w", "root/x",
        ]),
    )
    def test_generated_rule_sets_without_negation_agree_with_git(self, rules, path):
        """Exercise the core rule grammar against Git's own answer."""
        assume(not any(rule.startswith("!") or rule.endswith("/") for rule in rules))
        ours, theirs = _verdicts("\n".join(rules) + "\n", [path], [])
        self.assertEqual(ours, theirs, f"rules: {rules}")


class GitignoreRegressionTests(unittest.TestCase):
    """Regressions that previously made the fallback disagree with Git."""

    def test_a_directory_only_rule_does_not_ignore_a_file_of_that_name(self):
        """`foo/` ignores the directory foo, never a regular file named foo.

        This distinction prevents a regular boundary file from being silently
        dropped from the fallback digest.
        """
        ours, theirs = _verdicts("foo/\n", ["foo"], ["foo"])
        self.assertEqual(ours, theirs)

    def test_an_excluded_parent_directory_cannot_be_re_included(self):
        """Git: "It is not possible to re-include a file if a parent directory
        of that file is excluded."

        With `*` then `!keep.log`, Git still excludes `a/keep.log` because `a`
        is excluded.
        """
        ours, theirs = _verdicts("*\n!keep.log\n", ["a/keep.log"], ["a/keep.log"])
        self.assertEqual(ours, theirs)

    def test_an_anchored_rule_does_not_match_a_descendant(self):
        """`!/root` names the root entry, not everything beneath it.

        With `x*` before it, `root/x` must remain ignored because the negation
        names the root entry only.
        """
        ours, theirs = _verdicts("x*\n!/root\n", ["root/x"], ["root/x"])
        self.assertEqual(ours, theirs)


if __name__ == "__main__":
    unittest.main()
