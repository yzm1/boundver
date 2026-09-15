"""Where a review plan says a finding lives, and whether that is inside the repo.

`_source_path` turns a component root and a document label into the path a
plan emits as a `::notice file=` annotation. A CI annotation is a pointer, so
the only safe answer for a label it cannot place is no answer at all. The
guard splits both halves with PurePosixPath and checks the parts, which is
right on POSIX and blind on Windows, where a backslash is a separator and the
whole traversal arrives as a single part.

Covers OBL-GLOBS-001.
"""

from __future__ import annotations

import unittest
from pathlib import PurePosixPath

from hypothesis import given, settings
from hypothesis import strategies as st

from boundver._review_plan import _source_path

PREFIX = "canonical:"

#: Enough alphabet to build a traversal in either separator, plus the two
#: characters that make a segment degenerate.
SEGMENT_ALPHABET = "ab./\\: "

PROFILE = settings(max_examples=300, deadline=None)


def _contained(root: str, result: str) -> bool:
    """Does *result* name something under *root*, as a POSIX-relative path?"""
    if result.startswith("/") or PurePosixPath(result).is_absolute():
        return False
    parts = PurePosixPath(result).parts
    root_parts = PurePosixPath(root).parts
    return parts[: len(root_parts)] == root_parts


class SourcePathAcceptanceTests(unittest.TestCase):
    """The premise: ordinary input produces the location a reader expects."""

    def test_a_plain_label_lands_under_the_component_root(self):
        self.assertEqual(
            _source_path("svc", f"{PREFIX}api/v1.yaml"), "svc/api/v1.yaml"
        )

    def test_a_label_without_the_prefix_has_no_location(self):
        self.assertIsNone(_source_path("svc", "api/v1.yaml"))

    def test_a_non_string_has_no_location(self):
        self.assertIsNone(_source_path(None, f"{PREFIX}a.yaml"))
        self.assertIsNone(_source_path("svc", None))


class SourcePathContainmentTests(unittest.TestCase):
    """OBL-GLOBS-001: no returned path may escape the component root."""

    def test_a_posix_traversal_has_no_location(self):
        self.assertIsNone(_source_path("svc", f"{PREFIX}../etc/passwd"))
        self.assertIsNone(_source_path("..", f"{PREFIX}a.yaml"))

    def test_an_absolute_label_or_root_has_no_location(self):
        self.assertIsNone(_source_path("svc", f"{PREFIX}/etc/passwd"))
        self.assertIsNone(_source_path("/abs", f"{PREFIX}a.yaml"))

    def test_an_empty_label_has_no_location(self):
        self.assertIsNone(_source_path("svc", PREFIX))

    def test_a_backslash_traversal_has_no_location(self):
        """Known divergence: it parses as one segment and passes the guard."""
        self.assertIsNone(_source_path("svc", PREFIX + "..\\..\\etc\\passwd"))

    def test_a_windows_rooted_component_path_has_no_location(self):
        """Known divergence: a drive letter and a UNC root both pass."""
        self.assertIsNone(_source_path("C:/x", f"{PREFIX}a.yaml"))

    def test_a_dot_or_empty_segment_has_no_location(self):
        """Known divergence: both are normalised away rather than refused."""
        self.assertIsNone(_source_path("svc", f"{PREFIX}./a.yaml"))
        self.assertIsNone(_source_path("svc", f"{PREFIX}a//b.yaml"))


class SourcePathScopeTests(unittest.TestCase):
    """What each divergence returns, so a partial fix cannot pass unnoticed."""

    def test_the_backslash_traversal_is_refused(self):
        self.assertIsNone(_source_path("svc", PREFIX + "..\\..\\etc\\passwd"))

    def test_the_windows_roots_are_refused(self):
        self.assertIsNone(_source_path("C:/x", f"{PREFIX}a.yaml"))
        self.assertIsNone(_source_path("\\\\server\\share", f"{PREFIX}a.yaml"))

    def test_the_degenerate_segments_are_refused(self):
        self.assertIsNone(_source_path("svc", f"{PREFIX}./a.yaml"))
        self.assertIsNone(_source_path("svc", f"{PREFIX}a//b.yaml"))

    def test_the_mechanism_is_the_posix_parse(self):
        """A backslash traversal is one part, so no part is '..'."""
        self.assertEqual(PurePosixPath("..\\..\\etc\\passwd").parts, ("..\\..\\etc\\passwd",))
        self.assertEqual(PurePosixPath("../etc/passwd").parts, ("..", "etc", "passwd"))


class SourcePathPropertyTests(unittest.TestCase):
    """The invariant over generated input, rather than over chosen input."""

    @PROFILE
    @given(
        root=st.text(alphabet=SEGMENT_ALPHABET, min_size=0, max_size=12),
        label=st.text(alphabet=SEGMENT_ALPHABET, min_size=0, max_size=12),
    )
    def test_a_returned_path_starts_with_the_component_root(self, root, label):
        """The weaker half of the obligation, which does hold.

        The stronger half - that a degenerate label yields no location at all -
        is asserted by name above and fails. This one says that whatever comes
        back is at least prefixed by the root it was given, so the escape is
        always spelled out in the tail rather than hidden in the head.
        """
        result = _source_path(root, PREFIX + label)
        if result is not None:
            self.assertTrue(_contained(root, result), (root, label, result))

    @PROFILE
    @given(label=st.text(alphabet=SEGMENT_ALPHABET, min_size=0, max_size=12))
    def test_a_forward_slash_traversal_is_always_refused(self, label):
        """Every POSIX-spelled escape is caught, whatever surrounds it."""
        result = _source_path("svc", f"{PREFIX}../{label}")
        self.assertIsNone(result, (label, result))


if __name__ == "__main__":
    unittest.main()
