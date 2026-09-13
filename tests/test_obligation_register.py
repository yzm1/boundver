"""Regression tests retained from the v0.16 testing-obligation review.

The obligation identifiers preserve traceability to the private review record.
The tests themselves are the maintained public evidence and have no dependency
on that source material.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from boundver import _git as git_helpers
from boundver import _hashing as hashing
from boundver import _migration_analysis as migration_analysis
from boundver import _utils as utils
from boundver._utils import GuardrailError


class ModeAwareBytesTransformTests(unittest.TestCase):
    """OBL-GIT-SOURCE-001.

    `_ModeAwareBytes.replace` must return a `_ModeAwareBytes` carrying the same
    `git_mode`, `git_object_type` and `source_size` as the receiver, so the raw
    path-hash provider's CRLF normalization cannot silently downgrade a file
    entry and lose its mode binding.

    The register recorded both halves as tested and never composed: the CRLF
    path runs on plain `bytes`, and the mode-binding fixtures use content with
    no CRLF, so this override is never entered by the existing suite.
    """

    def test_replace_preserves_mode_type_and_source_size(self):
        value = hashing._ModeAwareBytes(
            b"first\r\nsecond\r\n", "100755", "blob", source_size=15
        )

        replaced = value.replace(b"\r\n", b"\n")

        self.assertEqual(bytes(replaced), b"first\nsecond\n")
        self.assertIsInstance(replaced, hashing._ModeAwareBytes)
        self.assertEqual(replaced.git_mode, "100755")
        self.assertEqual(replaced.git_object_type, "blob")

    def test_replace_keeps_the_pre_normalization_source_size(self):
        # The whole point of carrying source_size separately: normalization
        # shrinks the bytes, and I/O budgets must still charge what was read.
        value = hashing._ModeAwareBytes(b"a\r\nb", "100644", source_size=4)

        replaced = value.replace(b"\r\n", b"\n")

        self.assertEqual(len(bytes(replaced)), 3)
        self.assertEqual(replaced.source_size, 4)

    def test_replace_preserves_a_non_default_object_type(self):
        value = hashing._ModeAwareBytes(b"x\r\n", "120000", "symlink", source_size=3)

        replaced = value.replace(b"\r\n", b"\n")

        self.assertEqual(replaced.git_mode, "120000")
        self.assertEqual(replaced.git_object_type, "symlink")

    def test_replace_with_no_match_still_returns_a_mode_aware_value(self):
        value = hashing._ModeAwareBytes(b"no line endings here", "100644")

        replaced = value.replace(b"\r\n", b"\n")

        self.assertIsInstance(replaced, hashing._ModeAwareBytes)
        self.assertEqual(replaced.git_mode, "100644")
        self.assertEqual(replaced.source_size, len(b"no line endings here"))


class OfflineGitCommandScopedConfigTests(unittest.TestCase):
    """OBL-GIT-SOURCE-007.

    A command-scoped `-c` is accepted only when the next token literally starts
    with `safe.directory=`. Anything else is a configuration-injection vector
    and must raise before any process is built.

    The register found no test passing `-c` to `_offline_git_command` at all, so
    the prefix-walk loop was exercised only on its accept path.
    """

    REPO = Path("repo")
    UNSAFE_MESSAGE = "Refusing unsafe command-scoped Git configuration"

    def test_rejects_command_scoped_configuration_that_is_not_safe_directory(self):
        rejected = (
            ["-c", "core.pager=sh", "config", "--get", "core.bare"],
            ["-c", "include.path=/tmp/evil", "config", "--get", "core.bare"],
            ["-c", "core.fsmonitor=sh -c evil", "status", "--short"],
            # A near miss: the check is a literal prefix, so this must not pass.
            ["-c", "safe.directoryX=/repo", "config", "--get", "core.bare"],
            # A dangling -c has no value to inspect.
            ["-c"],
        )
        for args in rejected:
            with self.subTest(args=args):
                with self.assertRaises(ValueError) as caught:
                    git_helpers._offline_git_command(self.REPO, list(args))
                self.assertIn(self.UNSAFE_MESSAGE, str(caught.exception))

    def test_accepts_safe_directory_before_an_allowlisted_subcommand(self):
        command = git_helpers._offline_git_command(
            self.REPO, ["-c", "safe.directory=/repo", "config", "--get", "core.bare"]
        )

        self.assertIn("safe.directory=/repo", command)
        self.assertIn("config", command)

    def test_prefix_walk_does_not_fall_off_the_end_into_a_denied_subcommand(self):
        # The loop consumes global options and safe.directory pairs; whatever it
        # stops on must still face the allowlist. A network subcommand behind a
        # legal prefix must be refused exactly as it is bare.
        for prefix in (
            ["--literal-pathspecs"],
            ["-c", "safe.directory=/repo"],
            ["--literal-pathspecs", "-c", "safe.directory=/repo"],
            ["-c", "safe.directory=/repo", "--literal-pathspecs"],
        ):
            for denied in ("fetch", "push", "ls-remote", "clone"):
                with self.subTest(prefix=prefix, denied=denied):
                    with self.assertRaises(ValueError) as caught:
                        git_helpers._offline_git_command(
                            self.REPO, [*prefix, denied, "origin"]
                        )
                    self.assertIn("offline allowlist", str(caught.exception))

    def test_a_trailing_dash_c_after_the_subcommand_is_not_inspected_as_a_prefix(self):
        # Once the walk stops on the subcommand it no longer inspects tokens, so
        # this must be refused by the allowlist rather than silently accepted.
        with self.assertRaises(ValueError):
            git_helpers._offline_git_command(
                self.REPO, ["fetch", "-c", "safe.directory=/repo"]
            )


class PathGlobInvalidPatternAgreementTests(unittest.TestCase):
    """OBL-GIT-SOURCE-011.

    An invalid pattern must fail closed through every matching entry point,
    rather than becoming a silent non-match in one path and an error in
    another.
    """

    INVALID_PATTERNS = ("a//b", "/b", "", "a/./b", "a/../b")

    def test_match_path_glob_refuses_every_invalid_pattern(self):
        for pattern in self.INVALID_PATTERNS:
            with self.subTest(pattern=pattern):
                with self.assertRaises(GuardrailError):
                    utils._match_path_glob("a/b", pattern)

    def test_path_glob_operation_refuses_every_invalid_pattern(self):
        for pattern in self.INVALID_PATTERNS:
            with self.subTest(pattern=pattern):
                operation = utils._PathGlobOperation("obligation register")
                with self.assertRaises(GuardrailError):
                    operation.matches("a/b", pattern)


class WholeSegmentPrefixSelectionTests(unittest.TestCase):
    """OBL-GIT-SOURCE-009 and OBL-CROSSCUTTING-002.

    `_component_files` selects with two `bisect_left` calls over a sorted path
    index, and must return exactly the paths strictly below
    `component_path + "/"`, with that prefix stripped.

    The register found every multi-component fixture in the suite uses names
    with no shared prefix, so nothing today distinguishes whole-segment
    ownership from plain string-prefix matching. These names are chosen to sort
    around the separator: "-" (0x2D) sorts before "/" (0x2F), and "b" and "v"
    sort after it, so a bad upper bound shows up as a wrong slice rather than an
    empty one.
    """

    TRACKED = (
        "api",
        "api-old/z.py",
        "api/x.py",
        # Sorts immediately after the "api/" range and before "apib", so it is
        # the only name that distinguishes an upper bound of chr(ord("/") + 1)
        # from any looser one. Without it, widening the bound is undetectable.
        "api0/w.py",
        "apib",
        "apiv2/y.py",
        "src/api/q.py",
    )

    def _select(self, component_path):
        return migration_analysis._component_files(
            Path("repo"),
            component_path,
            "head",
            None,
            path_index=tuple(sorted(self.TRACKED)),
        )

    def test_only_whole_segment_descendants_are_selected(self):
        self.assertEqual(self._select("api"), ["x.py"])

    def test_a_sibling_whose_name_extends_the_component_path_is_excluded(self):
        selected = self._select("api")
        for outsider in ("y.py", "z.py", "w.py", "apib", "apiv2/y.py", "api-old/z.py"):
            self.assertNotIn(outsider, selected)

    def test_a_file_equal_to_the_component_path_is_not_its_own_descendant(self):
        # "api" is a tracked regular file here as well as a component root; the
        # range is strictly below "api/", so the file itself is not a member.
        self.assertNotIn("", self._select("api"))

    def test_a_nested_directory_of_the_same_name_belongs_to_its_own_parent(self):
        self.assertEqual(self._select("src/api"), ["q.py"])
        self.assertNotIn("q.py", self._select("api"))

    def test_a_trailing_separator_on_the_component_path_selects_the_same_set(self):
        self.assertEqual(self._select("api/"), self._select("api"))

    def test_the_whole_index_is_returned_for_a_pathless_component(self):
        for pathless in ("", "."):
            with self.subTest(component_path=pathless):
                self.assertEqual(
                    self._select(pathless), sorted(self.TRACKED)
                )


if __name__ == "__main__":
    unittest.main()
