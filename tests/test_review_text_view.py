"""What the review text view tells a reader, and what it leaves ambiguous.

Two questions about the same report. Whether an explicit facet override is
visible in it, and whether a consumer label that contains the separator can be
told apart from two labels.

Both divergences are marked rather than fixed: see the expectedFailure tests.

Covers OBL-GRAPH-003 and OBL-GRAPH-006.
"""

from __future__ import annotations

import unittest

from tests._parity import run_cli
from tests._scenarios import Scenario

#: A label boundver explicitly permits, containing the character used to join
#: the rendered list.
COMMA_LABEL = "team, with comma"


class _Reviewable:
    """A committed range with a reconciled lock at both ends."""

    def __init__(self, external=()) -> None:
        scene = Scenario()
        scene.component(
            "svc", path="svc", boundary=["api"], behavior=["api"],
            consumers=["sdk"], external_consumers=list(external) or None,
        )
        scene.component("sdk", path="sdk", provider="leaf")
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("sdk/index.ts", "export const x = 1;\n")
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("lock")
        self.base = scene.head()

        scene.append_line("svc/api/v1.yaml", "change: true")
        scene.commit("edit")
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("relock")
        self.target = scene.head()
        self.scene = scene

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def review(self, *arguments) -> str:
        result = run_cli(
            self.scene.root, "review", "--base", self.base,
            "--target", self.target, "--format", "text", *arguments,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout


class FacetOverrideDisclosureTests(unittest.TestCase):
    """OBL-GRAPH-003: an explicit --facets must be visible in the report."""

    def test_the_override_changes_the_report(self):
        """The first half holds: the two runs are distinguishable."""
        with _Reviewable() as repo:
            self.assertNotEqual(repo.review(), repo.review("--facets", "boundary"))

    def test_the_override_shows_in_the_per_facet_annotations(self):
        """Where it does show: a facet outside the override reads observed."""
        with _Reviewable() as repo:
            default = repo.review()
            overridden = repo.review("--facets", "boundary")
            self.assertIn("exact: ", default)
            self.assertIn("[selected]", default)
            for line in overridden.splitlines():
                stripped = line.strip()
                if stripped.startswith("exact: ") or stripped.startswith("behavior: "):
                    self.assertIn("[observed]", stripped)

    def test_the_header_states_the_override(self):
        """Known divergence: the header always names all four facets.

        A reader skimming the header of a report produced under an override
        sees the same sentence as one produced without it, and has to notice a
        per-facet annotation further down to learn the scope was narrowed.
        """
        with _Reviewable() as repo:
            overridden = repo.review("--facets", "boundary")
            header = next(
                line for line in overridden.splitlines() if "Compared facets" in line
            )
            self.assertNotIn("exact", header)
            self.assertIn("boundary", header)

    def test_the_header_matches_the_effective_override(self):
        with _Reviewable() as repo:
            for arguments in ((), ("--facets", "boundary")):
                with self.subTest(arguments=arguments):
                    header = next(
                        line for line in repo.review(*arguments).splitlines()
                        if "Compared facets" in line
                    )
                    expected = (
                        "Compared facets: boundary"
                        if arguments
                        else "Compared facets: exact, behavior, boundary, compat"
                    )
                    self.assertEqual(header.strip(), expected)


class ConsumerLabelAmbiguityTests(unittest.TestCase):
    """OBL-GRAPH-006: a rendered list must survive a label with a separator."""

    def _external_line(self, text: str) -> str:
        return next(
            line.strip() for line in text.splitlines()
            if line.strip().startswith("External consumers:")
        )

    def test_ordinary_labels_render_as_a_readable_list(self):
        with _Reviewable(external=["alpha", "beta"]) as repo:
            line = self._external_line(repo.review())
            self.assertIn("alpha", line)
            self.assertIn("beta", line)

    def test_a_label_containing_a_comma_is_accepted(self):
        """The premise: boundver permits it, so the renderer must cope."""
        with _Reviewable(external=[COMMA_LABEL, "plain"]) as repo:
            self.assertIn(COMMA_LABEL, repo.review())

    def test_the_rendered_list_stays_unambiguous(self):
        """Known divergence: the label is joined with the same ', ' separator.

        `External consumers: plain (both), team, with comma (both)` cannot be
        split back into the two labels it came from. A reader, or anything
        parsing the text view, sees three.
        """
        with _Reviewable(external=[COMMA_LABEL, "plain"]) as repo:
            line = self._external_line(repo.review())
            body = line[len("External consumers:"):].strip()
            self.assertEqual(len(body.split(", ")), 2, body)

    def test_the_list_escapes_commas_but_edges_retain_the_label(self):
        with _Reviewable(external=[COMMA_LABEL, "plain"]) as repo:
            text = repo.review()
            body = self._external_line(text)[len("External consumers:"):].strip()
            self.assertEqual(len(body.split(", ")), 2)
            self.assertIn("\\x2c", body)

            edges = [
                line.strip() for line in text.splitlines()
                if line.strip().startswith("svc -> ")
            ]
            self.assertTrue(
                any(COMMA_LABEL in edge and edge.endswith("]") for edge in edges),
                edges,
            )


if __name__ == "__main__":
    unittest.main()
