"""What a truncated structural explanation, an escaped pointer, and an absent
boundary digest are each allowed to say.

Three of the six obligations here turn on a quantifier that the existing suite's
fixtures cannot express. The structural-diff budget is one object shared by
every entry, every document and every component of a review, and the branch that
matters most - the one at ``_structural_review.py:326`` that checks
``structural_budget.exhausted`` *before* calling a later component's provider -
needs at least three boundary-changed components to execute at all: one to
consume the budget, one to trip it, and one to arrive after it is already spent.
``_make_range`` in ``test_review.py`` produces exactly one such component, so a
regression that built a fresh ``StructuralDiffBudget`` per component would pass
every test in the repository today. The fixture below therefore builds three
components carrying two OpenAPI documents each, and derives its ceilings from
row counts observed in an unconstrained run rather than from constants written
down by hand: five rows per document, ten per component, thirty per review. A
ceiling of ten leaves the first component whole and starves the rest; a ceiling
of five starves the first component's *second document*, which is the only way
to distinguish accumulation across documents from accumulation across
components.

The pointer obligation is quantified over an unbounded input space, so the
escape half is a Hypothesis property with an oracle that is not the code under
test: RFC 6901 unescaping is ``replace("~1", "/")`` then ``replace("~0", "~")``,
and applying the two escapes in the wrong order - the plausible regression -
produces a segment that fails to round-trip. Grep for ``~0`` across this
repository before this file and the only hit is the implementation itself; no
fixture, doc example or demo has ever used a key containing a tilde. The
value-freedom half is a checker over every row of every document of a table of
diffs, and it is paired with a premise test that plants a value in a payload and
proves the same scan finds it, because "no value leaked" is otherwise a sentence
that passes when nothing was ever produced.

The one absence that needed a second repository is the raw-provider review. A
provider that does not expose ``structural_diff`` is refused at
``_structural_review.py:143`` before the shared budget is ever consulted, so
lowering ``MAX_PROVIDER_DIFF_ROWS`` underneath it changes nothing at all - the
patched and unpatched results are equal dicts. Asserting that ``truncated``
stayed false under that lowered ceiling therefore proves nothing on its own,
because the ceiling was never read. The witness repository built beside it
carries the same three components and the same six documents under
``openapi-canonical``, where the identical patch turns ``truncated`` true on
every report, so the flag's silence under the raw provider is a fact about the
provider rather than about an unreachable guard.

Three results here are findings rather than coverage. The review result's
top-level ``complete`` is the literal ``True`` at ``_review.py:831``; no input
can make it false, so the obligation's "complete stays true" clause is pinned
structurally with ``ast`` instead of behaviourally, and a future computed value
will fail that pin and have to re-establish the claim. ``boundary_status ==
"error"`` never reaches a generated lockfile at all: ``_generation_errors``
rejects it and ``--allow-partial`` explicitly does not relax that, so the
register's suggestion to compare a partial-boundary generate against an
error-boundary generate describes a state the product refuses to write. The
error arm is real code, so it is exercised where it lives, one level down in
``_compute_component_entry``, and the refusal is pinned beside it, component
and reason rather than the generic header every generation failure carries. The
reachable pair the envelope has to separate is ``none:partial`` (implicit)
against ``none:ok`` (leaf), and it does. The third finding is a guard that
cannot fire: ``review_text_lines`` refuses a rendering larger than
``MAX_REVIEW_RESULT_BYTES``, but ``analyze_review_range`` has already measured
the same result's JSON encoding against the same ceiling, and the text form is a
lossy projection of that JSON - 4053 bytes against 11809 on this fixture,
because the text prints twelve-character digest prefixes where the JSON carries
the whole digest. Every ceiling small enough to refuse the text is small enough
to have refused the JSON, or the construction budget, first. The guard is
exercised by calling the renderer directly, and its unreachability from the CLI
is pinned beside it.

The last obligation is a straight divergence. Every one of the five annotation
names in ``_OPENAPI_STRIP_KEYS`` is deleted when it appears as a direct member
of an ``x-*`` specification extension, so ``x-gateway: {"description": "prod"}``
and ``x-gateway: {"description": "staging"}`` both canonicalize to
``x-gateway: {}`` and share a boundary digest, against a provider docstring that
promises extensions remain part of the contract because tooling gives them
routing meaning. That is written as an expected failure with the current
behaviour pinned name by name beside it, so a partial fix cannot pass unnoticed.

Covers OBL-GLOBS-026, OBL-GLOBS-033, OBL-GLOBS-034, OBL-HASHING-025,
OBL-HASHING-045 and OBL-HASHING-032.
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

import boundver._provider_diff as provider_diff_module
import boundver._review as review_module
from boundver._canonical_providers import _OPENAPI_STRIP_KEYS, _strip_openapi
from boundver._hashing import (
    HASH_DOMAIN_BEHAVIOR,
    HASH_DOMAIN_BOUNDARY,
    _hash_framed_entries,
)
from boundver._lockfile import (
    _SourceAccessor,
    _compute_component_entry,
    dump_lockfile,
    generate_lockfile,
)
from boundver._provider_diff import (
    StructuralDiffBudget,
    diff_canonical_json_entries,
    structural_diff_payload,
)
from boundver._review import analyze_review_range
from boundver._utils import ConfigError, GuardrailError
from boundver.providers import (
    PathHashProvider,
    ProviderContext,
    compute_boundary,
    create_registry,
)

from tests._parity import run_cli, run_cli_in_process
from tests._scenarios import Scenario


# ---------------------------------------------------------------------------
# A range with more than one boundary-changed component
# ---------------------------------------------------------------------------

#: Three components, two OpenAPI documents each. Three is the smallest number
#: that reaches all three structural outcomes in one review: complete, the
#: component that trips the budget, and the component that finds it already
#: exhausted. Two documents each is what separates accumulation across the
#: documents of one component from accumulation across components.
RANGE_COMPONENTS = ("alpha", "beta", "gamma")
RANGE_DOCUMENTS = ("one", "two")


def _base_document(tag: str) -> dict:
    return {
        "openapi": "3.1.0",
        "paths": {
            f"/{tag}": {
                "get": {
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {
                        "200": {"description": "ok"},
                        "404": {"description": "missing"},
                    },
                }
            }
        },
    }


def _target_document(tag: str) -> dict:
    """The same contract after five value-free structural transitions."""
    return {
        "openapi": "3.1.0",
        "paths": {
            f"/{tag}": {
                "get": {
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                },
                "post": {"responses": {"201": {}}},
            },
            f"/{tag}-health": {"get": {"responses": {"204": {}}}},
        },
    }


def _commit_endpoint(scene: Scenario, message: str) -> str:
    """Write config and lockfile, commit both, and return the commit id.

    Review reads an immutable committed lockfile at each endpoint, which
    ``Scenario.commit`` alone does not produce.
    """
    scene.write_config()
    scene.git("add", "--all")
    lockfile = generate_lockfile(scene.config, scene.root, source="index")
    (scene.root / "boundary.lock.json").write_text(
        dump_lockfile(lockfile), encoding="utf-8"
    )
    scene.git("add", "--all")
    scene.git("commit", "-m", message)
    return scene.head()


def _build_range(scene: Scenario, provider: str) -> Tuple[str, str]:
    """Commit a base and a target where every component's boundary moves."""
    for name in RANGE_COMPONENTS:
        scene.component(
            name,
            path=name,
            provider=provider,
            boundary=[f"{slot}.json" for slot in RANGE_DOCUMENTS],
        )
        for slot in RANGE_DOCUMENTS:
            scene.json_file(f"{name}/{slot}.json", _base_document(f"{name}-{slot}"))
    base = _commit_endpoint(scene, "base")
    for name in RANGE_COMPONENTS:
        for slot in RANGE_DOCUMENTS:
            scene.json_file(f"{name}/{slot}.json", _target_document(f"{name}-{slot}"))
    target = _commit_endpoint(scene, "target")
    return base, target


def _boundary_changed(result: dict) -> List[str]:
    """Every component the review itself says moved its boundary facet."""
    return [
        component["name"]
        for component in result["components"]["changed"]
        if any(item["facet"] == "boundary" for item in component["facets"])
    ]


def _report_rows(report: dict) -> int:
    return sum(len(document["changes"]) for document in report["documents"])


def _text_bytes(lines: List[str]) -> int:
    """The size the text guard measures, spelled as ``_review.py:1050`` spells it."""
    return sum(
        len(line.encode("utf-8", errors="backslashreplace")) + 1 for line in lines
    )


class _RangeFixture(unittest.TestCase):
    """One immutable range, built once and read by several tests."""

    provider = "openapi-canonical"

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = Scenario()
        cls.base, cls.target = _build_range(cls.scene, cls.provider)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def review(self, **kwargs: Any) -> dict:
        return analyze_review_range(self.scene.root, self.base, self.target, **kwargs)


class StructuralBudgetAccumulationTests(_RangeFixture):
    """OBL-GLOBS-026: one budget for the whole review, not one per component."""

    def _with_row_ceiling(self, ceiling: int) -> dict:
        with mock.patch.object(
            provider_diff_module, "MAX_PROVIDER_DIFF_ROWS", ceiling
        ):
            return self.review()

    def test_an_unconstrained_review_explains_every_boundary_changed_component(self):
        """Premise: the multi-component, multi-document path really executes.

        Every assertion below is about explanations that were *withheld*. If
        the fixture produced one report, or reports with one document, the
        starvation tests would pass against a per-component budget too.
        """
        result = self.review()
        structural = result["structural_changes"]
        explained = [report["component"] for report in structural["reports"]]

        self.assertEqual(explained, _boundary_changed(result))
        self.assertEqual(len(explained), len(RANGE_COMPONENTS))
        self.assertEqual(structural["complete"], True)
        self.assertEqual(structural["truncated"], False)
        for report in structural["reports"]:
            with self.subTest(component=report["component"]):
                self.assertEqual(report["status"], "complete")
                self.assertIsNone(report["reason"])
                self.assertEqual(
                    [document["label"] for document in report["documents"]],
                    [f"canonical:{slot}.json" for slot in RANGE_DOCUMENTS],
                )
                for document in report["documents"]:
                    self.assertTrue(document["changes"])

    def test_the_structural_row_budget_carries_across_documents_of_one_component(self):
        """A ceiling that fits document one alone must still starve document two.

        The detail string is the discriminator. A per-document budget would
        also leave this component unavailable, because `structural_diff_payload`
        re-counts rows across the whole result - but it would report the
        payload guard's wording and would not mark the shared budget
        exhausted, so the later components would not take the pre-call branch.
        """
        reports = self.review()["structural_changes"]["reports"]
        first = reports[0]
        ceiling = len(first["documents"][0]["changes"])
        self.assertLess(ceiling, _report_rows(first))

        starved = self._with_row_ceiling(ceiling)["structural_changes"]["reports"]
        report = starved[0]

        self.assertEqual(report["component"], first["component"])
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["reason"], "limit-exceeded")
        self.assertEqual(report["truncated"], True)
        self.assertEqual(report["documents"], [])
        self.assertEqual(report["summary"], {"added": 0, "removed": 0, "changed": 0})
        self.assertIn(
            f"{ceiling}-row aggregate output limit", report["detail"]
        )
        self.assertIn("No partial structural result was emitted", report["detail"])
        for later in starved[1:]:
            with self.subTest(component=later["component"]):
                self.assertEqual(
                    later["detail"],
                    "The aggregate structural-diff budget was already exhausted; "
                    "no partial rows were retained",
                )

    def test_the_structural_row_budget_carries_across_components_of_one_review(self):
        """Spending the whole ceiling on the first component starves the rest."""
        unconstrained = self.review()["structural_changes"]["reports"]
        ceiling = _report_rows(unconstrained[0])

        structural = self._with_row_ceiling(ceiling)["structural_changes"]
        reports = structural["reports"]

        self.assertEqual(
            [report["component"] for report in reports],
            [report["component"] for report in unconstrained],
        )
        self.assertEqual(reports[0]["status"], "complete")
        self.assertEqual(_report_rows(reports[0]), ceiling)
        for report in reports[1:]:
            with self.subTest(component=report["component"]):
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["reason"], "limit-exceeded")
                self.assertEqual(report["truncated"], True)
                self.assertEqual(report["documents"], [])
                self.assertEqual(
                    report["summary"], {"added": 0, "removed": 0, "changed": 0}
                )
        self.assertEqual(structural["complete"], False)
        self.assertEqual(structural["truncated"], True)

    def test_a_component_reached_after_exhaustion_is_never_asked_to_explain(self):
        """The third component takes the pre-call branch, not the raising one.

        The two branches produce the same reason and the same truncated flag,
        so only the detail distinguishes "this component spent the last row"
        from "this component was refused before its provider ran".
        """
        ceiling = _report_rows(self.review()["structural_changes"]["reports"][0])

        reports = self._with_row_ceiling(ceiling)["structural_changes"]["reports"]

        self.assertGreaterEqual(len(reports), 3)
        self.assertIn(
            f"{ceiling}-row aggregate output limit", reports[1]["detail"]
        )
        for report in reports[2:]:
            with self.subTest(component=report["component"]):
                self.assertEqual(
                    report["detail"],
                    "The aggregate structural-diff budget was already exhausted; "
                    "no partial rows were retained",
                )

    def test_a_starved_review_still_names_every_boundary_changed_component(self):
        """Truncation removes rows, never components: a silent drop is worse."""
        result = self._with_row_ceiling(1)
        structural = result["structural_changes"]

        self.assertEqual(
            [report["component"] for report in structural["reports"]],
            _boundary_changed(result),
        )
        self.assertEqual(structural["complete"], False)
        self.assertEqual(structural["truncated"], True)
        self.assertEqual(
            {report["reason"] for report in structural["reports"]},
            {"limit-exceeded"},
        )


# ---------------------------------------------------------------------------
# Row shape and RFC 6901 pointers
# ---------------------------------------------------------------------------

#: What a change row is allowed to contain, and nothing else.
CHANGE_ROW_KEYS = frozenset({"kind", "path", "before_type", "after_type"})
CHANGE_KINDS = frozenset({"added", "removed", "changed"})
JSON_TYPE_NAMES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)

#: Values planted in the diffed documents. None is a type name, a pointer
#: segment, or a plausible substring of one, so any appearance in the emitted
#: payload is a leak.
SENTINELS = (
    "SENTINEL-INTERNAL-HOSTNAME",
    "SENTINEL-CUSTOMER-IDENTIFIER",
    "SENTINEL-TOKEN-SHAPED-DEFAULT",
)

#: One structural situation each, as (before, after). Together they reach every
#: kind, every branch of ``_diff_json``, and both document statuses.
DIFF_CASES: Dict[str, Tuple[Any, Any]] = {
    "scalar changed": ({"a": SENTINELS[0]}, {"a": SENTINELS[1]}),
    "type changed": ({"a": SENTINELS[0]}, {"a": 4242}),
    "member added": ({}, {"a": SENTINELS[0]}),
    "member removed": ({"a": SENTINELS[0]}, {}),
    "subtree added": ({}, {"tree": {"deep": {"deeper": [SENTINELS[2]]}}}),
    "subtree removed": ({"tree": {"deep": [SENTINELS[2]]}}, {}),
    "array reordered": ({"a": [1, 2]}, {"a": [2, 1]}),
    "array grown": ({"a": [1]}, {"a": [1, {"x": SENTINELS[1]}]}),
    "array shrunk": ({"a": [1, SENTINELS[0]]}, {"a": [1]}),
    "tilde key": ({"a~b": 1, "c/d": 1}, {"a~b": 2, "c/d": 2}),
}

DOCUMENT_LABEL = "canonical:api.json"


def _entries(value: Any, label: str = DOCUMENT_LABEL) -> List[Tuple[str, bytes]]:
    return [(label, json.dumps(value, separators=(",", ":")).encode("utf-8"))]


def _payload(before: Any, after: Any, **kwargs: Any) -> dict:
    budget = StructuralDiffBudget()
    result = diff_canonical_json_entries(
        _entries(before, **kwargs), _entries(after, **kwargs), budget
    )
    return structural_diff_payload(result)


def _unescape_pointer_segment(segment: str) -> str:
    """RFC 6901 section 4, unescaping order fixed by the specification."""
    return segment.replace("~1", "/").replace("~0", "~")


def _segment_is_escaped(segment: str) -> bool:
    """Independent of the implementation: no raw '/', no stray '~'."""
    if "/" in segment:
        return False
    index = 0
    while index < len(segment):
        if segment[index] == "~":
            if index + 1 >= len(segment) or segment[index + 1] not in {"0", "1"}:
                return False
            index += 2
        else:
            index += 1
    return True


def _find_sentinels(payload: object) -> List[str]:
    """Every planted value that survived into the emitted payload."""
    rendered = json.dumps(payload, sort_keys=True)
    return [sentinel for sentinel in SENTINELS if sentinel in rendered]


class StructuralRowContentTests(unittest.TestCase):
    """OBL-GLOBS-033: pointers and type names, never a compared value."""

    def _rows(self) -> List[Tuple[str, dict]]:
        rows = []
        for name, (before, after) in DIFF_CASES.items():
            for document in _payload(before, after)["documents"]:
                for change in document["changes"]:
                    rows.append((name, change))
        return rows

    def test_every_case_in_the_table_produces_at_least_one_change_row(self):
        """Premise: a table entry that produced nothing would assert nothing."""
        for name, (before, after) in DIFF_CASES.items():
            with self.subTest(case=name):
                payload = _payload(before, after)
                self.assertTrue(payload["documents"], name)
                self.assertTrue(payload["documents"][0]["changes"], name)

    def test_every_change_row_carries_only_a_kind_a_pointer_and_json_types(self):
        rows = self._rows()
        self.assertGreaterEqual(len(rows), len(DIFF_CASES))
        for name, change in rows:
            with self.subTest(case=name, path=change["path"]):
                self.assertEqual(set(change), set(CHANGE_ROW_KEYS))
                self.assertIn(change["kind"], CHANGE_KINDS)
                for field in ("before_type", "after_type"):
                    if change[field] is not None:
                        self.assertIn(change[field], JSON_TYPE_NAMES)
                self.assertTrue(
                    change["path"] == ""
                    or (
                        change["path"].startswith("/")
                        and all(
                            _segment_is_escaped(segment)
                            for segment in change["path"][1:].split("/")
                        )
                    ),
                    change["path"],
                )

    def test_no_compared_value_reaches_any_emitted_structural_payload(self):
        for name, (before, after) in DIFF_CASES.items():
            with self.subTest(case=name):
                self.assertEqual(_find_sentinels(_payload(before, after)), [])

    def test_the_leak_scan_reports_a_value_that_did_reach_the_payload(self):
        """Premise for the assertion above: the scan is not vacuous."""
        payload = _payload(*DIFF_CASES["scalar changed"])
        self.assertEqual(_find_sentinels(payload), [])

        payload["documents"][0]["changes"][0]["before_value"] = SENTINELS[0]

        self.assertEqual(_find_sentinels(payload), [SENTINELS[0]])

    def test_an_added_subtree_is_reported_once_at_its_root(self):
        payload = _payload(*DIFF_CASES["subtree added"])

        changes = payload["documents"][0]["changes"]
        self.assertEqual(
            changes,
            [
                {
                    "kind": "added",
                    "path": "/tree",
                    "before_type": None,
                    "after_type": "object",
                }
            ],
        )

    def test_a_removed_subtree_is_reported_once_at_its_root(self):
        payload = _payload(*DIFF_CASES["subtree removed"])

        self.assertEqual(
            payload["documents"][0]["changes"],
            [
                {
                    "kind": "removed",
                    "path": "/tree",
                    "before_type": "object",
                    "after_type": None,
                }
            ],
        )

    def test_a_whole_added_document_is_reported_once_at_the_empty_pointer(self):
        budget = StructuralDiffBudget()
        result = diff_canonical_json_entries(
            [], _entries({"k": SENTINELS[0]}, label="canonical:new.json"), budget
        )

        payload = structural_diff_payload(result)

        self.assertEqual(payload["documents"][0]["status"], "added")
        self.assertEqual(
            payload["documents"][0]["changes"],
            [
                {
                    "kind": "added",
                    "path": "",
                    "before_type": None,
                    "after_type": "object",
                }
            ],
        )
        self.assertEqual(_find_sentinels(payload), [])

    def test_arrays_are_compared_positionally_rather_than_as_sets(self):
        """A reordered array is two positional changes, not zero."""
        payload = _payload(*DIFF_CASES["array reordered"])

        self.assertEqual(
            [change["path"] for change in payload["documents"][0]["changes"]],
            ["/a/0", "/a/1"],
        )
        self.assertEqual(payload["summary"]["changed"], 2)

    def test_a_lengthened_array_reports_only_the_new_positions(self):
        payload = _payload(*DIFF_CASES["array grown"])

        self.assertEqual(
            payload["documents"][0]["changes"],
            [
                {
                    "kind": "added",
                    "path": "/a/1",
                    "before_type": None,
                    "after_type": "object",
                }
            ],
        )

    def test_tilde_and_slash_keys_reach_the_change_row_escaped(self):
        payload = _payload(*DIFF_CASES["tilde key"])

        self.assertEqual(
            [change["path"] for change in payload["documents"][0]["changes"]],
            ["/a~0b", "/c~1d"],
        )


#: Characters chosen so a short random key is dense in the cases the escape
#: has to get right: the two reserved characters, and the two escape sequences
#: themselves, which a non-invertible implementation mangles.
POINTER_ALPHABET = "~/01ab"

POINTER_PROFILE = settings(max_examples=400, deadline=None)


class PointerEscapeRoundTripTests(unittest.TestCase):
    """OBL-GLOBS-033: `~` becomes `~0`, `/` becomes `~1`, invertibly."""

    @POINTER_PROFILE
    @given(
        parent=st.sampled_from(["", "/paths", "/a~0b"]),
        key=st.text(alphabet=POINTER_ALPHABET, max_size=8),
    )
    def test_a_pointer_segment_unescapes_back_to_the_key_it_came_from(self, parent, key):
        budget = StructuralDiffBudget()

        child = budget.pointer_child(parent, key)

        self.assertTrue(child.startswith(parent + "/"))
        segment = child[len(parent) + 1 :]
        self.assertTrue(_segment_is_escaped(segment), segment)
        self.assertEqual(_unescape_pointer_segment(segment), key)

    @POINTER_PROFILE
    @given(index=st.integers(min_value=0, max_value=10_000))
    def test_an_array_index_becomes_its_decimal_segment_unescaped(self, index):
        budget = StructuralDiffBudget()

        child = budget.pointer_child("/a", index)

        self.assertEqual(child, f"/a/{index}")

    def test_the_round_trip_oracle_rejects_escapes_applied_in_the_wrong_order(self):
        """Premise: the property above can fail.

        Escaping `/` before `~` turns a slash into `~01`, which unescapes to
        the literal `~1`. Without this test, a green property run would not
        distinguish a correct implementation from an oracle that accepts
        anything.
        """

        def wrong_order(key: str) -> str:
            return key.replace("/", "~1").replace("~", "~0")

        self.assertEqual(wrong_order("/"), "~01")
        self.assertNotEqual(_unescape_pointer_segment(wrong_order("/")), "/")
        self.assertEqual(_unescape_pointer_segment(wrong_order("/")), "~1")

        correct = StructuralDiffBudget().pointer_child("", "/")
        self.assertEqual(correct, "/~1")
        self.assertEqual(_unescape_pointer_segment(correct[1:]), "/")

    def test_an_unescaped_segment_would_fail_the_shape_check(self):
        """Premise for `_segment_is_escaped`: it rejects what it must reject."""
        self.assertFalse(_segment_is_escaped("a/b"))
        self.assertFalse(_segment_is_escaped("a~b"))
        self.assertFalse(_segment_is_escaped("trailing~"))
        self.assertTrue(_segment_is_escaped("a~0b"))
        self.assertTrue(_segment_is_escaped("a~1b"))
        self.assertTrue(_segment_is_escaped(""))


# ---------------------------------------------------------------------------
# Which failures are the reviewer's problem, and which are the review's
# ---------------------------------------------------------------------------

#: Every aggregate ceiling the review host declares, read from the module so a
#: ceiling added later is checked without editing this table. Each must fail
#: the command outright rather than shrink the result.
REVIEW_CEILINGS = tuple(
    sorted(
        name
        for name in dir(review_module)
        if name.startswith("MAX_REVIEW_")
        and name != "MAX_REVIEW_RECONCILIATION_CANDIDATES"
    )
)

#: The sentence each ceiling produces when it alone is lowered to 1, observed
#: on this fixture. Three names, two guards: `MAX_REVIEW_RESULT_ROWS` and
#: `MAX_REVIEW_RESULT_BYTES` are read by one `or` at ``_review.py:83-91`` and
#: report each other's unpatched value, so only the message says which of the
#: two the run actually reached. Recording it keeps the table below from
#: claiming three code paths where the product has two.
CEILING_MESSAGES = {
    "MAX_REVIEW_WORK_STEPS": (
        "Range review graph and slice analysis exceeds the 1-step aggregate "
        "limit."
    ),
    "MAX_REVIEW_RESULT_ROWS": (
        "Range review result exceeds the aggregate 1-row or 67108864-byte "
        "construction limit."
    ),
    "MAX_REVIEW_RESULT_BYTES": (
        "Range review result exceeds the aggregate 100000-row or 1-byte "
        "construction limit."
    ),
}


class ReviewCompletenessTests(_RangeFixture):
    """OBL-GLOBS-034: two completeness flags, three fatal ceilings."""

    def test_the_declared_review_ceilings_are_the_three_the_contract_names(self):
        """Pin the enumeration so the checker below cannot quietly go empty."""
        self.assertEqual(
            REVIEW_CEILINGS,
            (
                "MAX_REVIEW_RESULT_BYTES",
                "MAX_REVIEW_RESULT_ROWS",
                "MAX_REVIEW_WORK_STEPS",
            ),
        )
        self.assertEqual(review_module.MAX_REVIEW_WORK_STEPS, 250_000)
        self.assertEqual(review_module.MAX_REVIEW_RESULT_ROWS, 100_000)
        self.assertEqual(review_module.MAX_REVIEW_RESULT_BYTES, 64 * 1024 * 1024)
        self.assertEqual(review_module.MAX_REVIEW_RECONCILIATION_CANDIDATES, 8)

    def test_the_unconstrained_command_exits_zero_and_prints_a_result(self):
        """Premise: the ceiling checker's empty stdout has to mean something."""
        completed = run_cli_in_process(
            self.scene.root,
            "review",
            f"{self.base}..{self.target}",
            "--format",
            "json",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema"], "boundver-review/v1")
        self.assertEqual(payload["complete"], True)

    def test_every_review_ceiling_exits_two_without_emitting_a_result(self):
        """Each ceiling fails the command, and says which guard it reached.

        Without the message assertion this table would report three passes
        where the product has two guards, and a ceiling added later with no
        recorded message would look covered. An unknown name fails outright
        rather than being skipped.
        """
        for name in REVIEW_CEILINGS:
            with self.subTest(ceiling=name):
                self.assertIn(
                    name,
                    CEILING_MESSAGES,
                    f"{name} is declared but no observed refusal is recorded",
                )
                with mock.patch.object(review_module, name, 1):
                    completed = run_cli_in_process(
                        self.scene.root,
                        "review",
                        f"{self.base}..{self.target}",
                        "--format",
                        "json",
                    )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(completed.stdout, "")
                self.assertIn("ERROR: review failed:", completed.stderr)
                self.assertIn(CEILING_MESSAGES[name], completed.stderr)
                self.assertIn(
                    "No partial review result was emitted", completed.stderr
                )

    def test_the_row_and_byte_ceilings_are_two_names_for_one_guard(self):
        """Record the shape the table above cannot: three names, two guards.

        `MAX_REVIEW_RESULT_ROWS` and `MAX_REVIEW_RESULT_BYTES` are the two
        halves of one `or`, so each reports the other's unpatched value and
        neither can be lowered without the other's number appearing. If they
        are ever split into separate guards, the two messages stop sharing a
        prefix and this fails.
        """
        rows = CEILING_MESSAGES["MAX_REVIEW_RESULT_ROWS"]
        byts = CEILING_MESSAGES["MAX_REVIEW_RESULT_BYTES"]
        steps = CEILING_MESSAGES["MAX_REVIEW_WORK_STEPS"]
        prefix = "Range review result exceeds the aggregate "

        self.assertTrue(rows.startswith(prefix), rows)
        self.assertTrue(byts.startswith(prefix), byts)
        self.assertNotEqual(rows, byts)
        self.assertFalse(steps.startswith(prefix), steps)
        self.assertIn(f"{review_module.MAX_REVIEW_RESULT_BYTES}-byte", rows)
        self.assertIn(f"{review_module.MAX_REVIEW_RESULT_ROWS}-row", byts)

    def test_a_byte_ceiling_of_one_never_reaches_the_complete_json_guard(self):
        """The construction guard is strictly the earlier of the two.

        `MAX_REVIEW_RESULT_BYTES` is read twice: once per reserved row while
        the result is built, and once over the finished document. Lowering it
        to 1 can only ever reach the first, so the table entry above proves
        nothing about the second, which the oversize test below owns.
        """
        with mock.patch.object(review_module, "MAX_REVIEW_RESULT_BYTES", 1):
            with self.assertRaises(GuardrailError) as caught:
                self.review()

        message = str(caught.exception)
        self.assertIn("1-byte construction limit", message)
        self.assertNotIn("complete JSON limit", message)
        self.assertNotIn("complete-output limit", message)

    def test_an_oversized_complete_result_is_refused_rather_than_truncated(self):
        """The final whole-document guard, distinct from the per-row ceiling."""
        rendered = len(json.dumps(self.review(), sort_keys=True).encode("utf-8"))

        with mock.patch.object(review_module, "MAX_REVIEW_RESULT_BYTES", rendered - 1):
            with self.assertRaises(GuardrailError) as caught:
                self.review()

        self.assertIn(
            f"exceeds the {rendered - 1}-byte complete JSON limit", str(caught.exception)
        )
        self.assertNotIn("construction limit", str(caught.exception))
        self.assertIn("No partial review result was emitted", str(caught.exception))

    def test_the_text_output_guard_refuses_a_rendering_that_reaches_it(self):
        """Premise: the third byte guard is enforced, not decoration.

        `review_text_lines` measures its own rendering against the same
        ceiling. The test below shows the CLI can never hand it one that
        exceeds it, which is only a finding if the guard would have fired.
        """
        result = self.review()
        lines = review_module.review_text_lines(result)
        rendered = _text_bytes(lines)

        with mock.patch.object(review_module, "MAX_REVIEW_RESULT_BYTES", rendered):
            self.assertEqual(review_module.review_text_lines(result), lines)

        with mock.patch.object(review_module, "MAX_REVIEW_RESULT_BYTES", rendered - 1):
            with self.assertRaises(GuardrailError) as caught:
                review_module.review_text_lines(result)

        self.assertIn(
            f"exceeds the {rendered - 1}-byte complete-output limit",
            str(caught.exception),
        )
        self.assertIn("No partial review result was emitted", str(caught.exception))

    def test_no_text_format_run_can_reach_the_text_output_guard(self):
        """Finding: the complete-output limit is dominated and cannot fire.

        `core.py:1180-1199` calls `analyze_review_range` and only then renders
        text, and both read `MAX_REVIEW_RESULT_BYTES`. Reaching the text guard
        needs a ceiling at or above the JSON encoding and below the text
        rendering, and the text is a lossy projection of that JSON - twelve
        digest characters where the JSON carries sixty-four - so no such
        ceiling exists. Both runs below fail earlier, and neither mentions the
        guard the premise test above proved works.
        """
        result = self.review()
        json_bytes = len(json.dumps(result, sort_keys=True).encode("utf-8"))
        text_bytes = _text_bytes(review_module.review_text_lines(result))

        self.assertLess(text_bytes, json_bytes)

        for ceiling, expected in (
            (
                text_bytes - 1,
                f"aggregate {review_module.MAX_REVIEW_RESULT_ROWS}-row or "
                f"{text_bytes - 1}-byte construction limit",
            ),
            (json_bytes - 1, f"exceeds the {json_bytes - 1}-byte complete JSON limit"),
        ):
            with self.subTest(ceiling=ceiling):
                with mock.patch.object(
                    review_module, "MAX_REVIEW_RESULT_BYTES", ceiling
                ):
                    completed = run_cli_in_process(
                        self.scene.root,
                        "review",
                        f"{self.base}..{self.target}",
                        "--format",
                        "text",
                    )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(completed.stdout, "")
                self.assertIn(expected, completed.stderr)
                self.assertNotIn("complete-output limit", completed.stderr)

    def test_a_structural_budget_failure_leaves_the_review_itself_complete(self):
        with mock.patch.object(provider_diff_module, "MAX_PROVIDER_DIFF_ROWS", 1):
            completed = run_cli_in_process(
                self.scene.root,
                "review",
                f"{self.base}..{self.target}",
                "--format",
                "json",
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["complete"], True)
        structural = payload["structural_changes"]
        self.assertEqual(structural["complete"], False)
        self.assertEqual(structural["truncated"], True)
        for report in structural["reports"]:
            with self.subTest(component=report["component"]):
                self.assertEqual(report["reason"], "limit-exceeded")
                self.assertEqual(report["documents"], [])

    def test_the_top_level_complete_flag_is_a_literal_in_the_result(self):
        """Known limitation, recorded rather than asserted behaviourally.

        The obligation says an unsupported provider must leave the top-level
        `complete` true. Nothing can make it false: it is written as `True` in
        the returned dict. Pin the literal, so a change to a computed value
        fails here and has to re-establish the claim with real inputs.
        """
        source = Path(review_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "analyze_review_range"
        )
        literals = [
            value
            for node in ast.walk(function)
            if isinstance(node, ast.Dict)
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and key.value == "complete"
        ]

        self.assertEqual(len(literals), 1)
        self.assertIsInstance(literals[0], ast.Constant)
        self.assertIs(literals[0].value, True)


class UnsupportedProviderReviewTests(_RangeFixture):
    """OBL-GLOBS-034: a raw provider explains nothing and invalidates nothing."""

    provider = "path-hash"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.witness = Scenario()
        cls.witness_base, cls.witness_target = _build_range(
            cls.witness, "openapi-canonical"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.witness.close()
        super().tearDownClass()

    def witness_review(self) -> dict:
        return analyze_review_range(
            self.witness.root, self.witness_base, self.witness_target
        )

    def test_a_provider_without_the_structural_interface_exits_zero(self):
        completed = run_cli(
            self.scene.root,
            "review",
            f"{self.base}..{self.target}",
            "--format",
            "json",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["complete"], True)
        structural = payload["structural_changes"]
        self.assertEqual(structural["complete"], False)
        self.assertEqual(structural["truncated"], False)
        self.assertEqual(
            [report["component"] for report in structural["reports"]],
            _boundary_changed(payload),
        )
        for report in structural["reports"]:
            with self.subTest(component=report["component"]):
                self.assertEqual(report["status"], "unavailable")
                self.assertEqual(report["reason"], "provider-unsupported")
                self.assertEqual(report["truncated"], False)
                self.assertEqual(report["documents"], [])
                self.assertEqual(
                    report["summary"], {"added": 0, "removed": 0, "changed": 0}
                )

    def test_the_same_range_content_under_a_supported_provider_is_truncated(self):
        """Premise: a ceiling of one row is not inert on this range's content.

        The witness repository is built by the same `_build_range` from the
        same three components and the same six documents; only the configured
        provider differs. Without it, the test below would assert that
        `truncated` stayed false under a ceiling this fixture's code path never
        reads, which is an absence asserted where nothing was engaged.
        """
        with mock.patch.object(provider_diff_module, "MAX_PROVIDER_DIFF_ROWS", 1):
            structural = self.witness_review()["structural_changes"]

        self.assertEqual(structural["truncated"], True)
        self.assertEqual(structural["complete"], False)
        self.assertEqual(
            [report["component"] for report in structural["reports"]],
            list(RANGE_COMPONENTS),
        )
        self.assertEqual(
            {report["reason"] for report in structural["reports"]},
            {"limit-exceeded"},
        )
        self.assertEqual(
            {report["truncated"] for report in structural["reports"]}, {True}
        )

    def test_an_unsupported_provider_is_not_reported_as_truncated(self):
        """The two unavailable reasons must stay distinguishable.

        `provider-unsupported` and `limit-exceeded` both empty the document
        list; only `truncated` says whether evidence was withheld or never
        existed. The witness above turns that flag true on the same range
        content under `openapi-canonical`, so its staying false here is a fact
        about the provider. What the raw provider does is stronger than not
        truncating: `_provider_method` refuses it at `_structural_review.py:143`
        before the shared budget is consulted, so the whole report is
        byte-identical with the ceiling lowered and without it.
        """
        unpatched = self.review()["structural_changes"]

        self.assertEqual(unpatched["complete"], False)
        self.assertEqual(unpatched["truncated"], False)
        with mock.patch.object(provider_diff_module, "MAX_PROVIDER_DIFF_ROWS", 1):
            starved = self.review()["structural_changes"]

        self.assertEqual(starved["truncated"], False)
        self.assertEqual(
            {report["reason"] for report in starved["reports"]},
            {"provider-unsupported"},
        )
        self.assertEqual(starved, unpatched)


# ---------------------------------------------------------------------------
# The behavior envelope with no boundary digest to bind
# ---------------------------------------------------------------------------

#: Every reachable way a component can carry a behavior fingerprint and no
#: boundary digest, with the status the lockfile records for it. `error` is
#: absent on purpose: generation refuses to write it, which is pinned below.
NULL_BOUNDARY_PROVIDERS = {
    "implicit": "partial",
    "leaf": "ok",
}

BEHAVIOR_PATHS = ("behavior.txt",)


def _raw_behavior_digest(
    root: Path, component_path: str, paths, accessor: _SourceAccessor
) -> str:
    """The inner digest, computed the way `_compute_component_entry` does.

    Recomputing it here is what makes the envelope assertion an equality
    against a formula rather than against whatever the code produced.
    """
    provider = PathHashProvider()
    provider.name = "behavior"
    ctx = ProviderContext(
        repo_root=root,
        component_path=component_path,
        boundary_cfg={"paths": list(paths)},
        source="head",
        read_file=accessor.read_file,
        read_file_limited=accessor.read_file_limited,
        list_files=accessor.list_files,
    )
    digest, status, errors = compute_boundary(provider, ctx)
    if digest is None:
        raise AssertionError(f"behavior fixture did not resolve: {status} {errors}")
    return digest


def _envelope(raw: str, boundary_identity: str) -> str:
    return _hash_framed_entries(
        [
            ("behavior", raw.encode("ascii")),
            ("boundary", boundary_identity.encode("ascii")),
        ],
        domain=HASH_DOMAIN_BEHAVIOR,
    )


def _null_boundary_scene(provider: str) -> Scenario:
    scene = Scenario()
    scene.component(
        "svc",
        path="svc",
        provider=provider,
        boundary=[],
        behavior=list(BEHAVIOR_PATHS),
    )
    scene.file("svc/behavior.txt", "behavior one\n")
    scene.file("svc/impl.txt", "implementation\n")
    scene.commit()
    return scene


class BehaviorEnvelopeTests(unittest.TestCase):
    """OBL-HASHING-025 and OBL-HASHING-045: what the envelope binds."""

    def test_a_boundary_only_change_moves_a_disjoint_behavior_fingerprint(self):
        with Scenario() as scene:
            scene.component(
                "svc",
                path="svc",
                provider="path-hash",
                boundary=["api/contract.json"],
                behavior=["impl.txt"],
            )
            scene.json_file("svc/api/contract.json", {"v": 1})
            scene.file("svc/impl.txt", "implementation one\n")
            scene.commit()
            before = scene.fingerprints("svc")

            scene.json_file("svc/api/contract.json", {"v": 2})
            scene.commit("boundary only")
            after = scene.fingerprints("svc")

            self.assertNotEqual(before["boundary"], after["boundary"])
            self.assertNotEqual(before["behavior"], after["behavior"])

    def test_a_behavior_only_change_leaves_the_boundary_digest_alone(self):
        """Premise: the two selections really are disjoint.

        Without this, the test above would pass just as well if the behavior
        provider were silently selecting the boundary file too.
        """
        with Scenario() as scene:
            scene.component(
                "svc",
                path="svc",
                provider="path-hash",
                boundary=["api/contract.json"],
                behavior=["impl.txt"],
            )
            scene.json_file("svc/api/contract.json", {"v": 1})
            scene.file("svc/impl.txt", "implementation one\n")
            scene.commit()
            before = scene.fingerprints("svc")

            scene.file("svc/impl.txt", "implementation two\n")
            scene.commit("behavior only")
            after = scene.fingerprints("svc")

            self.assertEqual(before["boundary"], after["boundary"])
            self.assertNotEqual(before["behavior"], after["behavior"])

    def test_an_absent_boundary_digest_binds_none_and_the_recorded_status(self):
        for provider, status in NULL_BOUNDARY_PROVIDERS.items():
            with self.subTest(provider=provider):
                with _null_boundary_scene(provider) as scene:
                    entry = scene.generate()["components"]["svc"]
                    accessor = _SourceAccessor(scene.root, "head")
                    with accessor:
                        raw = _raw_behavior_digest(
                            scene.root, "svc", BEHAVIOR_PATHS, accessor
                        )

                    self.assertEqual(entry["boundary_status"], status)
                    self.assertIsNone(entry["fingerprints"]["boundary"])
                    self.assertEqual(
                        entry["fingerprints"]["behavior"],
                        _envelope(raw, f"none:{status}"),
                    )
                    self.assertNotEqual(entry["fingerprints"]["behavior"], raw)

    def test_two_absent_boundary_statuses_produce_two_behavior_fingerprints(self):
        """Metamorphic: identical behavior content, different recorded status."""
        digests = {}
        raws = set()
        for provider in NULL_BOUNDARY_PROVIDERS:
            with _null_boundary_scene(provider) as scene:
                entry = scene.generate()["components"]["svc"]
                accessor = _SourceAccessor(scene.root, "head")
                with accessor:
                    raws.add(
                        _raw_behavior_digest(
                            scene.root, "svc", BEHAVIOR_PATHS, accessor
                        )
                    )
                digests[provider] = entry["fingerprints"]["behavior"]

        self.assertEqual(len(raws), 1, "the behavior content must be identical")
        self.assertEqual(len(set(digests.values())), len(NULL_BOUNDARY_PROVIDERS))

    def test_an_error_boundary_status_binds_none_error(self):
        """The third arm, exercised where it lives.

        `boundary_status == "error"` never reaches a generated lockfile, so
        this drives `_compute_component_entry` directly rather than pretending
        `generate --allow-partial` can produce it.
        """
        with Scenario() as scene:
            scene.component(
                "svc",
                path="svc",
                provider="openapi-canonical",
                boundary=["api.json"],
                behavior=list(BEHAVIOR_PATHS),
            )
            scene.file("svc/api.json", '{"not": "openapi"}\n')
            scene.file("svc/behavior.txt", "behavior one\n")
            scene.commit()
            accessor = _SourceAccessor(scene.root, "head")
            with accessor:
                entry = _compute_component_entry(
                    "svc",
                    scene.config["components"]["svc"],
                    scene.root,
                    "head",
                    {},
                    accessor,
                    create_registry(),
                )
                raw = _raw_behavior_digest(
                    scene.root, "svc", BEHAVIOR_PATHS, accessor
                )

            self.assertEqual(entry["boundary_status"], "error")
            self.assertIsNone(entry["fingerprints"]["boundary"])
            self.assertEqual(
                entry["fingerprints"]["behavior"], _envelope(raw, "none:error")
            )
            self.assertNotEqual(
                entry["fingerprints"]["behavior"], _envelope(raw, "none:partial")
            )

    def test_generation_refuses_to_write_an_error_boundary_status(self):
        """The register expects `--allow-partial` to bless this. It does not.

        "Lockfile generation failed" alone is the header every refusal carries,
        including ones this fixture is not about - a mistyped provider name, a
        missing file. The claim is that *this* component's *boundary error* is
        what generation refuses, and that lives in the second line, so the
        component and the provider's own reason are both pinned. The companion
        test above proves the same fixture really produces boundary_status
        "error" through `_compute_component_entry`.
        """
        with Scenario() as scene:
            scene.component(
                "svc",
                path="svc",
                provider="openapi-canonical",
                boundary=["api.json"],
                behavior=list(BEHAVIOR_PATHS),
            )
            scene.file("svc/api.json", '{"not": "openapi"}\n')
            scene.file("svc/behavior.txt", "behavior one\n")
            scene.commit()

            for strict in (True, False):
                with self.subTest(strict=strict):
                    with self.assertRaises(ConfigError) as caught:
                        generate_lockfile(scene.config, scene.root, strict=strict)
                    message = str(caught.exception)
                    self.assertEqual(
                        message,
                        "Lockfile generation failed:\n"
                        "svc: OpenAPI canonicalization failed for api.json: "
                        "OpenAPI document must declare 'openapi' 3.0.x/3.1.x "
                        "or 'swagger' 2.0",
                    )

    def test_the_envelope_is_hashed_over_exactly_two_labels_in_its_own_domain(self):
        with _null_boundary_scene("implicit") as scene:
            stored = scene.generate()["components"]["svc"]["fingerprints"]["behavior"]
            accessor = _SourceAccessor(scene.root, "head")
            with accessor:
                raw = _raw_behavior_digest(
                    scene.root, "svc", BEHAVIOR_PATHS, accessor
                )

        entries = [
            ("behavior", raw.encode("ascii")),
            ("boundary", b"none:partial"),
        ]
        self.assertEqual(
            stored, _hash_framed_entries(entries, domain=HASH_DOMAIN_BEHAVIOR)
        )
        self.assertEqual(HASH_DOMAIN_BEHAVIOR, "behavior-envelope")

        variants = {
            "another domain": _hash_framed_entries(
                entries, domain=HASH_DOMAIN_BOUNDARY
            ),
            "renamed labels": _hash_framed_entries(
                [("inner", raw.encode("ascii")), ("outer", b"none:partial")],
                domain=HASH_DOMAIN_BEHAVIOR,
            ),
            "behavior alone": _hash_framed_entries(
                [("behavior", raw.encode("ascii"))], domain=HASH_DOMAIN_BEHAVIOR
            ),
            "boundary status dropped": _hash_framed_entries(
                [("behavior", raw.encode("ascii")), ("boundary", b"none:")],
                domain=HASH_DOMAIN_BEHAVIOR,
            ),
        }
        for name, digest in variants.items():
            with self.subTest(variant=name):
                self.assertNotEqual(stored, digest)

    def test_the_envelope_entries_are_hashed_as_semantic_values(self):
        """The two-field entry form must mean mode `semantic`, type `value`.

        `_hash_framed_entries` reads mode and object type off the content
        object, so plain `bytes` take a documented default. Spelling that
        default out here means a change to it fails a test instead of silently
        rotating every configured behavior fingerprint.
        """
        content = b"none:partial"
        implicit = _hash_framed_entries(
            [("behavior", b"inner"), ("boundary", content)],
            domain=HASH_DOMAIN_BEHAVIOR,
        )
        explicit = _hash_framed_entries(
            [
                ("behavior", "semantic", "value", b"inner"),
                ("boundary", "semantic", "value", content),
            ],
            domain=HASH_DOMAIN_BEHAVIOR,
        )
        other = _hash_framed_entries(
            [
                ("behavior", "semantic", "value", b"inner"),
                ("boundary", "100644", "blob", content),
            ],
            domain=HASH_DOMAIN_BEHAVIOR,
        )

        self.assertEqual(implicit, explicit)
        self.assertNotEqual(implicit, other)


# ---------------------------------------------------------------------------
# x-* specification extensions
# ---------------------------------------------------------------------------

#: The names `_strip_openapi` removes at every nesting level. Read from the
#: module so a sixth annotation name is covered without editing this file.
ANNOTATION_NAMES = tuple(sorted(_OPENAPI_STRIP_KEYS))


def _extension_document(extension: object) -> dict:
    return {"openapi": "3.1.0", "paths": {}, "x-gateway": extension}


class OpenApiExtensionMemberTests(unittest.TestCase):
    """OBL-HASHING-032: what survives inside an `x-*` specification extension."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = Scenario()
        cls.scene.component(
            "svc", path="svc", provider="openapi-canonical", boundary=["api.json"]
        )
        cls.scene.json_file("svc/api.json", {"openapi": "3.1.0", "paths": {}})
        cls.scene.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scene.close()

    def digest(self, extension: object) -> Optional[str]:
        """The boundary digest the provider records for this extension.

        Read from the working tree so one repository can answer many variants
        without a commit per digest.
        """
        self.scene.json_file("svc/api.json", _extension_document(extension))
        return self.scene.digest("svc", "boundary", source="working-tree")

    def test_a_scalar_extension_value_rotates_the_digest(self):
        """Premise: the harness observes rotation when rotation happens."""
        self.assertNotEqual(self.digest("prod"), self.digest("staging"))

    def test_an_ordinary_extension_member_rotates_the_digest(self):
        """Premise for the equalities below: nothing about `x-*` is inert."""
        self.assertNotEqual(
            self.digest({"route": "prod"}), self.digest({"route": "staging"})
        )
        self.assertNotEqual(
            self.digest({"gateway": {"route": "prod"}}),
            self.digest({"gateway": {"route": "staging"}}),
        )

    def test_the_annotation_name_table_is_not_empty(self):
        self.assertEqual(
            ANNOTATION_NAMES,
            ("description", "example", "examples", "externalDocs", "summary"),
        )

    def test_an_annotation_named_extension_member_rotates_the_digest(self):
        for name in ANNOTATION_NAMES:
            with self.subTest(member=name):
                self.assertNotEqual(
                    self.digest({name: "prod"}), self.digest({name: "staging"})
                )

    def test_every_annotation_named_extension_member_is_preserved(self):
        for name in ANNOTATION_NAMES:
            with self.subTest(member=name):
                self.assertNotEqual(
                    self.digest({name: "prod"}), self.digest({name: "staging"})
                )
                self.assertEqual(
                    _strip_openapi(_extension_document({name: "prod"}))["x-gateway"],
                    {name: "prod"},
                )

    def test_an_annotation_named_member_is_preserved_at_any_depth_in_an_extension(self):
        for name in ANNOTATION_NAMES:
            with self.subTest(member=name):
                nested = {"routing": {"upstream": {name: "prod"}}}
                self.assertEqual(
                    _strip_openapi(_extension_document(nested))["x-gateway"],
                    nested,
                )
                self.assertNotEqual(
                    self.digest(nested),
                    self.digest({"routing": {"upstream": {name: "staging"}}}),
                )

    def test_an_extension_whose_members_are_ordinary_names_survives_intact(self):
        extension = {"routing": {"upstream": "svc", "weights": [1, 2]}}

        self.assertEqual(
            _strip_openapi(_extension_document(extension))["x-gateway"], extension
        )


if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()
