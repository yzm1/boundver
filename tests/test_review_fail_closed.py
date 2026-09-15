"""What a review prints when it refuses to print a review.

A partially rendered review is worse than no review: it looks like a complete
one over a smaller range, and neither a reader nor a log scraper can tell the
difference. So the contract is all-or-nothing — zero bytes on stdout and exit
2 — and the thing that makes it true today is an ordering, that the whole line
list is built before anything is printed. An ordering is exactly what a later
refactor to streaming emission would change without anyone noticing.

A second ceiling, on the printed plan rather than on the reviewed range, is
pinned further down. Its exact rendering is checked before either stdout or a
requested summary file is written.

Covers OBL-LOCKFILE-001 and OBL-OUTPUT-031.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import boundver._review as review
import boundver._review_plan as review_plan
import boundver.core as core

from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2

#: Every guardrail on this path ends with the same promise.
NO_PARTIAL = "No partial review result was emitted"


class _Reviewable:
    """A range with a real change in it, so a review has something to say."""

    def __init__(self) -> None:
        scene = Scenario()
        scene.component("svc", path="svc", boundary=["api"], consumers=["sdk"])
        scene.component("sdk", path="sdk", provider="leaf")
        scene.slice("all", mode="exact", components=["sdk", "svc"])
        scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
        scene.file("sdk/index.ts", "export const x = 1;\n")
        scene.commit()
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("lock")
        self.base = scene.head()
        scene.append_line("svc/api/v1.yaml", "change\n")
        scene.commit("edit")
        run_cli(scene.root, "generate", "--source", "head")
        scene.commit("relock")
        self.target = scene.head()
        self.scene = scene

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def review(self, *extra):
        return run_cli_in_process(
            self.scene.root, "review",
            "--base", self.base, "--target", self.target, *extra,
        )

    def result(self) -> dict:
        return review.analyze_review_range(self.scene.root, self.base, self.target)

    def guard_at(self, limit: int) -> str:
        """Which of the three ceilings refuses this range at a given limit."""
        with patch.object(review, "MAX_REVIEW_RESULT_BYTES", limit):
            try:
                built = self.result()
            except review.GuardrailError as exc:
                return "construction" if "construction limit" in str(exc) else "json"
            try:
                review.review_text_lines(built)
            except review.GuardrailError:
                return "text"
        return "none"


def _text_bytes(lines) -> int:
    """The measure review_text_lines applies to itself."""
    return sum(len(line.encode("utf-8", errors="backslashreplace")) + 1 for line in lines)


class RendererFailClosedTests(unittest.TestCase):
    """The renderer returns a whole list or nothing at all."""

    def test_the_renderer_raises_rather_than_returning_a_prefix(self):
        with _Reviewable() as repo:
            result = repo.result()
            size = _text_bytes(review.review_text_lines(result))
            with patch.object(review, "MAX_REVIEW_RESULT_BYTES", size - 1):
                with self.assertRaises(review.GuardrailError) as raised:
                    review.review_text_lines(result)
            self.assertIn(NO_PARTIAL, str(raised.exception))

    def test_the_same_result_renders_at_the_exact_size(self):
        """The premise: the refusal is the ceiling, not a broken result."""
        with _Reviewable() as repo:
            result = repo.result()
            lines = review.review_text_lines(result)
            with patch.object(review, "MAX_REVIEW_RESULT_BYTES", _text_bytes(lines)):
                self.assertEqual(review.review_text_lines(result), lines)


class StdoutIsUntouchedTests(unittest.TestCase):
    """Not "no useful output" — no bytes."""

    FORMATS = ("text", "json", "plan")

    def test_a_review_that_fits_prints_something(self):
        """The premise, without which empty stdout would prove nothing."""
        with _Reviewable() as repo:
            for form in self.FORMATS:
                with self.subTest(format=form):
                    printed = repo.review("--format", form)
                    self.assertEqual(printed.returncode, 0, printed.stderr)
                    self.assertNotEqual(printed.stdout, "")

    def test_every_view_emits_zero_bytes_when_the_ceiling_is_exceeded(self):
        with _Reviewable() as repo:
            for form in self.FORMATS:
                with self.subTest(format=form):
                    with patch.object(review, "MAX_REVIEW_RESULT_BYTES", 1):
                        refused = repo.review("--format", form)
                    self.assertEqual(refused.returncode, COULD_NOT_CHECK, refused.stdout)
                    self.assertEqual(refused.stdout, "")
                    self.assertIn(NO_PARTIAL, refused.stderr)

    def test_the_work_budget_also_emits_zero_bytes(self):
        """The other guardrail on the same path, which fires earlier still."""
        with _Reviewable() as repo:
            with patch.object(review, "MAX_REVIEW_WORK_STEPS", 1):
                refused = repo.review("--format", "text")
            self.assertEqual(refused.returncode, COULD_NOT_CHECK, refused.stdout)
            self.assertEqual(refused.stdout, "")
            self.assertIn(NO_PARTIAL, refused.stderr)

    def test_no_summary_file_is_left_behind_by_a_refused_plan(self):
        """A plan writes a file before printing; the refusal precedes both."""
        with _Reviewable() as repo:
            with patch.object(review, "MAX_REVIEW_RESULT_BYTES", 1):
                refused = repo.review(
                    "--format", "plan", "--summary-file", "summary.md"
                )
            self.assertEqual(refused.returncode, COULD_NOT_CHECK, refused.stdout)
            self.assertEqual(refused.stdout, "")
            self.assertFalse((repo.scene.root / "summary.md").exists())


class PlanEmissionCeilingTests(unittest.TestCase):
    """The ceiling on the printed plan, which is not the one on the built plan.

    Two checks read MAX_PLAN_RESULT_BYTES. build_review_plan measures its own
    compact, sorted serialisation before it returns the plan, and _cmd_review
    measures the indented rendering again as it prints it. They are not
    redundant, because indentation makes the printed form the larger of the
    two, so a plan can clear the first check and still have to be refused by
    the second.

    Nothing had ever entered the band between them. Deleting the byte cap from
    the emission call (MUT-OUTPUT-444) left the whole suite green, because
    every case that reaches for a ceiling lowers the review one instead, and
    that refusal happens while the range is still being analysed, long before
    a plan exists to print. These tests enter the band by lowering only the
    constant that boundver.core reads, leaving _review_plan's own copy where
    it is.

    OBL-OUTPUT-031 asks for two things in this band, and the command delivers
    one of them. Stdout really is untouched and the exit code really is 2. The
    summary file, though, is written before the plan is printed and stays on
    disk after the printing is refused, so a run that published nothing still
    leaves a durable summary behind. That half is marked below rather than
    forced.
    """

    #: Low enough that the indented plan cannot fit under any circumstances.
    EMISSION_LIMIT = 1

    #: What the emission cap says when it refuses.
    OVER_LIMIT = "JSON output exceeds"

    def _refuse(self, repo, name: str):
        """Run a plan review with only the emission cap lowered."""
        with patch.object(core, "MAX_PLAN_RESULT_BYTES", self.EMISSION_LIMIT):
            return repo.review("--format", "plan", "--summary-file", name)

    def test_the_plan_still_builds_while_the_emission_cap_is_lowered(self):
        """The premise, without which the refusal would prove nothing.

        Patching boundver.core must not reach the constant that
        build_review_plan checks itself against. If it did, the plan would be
        refused during construction and the emission cap would never run, so
        every assertion below would pass for the wrong reason.
        """
        untouched = review_plan.MAX_PLAN_RESULT_BYTES
        with _Reviewable() as repo:
            result = repo.result()
            with patch.object(core, "MAX_PLAN_RESULT_BYTES", self.EMISSION_LIMIT):
                self.assertEqual(review_plan.MAX_PLAN_RESULT_BYTES, untouched)
                built = review_plan.build_review_plan(result)
            self.assertIs(built["complete"], True)

    def test_the_emission_cap_refuses_the_plan_with_zero_bytes_on_stdout(self):
        """MUT-OUTPUT-444: the cap on the printed plan is load-bearing.

        Removing max_bytes from the _print_json call that emits the plan used
        to change nothing that any test could see, because no test had ever
        got as far as printing a plan under a lowered ceiling. With the cap
        gone the oversized plan is printed in full and the command reports
        success, so this asserts the refusal itself: exit 2, nothing at all on
        stdout, and a message naming the limit that was exceeded.
        """
        with _Reviewable() as repo:
            refused = self._refuse(repo, "refused.md")
            self.assertEqual(refused.returncode, COULD_NOT_CHECK, refused.stdout)
            self.assertEqual(refused.stdout, "")
            self.assertIn(self.OVER_LIMIT, refused.stderr)

    def test_a_plan_under_the_cap_is_printed_and_its_summary_written(self):
        """The contrast: the cap refuses oversized plans, not every plan.

        A guard that rejected the ordinary case would satisfy the test above
        just as well, so the same command with the ceiling left alone has to
        succeed, print a complete plan, and write the summary it was asked
        for.
        """
        with _Reviewable() as repo:
            printed = repo.review("--format", "plan", "--summary-file", "ok.md")
            self.assertEqual(printed.returncode, 0, printed.stderr)
            self.assertIs(json.loads(printed.stdout)["complete"], True)
            self.assertTrue((repo.scene.root / "ok.md").exists())

    def test_a_refused_plan_leaves_no_summary_file_behind(self):
        """OBL-OUTPUT-031: refusal precedes every durable output."""
        with _Reviewable() as repo:
            self._refuse(repo, "orphan.md")
            self.assertFalse((repo.scene.root / "orphan.md").exists())

    def test_the_refusal_does_not_leave_an_orphaned_summary(self):
        with _Reviewable() as repo:
            refused = self._refuse(repo, "orphan.md")
            self.assertEqual(refused.returncode, COULD_NOT_CHECK, refused.stdout)
            orphan = repo.scene.root / "orphan.md"
            self.assertFalse(orphan.exists())



class GuardrailOrderingTests(unittest.TestCase):
    """Which guard actually fires, and why the text one never does.

    Three ceilings read the one constant: a running total charged per retained
    row during traversal, a check on the serialised result, and the text
    renderer's own. They fire in that order as the limit rises, and the text
    rendering of a result is smaller than its JSON form, so by the time a limit
    is high enough to clear the JSON check it is already above the text size.
    The text ceiling is unreachable from the CLI.

    That is a redundancy rather than a hole — the contract holds, enforced by
    an earlier guard — but it is worth pinning, because a change that made the
    text rendering the larger of the two would move which guard runs, and the
    text guard is the one nothing else has ever exercised.
    """

    SHAPES = (
        ("empty", 0, False),
        ("one component", 1, False),
        ("a fan-out", 6, True),
    )

    def _sizes(self, consumers: int, changed: bool):
        scene = Scenario()
        try:
            names = [f"sdk{index}" for index in range(consumers)]
            scene.component("svc", path="svc", boundary=["api"], consumers=names or None)
            for name in names:
                scene.component(name, path=name, provider="leaf")
                scene.file(f"{name}/index.ts", "export const x = 1;\n")
            scene.file("svc/api/v1.yaml", "openapi: 3.1.0\n")
            scene.commit()
            run_cli(scene.root, "generate", "--source", "head")
            scene.commit("lock")
            base = scene.head()
            if changed:
                scene.append_line("svc/api/v1.yaml", "change\n")
                scene.commit("edit")
                run_cli(scene.root, "generate", "--source", "head")
                scene.commit("relock")
            result = review.analyze_review_range(scene.root, base, scene.head())
            rendered = json.dumps(result, sort_keys=True, ensure_ascii=False)
            return _text_bytes(review.review_text_lines(result)), len(
                rendered.encode("utf-8")
            )
        finally:
            scene.close()

    def test_the_json_form_is_the_larger_of_the_two(self):
        for label, consumers, changed in self.SHAPES:
            with self.subTest(shape=label):
                text, serialised = self._sizes(consumers, changed)
                self.assertGreater(serialised, text)

    def test_both_earlier_guards_are_reachable_and_the_text_one_is_not(self):
        """Sweep the limit from below every ceiling to above all of them."""
        with _Reviewable() as repo:
            text = _text_bytes(review.review_text_lines(repo.result()))
            fired = {limit: repo.guard_at(limit) for limit in (
                1, text // 2, text, text + 1, text * 2, text * 8,
            )}
            self.assertIn("construction", fired.values(), fired)
            self.assertIn("json", fired.values(), fired)
            self.assertIn("none", fired.values(), fired)
            self.assertNotIn("text", fired.values(), fired)

    def test_the_guards_fire_in_order_as_the_limit_rises(self):
        """No interleaving: once a ceiling is cleared it stays cleared."""
        with _Reviewable() as repo:
            order = ["construction", "json", "text", "none"]
            seen = [repo.guard_at(limit) for limit in (
                1, 400, 800, 1200, 2000, 4000, 8000, 32000,
            )]
            self.assertEqual(seen, sorted(seen, key=order.index), seen)


if __name__ == "__main__":
    unittest.main()
