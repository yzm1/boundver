"""The ignore matcher boundver falls back to, measured against Git itself.

Three questions about the same code: whether a negation can resurrect a file
under an excluded directory, whether subtree pruning changes the answer, and
whether the fallback corpus agrees with Git's corpus.

Covers OBL-GIT-SOURCE-017, OBL-GIT-SOURCE-018 and OBL-GIT-SOURCE-019.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from boundver._git import (
    _GitignoreRules,
    _list_files_for_source,
    _list_unborn_working_tree_paths,
)

from tests import test_git_oracle as oracle_tests
from tests._scenarios import Scenario

#: Rule sets and the trees they apply to, chosen so two engage the pruning
#: optimisation and two contain the negation that disables it.
TREES = {
    "all excluded": ("build/\n", ["build/a.txt", "build/deep/b.txt", "keep.txt"]),
    "with a negation": (
        "build/\n!build/keep.txt\n",
        ["build/a.txt", "build/keep.txt", "keep.txt"],
    ),
    "star and negation": ("*\n!keep.log\n", ["a/keep.log", "keep.log", "other.txt"]),
    "nested ignore": ("a/\n", ["a/b/c.txt", "a/d.txt", "e.txt"]),
}


def _rules(rules_text: str) -> _GitignoreRules:
    rules = _GitignoreRules()
    for line in rules_text.splitlines():
        rules.add(line)
    return rules


def _plain_directory(rules_text: str, files) -> Path:
    """A directory that is not a Git repository, so the fallback lister runs."""
    root = Path(tempfile.mkdtemp())
    (root / ".gitignore").write_text(rules_text, encoding="utf-8")
    for name in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    return root


def _never_prune(self, rel_path: str) -> bool:
    return False


class DirectoryRuleNegationTests(unittest.TestCase):
    """OBL-GIT-SOURCE-017: an excluded parent cannot be re-entered."""

    def test_the_directory_rule_alone_agrees_with_git(self):
        """The premise: the exclusion itself is right."""
        ours, theirs = oracle_tests._verdicts(
            "build/\n", ["build/keep.txt"], ["build/keep.txt"]
        )
        self.assertEqual(ours, theirs)
        self.assertEqual(ours, {"build/keep.txt": True})

    def test_a_re_inclusion_under_that_rule_stays_ignored(self):
        """An excluded parent prevents a descendant from being re-included."""
        ours, theirs = oracle_tests._verdicts(
            "build/\n!build/keep.txt\n", ["build/keep.txt"], ["build/keep.txt"]
        )
        self.assertEqual(ours, theirs)

    def test_the_re_inclusion_answer_is_ignored_on_both_sides(self):
        ours, theirs = oracle_tests._verdicts(
            "build/\n!build/keep.txt\n", ["build/keep.txt"], ["build/keep.txt"]
        )
        self.assertEqual(theirs, {"build/keep.txt": True})
        self.assertEqual(ours, {"build/keep.txt": True})

    def test_the_same_shape_without_the_trailing_slash(self):
        """The rule spelling is not what decides it."""
        ours, theirs = oracle_tests._verdicts(
            "build\n!build/keep.txt\n", ["build/keep.txt"], ["build/keep.txt"]
        )
        self.assertEqual(theirs, {"build/keep.txt": True})
        self.assertEqual(ours, {"build/keep.txt": True})


class PruningEquivalenceTests(unittest.TestCase):
    """OBL-GIT-SOURCE-018: the optimisation must not change the answer."""

    def _listed(self, root: Path, *, prune: bool):
        if prune:
            return sorted(_list_files_for_source(root, ".", "working-tree"))
        with patch.object(_GitignoreRules, "can_prune_directory", _never_prune):
            return sorted(_list_files_for_source(root, ".", "working-tree"))

    def test_the_file_set_is_the_same_with_and_without_pruning(self):
        for label, (rules_text, files) in TREES.items():
            with self.subTest(tree=label):
                root = _plain_directory(rules_text, files)
                try:
                    self.assertEqual(
                        self._listed(root, prune=True),
                        self._listed(root, prune=False),
                    )
                finally:
                    shutil.rmtree(root, ignore_errors=True)

    def test_pruning_is_engaged_for_some_of_these_trees(self):
        """The premise: an equality that held because nothing pruned is empty."""
        engaged = {
            label: _rules(rules_text).can_prune_directory(
                next(name.split("/")[0] for name in files if "/" in name)
            )
            for label, (rules_text, files) in TREES.items()
        }
        self.assertTrue(any(engaged.values()), engaged)
        self.assertTrue(any(not value for value in engaged.values()), engaged)

    def test_a_negation_disables_pruning_entirely(self):
        """The mechanism: any negation makes the optimisation decline."""
        self.assertTrue(_rules("build/\n").can_prune_directory("build"))
        self.assertFalse(
            _rules("build/\n!build/keep.txt\n").can_prune_directory("build")
        )


class UnbornCorpusTests(unittest.TestCase):
    """OBL-GIT-SOURCE-019: equal, or every difference accounted for."""

    def _corpora(self, rules_text: str, files):
        with Scenario() as scene:
            (scene.root / ".gitignore").write_text(rules_text, encoding="utf-8")
            for name in files:
                target = scene.root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x\n", encoding="utf-8")
            native = sorted(_list_unborn_working_tree_paths(scene.root, "."))
        rules = _rules(rules_text)
        ours = sorted(
            name for name in [".gitignore", *files] if not rules.is_ignored(name)
        )
        return native, ours

    def test_the_two_corpora_agree_where_no_negation_applies(self):
        for label in ("all excluded", "nested ignore"):
            with self.subTest(tree=label):
                native, ours = self._corpora(*TREES[label])
                self.assertEqual(native, ours)

    def test_the_corpora_also_agree_when_negation_applies(self):
        for label in ("with a negation", "star and negation"):
            with self.subTest(tree=label):
                rules_text, files = TREES[label]
                native, ours = self._corpora(rules_text, files)
                self.assertEqual(native, ours)


if __name__ == "__main__":
    unittest.main()
