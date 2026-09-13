"""Filter values that strip to nothing.

`--facets` and `--components` narrow what a run checks. A value that reduces
to no tokens is a value the user did not mean, and the dangerous reading is the
silent one: treating it as "everything" turns a narrowing flag into a no-op,
so a script whose computed list came out empty checks something other than
what it asked for and never learns.

The two commands now reject every explicit component filter that contains no
names. Facet filtering retains its separate policy: omission, blank values and
separator-only spellings all use the configured gates.

Covers OBL-FACETS-002 and OBL-FACETS-003.
"""

from __future__ import annotations

import unittest

from tests._parity import run_cli
from tests._scenarios import Scenario

#: Values whose tokens all strip to empty.
SEPARATOR_ONLY = (",", " , ", ",,", ", ,")
BLANK = ("", " ", "   ")

USAGE_ERROR = 2


class _Repository:
    """A drifted repository for verify, and a reconciled range for review.

    review refuses an unreconciled config or lock state before it looks at any
    flag, so a stale lock would make every review call exit 2 for a reason
    that has nothing to do with the value under test.
    """

    def __init__(self) -> None:
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api"])
        scene.component("other", path="other", provider="leaf")
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("other/x.py", "x = 1\n")
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("lock")
        self.base = scene.head()

        scene.append_line("svc/api/v1.yaml", "reviewed change\n")
        scene.commit("edit")
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("relock")
        self.target = scene.head()

        # Now drift again and leave it, so verify has something to report.
        scene.append_line("svc/api/v1.yaml", "drift: true")
        scene.commit("drift")
        self.scene = scene

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def verify(self, *arguments):
        return run_cli(self.scene.root, "verify", "--source", "head", *arguments)

    def review(self, *arguments):
        return run_cli(
            self.scene.root, "review",
            "--base", self.base, "--target", self.target, *arguments,
        )


class ReviewPreconditionTests(unittest.TestCase):
    """Without these, every review assertion below could pass vacuously."""

    def test_review_succeeds_on_the_range_with_no_facet_flag(self):
        with _Repository() as repo:
            self.assertEqual(repo.review().returncode, 0, repo.review().stderr)

    def test_review_accepts_a_real_facet(self):
        with _Repository() as repo:
            self.assertEqual(repo.review("--facets", "exact").returncode, 0)


class FacetFilterTests(unittest.TestCase):
    """OBL-FACETS-002: verify and review must treat the value alike."""

    def test_a_separator_only_value_uses_configured_policy_in_both(self):
        with _Repository() as repo:
            for value in SEPARATOR_ONLY:
                with self.subTest(value=value):
                    verified = repo.verify("--facets", value)
                    reviewed = repo.review("--facets", value)
                    self.assertNotEqual(verified.returncode, USAGE_ERROR)
                    self.assertEqual(reviewed.returncode, 0, reviewed.stderr)

    def test_the_two_commands_agree_on_every_value(self):
        """This is the obligation, and it holds."""
        with _Repository() as repo:
            for value in SEPARATOR_ONLY + BLANK + ("exact",):
                with self.subTest(value=value):
                    self.assertEqual(
                        repo.verify("--facets", value).returncode == USAGE_ERROR,
                        repo.review("--facets", value).returncode == USAGE_ERROR,
                    )

    def test_a_blank_value_is_accepted_by_both(self):
        """Consistent, and still a silent widening: see the components case."""
        with _Repository() as repo:
            for value in BLANK:
                with self.subTest(value=value):
                    self.assertNotEqual(
                        repo.verify("--facets", value).returncode, USAGE_ERROR
                    )
                    self.assertEqual(repo.review("--facets", value).returncode, 0)


class ComponentFilterTests(unittest.TestCase):
    """OBL-FACETS-003: an empty selection must not mean every component."""

    @staticmethod
    def _reported(result) -> set:
        text = (result.stdout or "") + (result.stderr or "")
        return {
            line.strip().split()[1].split(".")[0]
            for line in text.splitlines()
            if line.strip().startswith("MISMATCH ")
        }

    def test_a_named_component_narrows_the_run(self):
        """The premise: the flag does something when given a real name."""
        with _Repository() as repo:
            self.assertEqual(self._reported(repo.verify("--components", "svc")), {"svc"})
            self.assertEqual(repo.verify("--components", "other").returncode, 0)

    def test_an_unknown_component_is_a_usage_error(self):
        """The flag does validate its entries, which is what makes the silence
        below a gap rather than a policy."""
        with _Repository() as repo:
            result = repo.verify("--components", "nope")
            self.assertEqual(result.returncode, USAGE_ERROR)
            self.assertIn("unknown --components entries", result.stderr)

    def test_the_two_flags_keep_distinct_empty_value_policies(self):
        with _Repository() as repo:
            self.assertNotEqual(repo.verify("--facets", ",").returncode, USAGE_ERROR)
            self.assertEqual(repo.verify("--components", ",").returncode, USAGE_ERROR)

    def test_an_empty_selection_is_a_usage_error(self):
        """A computed empty selection cannot widen into a full verification."""
        with _Repository() as repo:
            for value in SEPARATOR_ONLY + BLANK:
                with self.subTest(value=value):
                    result = repo.verify("--components", value)
                    self.assertEqual(result.returncode, USAGE_ERROR, repr(value))
                    self.assertIn("must name at least one", result.stderr)

    def test_an_omitted_filter_still_verifies_everything(self):
        with _Repository() as repo:
            unfiltered = repo.verify()
            self.assertEqual(self._reported(unfiltered), {"svc"})
            for value in SEPARATOR_ONLY + BLANK:
                with self.subTest(value=value):
                    filtered = repo.verify("--components", value)
                    self.assertEqual(filtered.returncode, USAGE_ERROR)
                    self.assertEqual(self._reported(filtered), set())

    def test_generate_rejects_an_empty_selection_without_rewriting_the_lock(self):
        with _Repository() as repo:
            lock_path = repo.scene.root / "boundary.lock.json"
            before = lock_path.read_bytes()
            result = run_cli(
                repo.scene.root,
                "generate",
                "--source",
                "head",
                "--components",
                ",",
            )
            self.assertEqual(result.returncode, USAGE_ERROR)
            self.assertIn("must name at least one", result.stderr)
            self.assertEqual(lock_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
