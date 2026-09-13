"""What the CI summary counts, and what it lets through.

A bounded summary makes two promises. The first is arithmetic: the sentence at
the foot says how many routing rows were shown out of how many exist, and both
numbers have to be the ones a reader can count in the page above. The second is
containment: every name in that page comes from the repository being reviewed,
so a name must stay inside the element that displays it and must not become
structure of its own.

The counting half is a small closed space - rows against the row limit, rows
against the byte limit, and fields against the field limit - so the tests walk
it rather than sampling it, including the cells exactly at each limit.

Covers OBL-OUTPUT-011 and OBL-OUTPUT-014.
"""

from __future__ import annotations

import json
import unittest

from boundver import _output
from boundver._review_plan import (
    MAX_PLAN_SUMMARY_BYTES,
    MAX_PLAN_SUMMARY_FIELD_BYTES,
    MAX_PLAN_SUMMARY_ROWS,
    PLAN_SCHEMA,
    render_review_plan_markdown,
)

from tests._parity import run_cli
from tests._scenarios import Scenario

#: The reserve the renderer keeps for the footer when it stops on bytes.
MARKER_RESERVE = 512

COMPLETE = "Presentation complete: all {} routing rows shown."
TRUNCATED = "Presentation truncated: {} of {} routing rows shown"
BOUNDED_CLAUSE = "; one or more displayed fields were bounded"

#: The six sections, in the order the renderer walks them, and how each one is
#: reached from the plan root.
SECTIONS = {
    "changed_components": lambda rows: {"changed_components": rows},
    "impacted_components": lambda rows: {"selection": {"impacted_components": rows}},
    "external_consumers": lambda rows: {"selection": {"external_consumers": rows}},
    "changed_slices": lambda rows: {"selection": {"changed_slices": rows}},
    "impacted_slices": lambda rows: {"selection": {"impacted_slices": rows}},
    "source_locations": lambda rows: {"source_locations": rows},
}


def _plan(**over) -> dict:
    """A complete plan carrying only what the renderer reads."""
    selection = {
        "impacted_components": [],
        "external_consumers": [],
        "changed_slices": [],
        "impacted_slices": [],
    }
    selection.update(over.pop("selection", {}))
    document = {
        "schema": PLAN_SCHEMA,
        "complete": True,
        "endpoints": {"base": {"commit": "a" * 40}, "target": {"commit": "b" * 40}},
        "request": {"merge_base": False},
        "policy": {"impact": "none"},
        "structural_changes": {"complete": True},
        "changed_components": [],
        "selection": selection,
        "source_locations": [],
    }
    document.update(over)
    return document


def _row_values(section: str, count: int, *, width: int = 4) -> list:
    """Rows of the shape the named section holds."""
    names = [f"n{index:0{width}d}" for index in range(count)]
    if section == "changed_components":
        return [{"name": name, "facets": [{"facet": "exact"}]} for name in names]
    if section == "source_locations":
        return [{"path": f"svc/{name}.yaml", "component": "svc"} for name in names]
    return names


def _rendered(section: str, rows, **kwargs) -> str:
    return render_review_plan_markdown(_plan(**SECTIONS[section](rows)), **kwargs)


def _sentence(text: str) -> str:
    lines = [line for line in text.splitlines() if line.startswith("Presentation")]
    assert len(lines) == 1, lines
    return lines[0]


def _shown(text: str) -> int:
    """Count the rows a reader can actually see, not the header bullets."""
    counted = 0
    inside = False
    for line in text.splitlines():
        if line.startswith("### "):
            inside = True
        elif line.startswith("---"):
            inside = False
        elif inside and line.startswith("- "):
            counted += 1
    return counted


class PlanShapeTests(unittest.TestCase):
    """The premise: the fixture above is the shape a real plan has."""

    def test_a_real_plan_carries_every_section_the_fixture_names(self):
        with Scenario() as scene:
            scene.component("svc", path="svc", provider="leaf")
            scene.file("svc/main.py", "x\n")
            scene.commit()
            self.assertEqual(
                run_cli(scene.root, "generate", "--source", "head").returncode, 0
            )
            scene.commit("lock")
            base = scene.head()
            scene.append_line("svc/main.py", "y\n")
            scene.commit("edit")
            self.assertEqual(
                run_cli(scene.root, "generate", "--source", "head").returncode, 0
            )
            scene.commit("relock")
            result = run_cli(
                scene.root, "review", "--base", base, "--target", scene.head(),
                "--format", "plan",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(result.stdout)

        self.assertEqual(plan["schema"], PLAN_SCHEMA)
        self.assertIs(plan["complete"], True)
        for section in ("changed_components", "source_locations"):
            self.assertIsInstance(plan[section], list, section)
        for section in (
            "impacted_components",
            "external_consumers",
            "changed_slices",
            "impacted_slices",
        ):
            self.assertIsInstance(plan["selection"][section], list, section)
        self.assertEqual(
            _sentence(render_review_plan_markdown(plan)).split(":")[0],
            "Presentation complete",
        )


class RowArithmeticTests(unittest.TestCase):
    """OBL-OUTPUT-011: X is what was rendered, Y is what exists."""

    def test_each_section_contributes_to_the_total(self):
        """Y is a sum over all six sections, not one of them."""
        over = {"selection": {}}
        for section, place in SECTIONS.items():
            built = place(_row_values(section, 1))
            if "selection" in built:
                over["selection"].update(built["selection"])
            else:
                over.update(built)
        text = render_review_plan_markdown(_plan(**over))
        self.assertIn(COMPLETE.format(6), _sentence(text))
        self.assertEqual(_shown(text), 6)

    def test_the_shown_count_is_the_number_of_rendered_rows(self):
        for section in SECTIONS:
            for total, limit in ((0, 3), (2, 3), (3, 3), (4, 3), (5, 0)):
                with self.subTest(section=section, rows=total, max_rows=limit):
                    text = _rendered(
                        section, _row_values(section, total), max_rows=limit
                    )
                    shown = min(total, limit)
                    self.assertEqual(_shown(text), shown)
                    sentence = _sentence(text)
                    if shown == total:
                        self.assertIn(COMPLETE.format(total), sentence)
                    else:
                        self.assertIn(TRUNCATED.format(shown, total), sentence)

    def test_a_count_equal_to_the_limit_is_complete(self):
        """The cell at the limit: complete, not truncated."""
        text = _rendered("impacted_components", _row_values("impacted_components", 3), max_rows=3)
        self.assertIn(COMPLETE.format(3), _sentence(text))
        self.assertNotIn("truncated", _sentence(text))

    def test_one_row_over_the_limit_is_truncated_by_exactly_one(self):
        text = _rendered("impacted_components", _row_values("impacted_components", 4), max_rows=3)
        self.assertIn(TRUNCATED.format(3, 4), _sentence(text))

    def test_an_empty_plan_is_complete_at_zero(self):
        text = render_review_plan_markdown(_plan())
        self.assertIn(COMPLETE.format(0), _sentence(text))
        self.assertEqual(_shown(text), 0)

    def test_the_default_limits_are_the_published_ones(self):
        self.assertEqual(MAX_PLAN_SUMMARY_ROWS, 50)
        self.assertEqual(MAX_PLAN_SUMMARY_BYTES, 65536)
        text = _rendered("impacted_components", _row_values("impacted_components", 51))
        self.assertIn(TRUNCATED.format(50, 51), _sentence(text))


class ByteStopTests(unittest.TestCase):
    """OBL-OUTPUT-011: the stop that comes from bytes rather than rows."""

    #: Rows wide enough that the byte limit is reached well before row 50.
    ROWS = _row_values("impacted_components", 40, width=60)

    def _render(self, allowance: int) -> str:
        return _rendered("impacted_components", self.ROWS, max_bytes=allowance)

    def test_the_stop_is_driven_by_bytes_not_by_the_row_limit(self):
        """The premise: fewer rows are shown than the row limit allows."""
        text = self._render(2048)
        self.assertLess(_shown(text), MAX_PLAN_SUMMARY_ROWS)
        self.assertIn(TRUNCATED.format(_shown(text), len(self.ROWS)), _sentence(text))

    def test_the_rendering_stays_inside_its_allowance(self):
        for allowance in (2048, 2560, 3072, 4096):
            with self.subTest(max_bytes=allowance):
                rendered = self._render(allowance)
                self.assertLessEqual(len(rendered.encode("utf-8")), allowance)

    def test_the_reserve_is_what_holds_the_next_row_back(self):
        """Handing back one row plus the reserve admits at least one more."""
        row_size = len(f"- <code>{self.ROWS[0]}</code>\n".encode("utf-8"))
        for allowance in (2048, 2560, 3072):
            with self.subTest(max_bytes=allowance):
                shown = _shown(self._render(allowance))
                self.assertLess(shown, len(self.ROWS))
                widened = _shown(self._render(allowance + row_size + MARKER_RESERVE))
                self.assertGreater(widened, shown)

    def test_the_footer_still_fits_after_the_last_row(self):
        """Which is what the reserve is kept for."""
        for allowance in (2048, 2560, 3072):
            with self.subTest(max_bytes=allowance):
                text = self._render(allowance)
                self.assertIn("Presentation truncated", _sentence(text))
                self.assertLessEqual(len(text.encode("utf-8")), allowance)

    def test_a_larger_allowance_never_shows_fewer_rows(self):
        counts = [_shown(self._render(allowance)) for allowance in
                  (2048, 2560, 3072, 4096, 8192)]
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[-1], len(self.ROWS))


class BoundedFieldTests(unittest.TestCase):
    """OBL-OUTPUT-011: a field bounded on its own still means truncated."""

    LONG = "q" * (MAX_PLAN_SUMMARY_FIELD_BYTES + 88)

    def test_every_row_shown_but_one_field_bounded_still_says_truncated(self):
        text = _rendered("impacted_components", [self.LONG])
        self.assertEqual(_shown(text), 1)
        self.assertIn(TRUNCATED.format(1, 1) + BOUNDED_CLAUSE, _sentence(text))

    def test_the_bounded_field_is_visibly_shortened(self):
        """The premise: the clause reports something a reader can see."""
        text = _rendered("impacted_components", [self.LONG])
        self.assertIn("...</code>", text)
        self.assertNotIn(self.LONG, text)

    def test_a_field_fitting_with_its_markup_is_not_bounded(self):
        text = _rendered(
            "impacted_components",
            ["q" * (MAX_PLAN_SUMMARY_FIELD_BYTES - len("<code></code>"))],
        )
        self.assertIn(COMPLETE.format(1), _sentence(text))

    def test_a_field_one_byte_over_the_limit_is_bounded(self):
        text = _rendered(
            "impacted_components", ["q" * (MAX_PLAN_SUMMARY_FIELD_BYTES + 1)]
        )
        self.assertIn(BOUNDED_CLAUSE, _sentence(text))

    def test_an_over_long_commit_bounds_a_plan_with_no_rows_at_all(self):
        """X equals Y here, so only the field can make it truncated."""
        text = render_review_plan_markdown(
            _plan(endpoints={"base": {"commit": self.LONG}, "target": {"commit": "b"}})
        )
        self.assertEqual(_shown(text), 0)
        self.assertIn(TRUNCATED.format(0, 0) + BOUNDED_CLAUSE, _sentence(text))

    def test_both_endpoints_are_measured(self):
        for endpoint in ("base", "target"):
            with self.subTest(endpoint=endpoint):
                endpoints = {"base": {"commit": "a"}, "target": {"commit": "b"}}
                endpoints[endpoint] = {"commit": self.LONG}
                text = render_review_plan_markdown(_plan(endpoints=endpoints))
                self.assertIn(BOUNDED_CLAUSE, _sentence(text))

    def test_the_complete_branch_appears_only_when_both_conditions_hold(self):
        """The obligation as a biconditional, over the whole small space."""
        for total, limit, long_field in (
            (0, 50, False), (0, 50, True),
            (3, 3, False), (3, 3, True),
            (4, 3, False), (4, 3, True),
        ):
            with self.subTest(rows=total, max_rows=limit, long_field=long_field):
                rows = _row_values("impacted_components", total)
                if long_field and rows:
                    rows[0] = self.LONG
                endpoints = {"base": {"commit": "a"}, "target": {"commit": "b"}}
                if long_field and not rows:
                    endpoints["base"] = {"commit": self.LONG}
                text = render_review_plan_markdown(
                    _plan(
                        selection={"impacted_components": rows}, endpoints=endpoints
                    ),
                    max_rows=limit,
                )
                shown = _shown(text)
                # Every long value in this matrix is displayed: it is either
                # the first row, which is always shown, or a commit in the
                # fixed header.
                complete = shown == total and not long_field
                self.assertEqual(
                    _sentence(text).startswith("Presentation complete"), complete
                )


class DroppedFieldClauseTests(unittest.TestCase):
    """A field bounded on a row the byte stop then threw away."""

    #: Fourteen rows fit in 2048 bytes, so the fifteenth is dropped.
    ROWS = (
        _row_values("impacted_components", 14, width=60)
        + ["q" * 900]
        + _row_values("impacted_components", 5, width=60)
    )

    def _text(self) -> str:
        return _rendered("impacted_components", self.ROWS, max_bytes=2048)

    def test_a_field_that_was_not_displayed_is_not_claimed_as_displayed(self):
        """Known divergence: the clause counts rows the reader never saw."""
        self.assertNotIn(BOUNDED_CLAUSE, _sentence(self._text()))

    def test_a_dropped_bounded_row_does_not_set_the_displayed_field_clause(self):
        text = self._text()
        self.assertNotIn(BOUNDED_CLAUSE, _sentence(text))
        self.assertNotIn("qqq", text)
        self.assertNotIn("...</code>", text)

    def test_the_row_limit_path_does_not_have_the_same_problem(self):
        """It tests the limit before rendering, so nothing is measured."""
        text = _rendered(
            "impacted_components",
            _row_values("impacted_components", 3) + ["q" * 900],
            max_rows=3,
        )
        self.assertIn(TRUNCATED.format(3, 4), _sentence(text))
        self.assertNotIn(BOUNDED_CLAUSE, _sentence(text))


#: Payloads a repository can put in any name the summary displays.
HOSTILE = {
    "closing tag": "</code> loose",
    "an element": "<script>alert(1)</script>",
    "a newline": "one\ntwo",
    "a carriage return": "one\rtwo",
    "an escape sequence": "\x1b[31mred\x1b[0m",
    "a workflow command": "::error::owned",
    "a heading": "# heading",
    "a fence": "```sh",
    "a table row": "| a | b |",
    "a quote": 'say "x" and \'y\'',
    "an ampersand": "a & b",
    "a line separator": "one" + chr(0x2028) + "two",
    "over the field limit": "z" * 900,
}

#: Where each hostile value is planted, and how to build the row around it.
FIELDS = {
    "changed_components name": lambda value: {
        "changed_components": [{"name": value, "facets": [{"facet": "exact"}]}]
    },
    "impacted_components": lambda value: {
        "selection": {"impacted_components": [value]}
    },
    "external_consumers": lambda value: {"selection": {"external_consumers": [value]}},
    "changed_slices": lambda value: {"selection": {"changed_slices": [value]}},
    "impacted_slices": lambda value: {"selection": {"impacted_slices": [value]}},
    "source_locations path": lambda value: {
        "source_locations": [{"path": value, "component": "svc"}]
    },
    "source_locations component": lambda value: {
        "source_locations": [{"path": "svc/a.yaml", "component": value}]
    },
    "base commit": lambda value: {
        "endpoints": {"base": {"commit": value}, "target": {"commit": "b"}}
    },
    "target commit": lambda value: {
        "endpoints": {"base": {"commit": "a"}, "target": {"commit": value}}
    },
}

#: How every line of the summary may begin.
LINE_STARTS = ("- ", "### ", "## ", "**", "---", "Presentation", "")


class HostileNameContainmentTests(unittest.TestCase):
    """OBL-OUTPUT-014: a name is displayed, never obeyed."""

    def _every_rendering(self):
        for field, place in FIELDS.items():
            for label, value in HOSTILE.items():
                yield field, label, render_review_plan_markdown(_plan(**place(value)))

    def test_no_line_begins_with_a_workflow_command(self):
        for field, label, text in self._every_rendering():
            with self.subTest(field=field, payload=label):
                for line in text.splitlines():
                    self.assertFalse(line.startswith("::"), line[:60])

    def test_no_line_begins_with_anything_unexpected(self):
        for field, label, text in self._every_rendering():
            with self.subTest(field=field, payload=label):
                for line in text.splitlines():
                    self.assertTrue(
                        line.startswith(LINE_STARTS), repr(line[:60])
                    )

    def test_the_code_elements_stay_balanced(self):
        for field, label, text in self._every_rendering():
            with self.subTest(field=field, payload=label):
                self.assertEqual(text.count("<code>"), text.count("</code>"))

    def test_no_markup_escapes_the_code_element(self):
        for field, label, text in self._every_rendering():
            with self.subTest(field=field, payload=label):
                stripped = text.replace("<code>", "").replace("</code>", "")
                self.assertNotIn("<", stripped)
                self.assertNotIn(">", stripped)

    def test_no_control_byte_reaches_the_page(self):
        for field, label, text in self._every_rendering():
            with self.subTest(field=field, payload=label):
                for character in text:
                    codepoint = ord(character)
                    if character == "\n":
                        continue
                    self.assertFalse(codepoint < 0x20, hex(codepoint))
                    self.assertFalse(0x7F <= codepoint <= 0x9F, hex(codepoint))
                    self.assertNotIn(codepoint, (0x2028, 0x2029))

    def test_a_multi_line_value_stays_on_one_line(self):
        for field, place in FIELDS.items():
            with self.subTest(field=field):
                before = len(render_review_plan_markdown(
                    _plan(**place("plain"))
                ).splitlines())
                after = len(render_review_plan_markdown(
                    _plan(**place("one\ntwo\nthree"))
                ).splitlines())
                self.assertEqual(before, after)

    def test_the_value_is_still_readable_after_escaping(self):
        """The premise: containment did not simply delete the name."""
        text = render_review_plan_markdown(
            _plan(selection={"impacted_components": ["</code> loose"]})
        )
        self.assertIn("&lt;/code&gt; loose", text)


class BacktickNeutralisationTests(unittest.TestCase):
    """A name that carries Markdown structure of its own."""

    def _row(self, value: str) -> str:
        text = render_review_plan_markdown(
            _plan(selection={"impacted_components": [value]})
        )
        rows = [line for line in text.splitlines() if line.startswith("- <code>")]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_a_backtick_in_a_name_is_neutralised(self):
        """Known divergence: html.escape does not touch Markdown syntax."""
        self.assertNotIn("`", self._row("a`b`c"))

    def test_backticks_are_rendered_as_entities_inside_the_element(self):
        row = self._row("a`b`c")
        self.assertEqual(row.count("`"), 0)
        self.assertIn("<code>a&#96;b&#96;c</code>", row)

    def test_the_other_markdown_starters_are_harmless_where_they_land(self):
        """They are never at the start of a line, which is what matters."""
        for value in ("# h", "> q", "* i", "1. i", "| a |"):
            with self.subTest(value=value):
                self.assertTrue(self._row(value).startswith("- <code>"))


class DisplayDivergenceTests(unittest.TestCase):
    """The two _display_text copies differ, and the difference is intended."""

    def test_the_terminal_helper_neutralises_a_leading_workflow_command(self):
        self.assertTrue(_output._display_text("::error::x").startswith("\\x3a"))

    def test_the_summary_helper_leaves_it_alone(self):
        from boundver import _review_plan

        self.assertEqual(_review_plan._display_text("::error::x"), "::error::x")

    def test_which_is_safe_because_a_row_never_starts_with_the_value(self):
        text = render_review_plan_markdown(
            _plan(selection={"impacted_components": ["::error::owned"]})
        )
        self.assertIn("- <code>::error::owned</code>", text)
        for line in text.splitlines():
            self.assertFalse(line.startswith("::"), line[:40])


if __name__ == "__main__":
    unittest.main()
