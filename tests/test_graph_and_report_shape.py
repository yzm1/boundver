"""A closure that must terminate, a diagnostic that must know its owner, and
a document whose shape must not depend on the news it carries.

A consumer graph is repository-controlled, so a cycle in it is an input rather
than a bug, and the closure has to answer anyway. A consumer diagnostic is
rendered text that has to be tied back to the mismatch it belongs to, using a
display string that may itself contain the separator. A bounded consumer
preview is a document of the same kind: it prints a few names and counts the
rest, and that count is the only trace the names it omits ever leave. And a
machine-readable verify result is only machine-readable if its key set is the
same whatever happened.

Covers OBL-GRAPH-006, OBL-GRAPH-010, OBL-GRAPH-013, OBL-GRAPH-014 and
OBL-GRAPH-017.
"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver._baseline import violation_identity
from boundver._consumer_graph import consumer_closure
from boundver._output import print_consumer_impact
from boundver._utils import _bounded_diagnostic_list_preview

from tests._parity import run_cli
from tests._scenarios import Scenario

COULD_NOT_CHECK = 2
DRIFT = 1

PROFILE = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

NAMES = ("a", "b", "c", "d")

#: Arrays the JSON view must always carry, whatever the outcome.
ALWAYS_ARRAYS = ("issues", "observations", "notices", "consumer_impact")


def _graph(edges: dict) -> dict:
    return {name: {"consumers": sorted(targets)} for name, targets in edges.items()}


class ConsumerClosureTests(unittest.TestCase):
    """OBL-GRAPH-010: terminate, once each, and the seed only when asked."""

    CYCLES = {
        "two cycle": {"a": {"b"}, "b": {"a"}},
        "three cycle": {"a": {"b"}, "b": {"c"}, "c": {"a"}},
        "self edge": {"a": {"a"}},
        "cycle off the seed": {"a": {"b"}, "b": {"c"}, "c": {"b"}},
    }

    def test_a_seed_reachable_from_itself_is_excluded_by_default(self):
        """The branch the gap named: include_seeds=False on a cyclic seed."""
        for label, edges in self.CYCLES.items():
            with self.subTest(graph=label):
                closure = consumer_closure(_graph(edges), ["a"])
                self.assertNotIn("a", closure)

    def test_the_seed_appears_exactly_once_when_included(self):
        for label, edges in self.CYCLES.items():
            with self.subTest(graph=label):
                closure = consumer_closure(_graph(edges), ["a"], include_seeds=True)
                self.assertEqual(closure.count("a"), 1)

    def test_every_reachable_node_appears_exactly_once(self):
        closure = consumer_closure(_graph({"a": {"b"}, "b": {"c"}, "c": {"a"}}), ["a"])
        self.assertEqual(closure, ["b", "c"])

    def test_duplicate_and_malformed_edges_are_ignored(self):
        graph = {"a": {"consumers": ["b", "b", 3, None]}, "b": {}}
        self.assertEqual(consumer_closure(graph, ["a"]), ["b"])

    def test_an_unknown_seed_reaches_nothing(self):
        """The premise: an empty answer can be right."""
        self.assertEqual(consumer_closure(_graph({"a": {"b"}}), ["missing"]), [])

    @PROFILE
    @given(
        edges=st.dictionaries(
            st.sampled_from(NAMES),
            st.frozensets(st.sampled_from(NAMES), max_size=len(NAMES)),
            max_size=len(NAMES),
        ),
        seed=st.sampled_from(NAMES),
    )
    def test_the_closure_is_deterministic_and_seed_free(self, edges, seed):
        """Over arbitrary graphs, cycles and self-edges included."""
        graph = _graph({name: set(targets) for name, targets in edges.items()})
        closure = consumer_closure(graph, [seed])
        self.assertNotIn(seed, closure)
        self.assertEqual(len(closure), len(set(closure)))
        self.assertEqual(closure, sorted(closure))
        with_seed = consumer_closure(graph, [seed], include_seeds=True)
        self.assertEqual(with_seed, sorted(set(closure) | {seed}))


class ConsumerDiagnosticIdentityTests(unittest.TestCase):
    """OBL-GRAPH-013: bound to its owner, or bound to nothing."""

    DIRECT = "AFFECTED CONSUMERS svc: sdk"
    TRANSITIVE = "AFFECTED CONSUMERS (TRANSITIVE) svc: sdk"
    #: Owned by 'svcx', whose name begins with the unrelated component 'svc'.
    PREFIX_OF_OWNER = "AFFECTED CONSUMERS svcx: sdk"

    def test_a_mismatch_line_identifies_itself(self):
        """The premise: the component-facet form is what a subject comes from."""
        identity = violation_identity("MISMATCH svc.boundary: lockfile=a current=b")
        self.assertEqual(identity["kind"], "component-facet")
        self.assertEqual((identity["subject"], identity["facet"]), ("svc", "boundary"))

    def test_a_consumer_line_binds_to_a_known_subject(self):
        for issue, facet in ((self.DIRECT, "direct"), (self.TRANSITIVE, "transitive")):
            with self.subTest(issue=issue):
                identity = violation_identity(issue, component_subjects={"svc"})
                self.assertEqual(identity["kind"], "affected-consumers")
                self.assertEqual((identity["subject"], identity["facet"]), ("svc", facet))

    def test_a_consumer_list_may_begin_with_another_name(self):
        """The display string carries ': ' inside it and still binds right."""
        identity = violation_identity(
            "AFFECTED CONSUMERS svc: other: sdk", component_subjects={"svc"}
        )
        self.assertEqual(identity["subject"], "svc")

    def test_the_longest_matching_subject_wins(self):
        """Which is how a component name containing ': ' stays unambiguous."""
        identity = violation_identity(
            "AFFECTED CONSUMERS svc: other: sdk",
            component_subjects={"svc", "svc: other"},
        )
        self.assertEqual(identity["subject"], "svc: other")

    def test_an_orphan_consumer_line_has_no_identity(self):
        """So it cannot be baselined, and must be reported."""
        for subjects in (None, set()):
            with self.subTest(subjects=subjects):
                self.assertIsNone(
                    violation_identity(self.DIRECT, component_subjects=subjects)
                )

    def test_a_consumer_line_naming_an_unrelated_subject_has_no_identity(self):
        self.assertIsNone(
            violation_identity(self.DIRECT, component_subjects={"other"})
        )

    def test_a_consumer_line_does_not_bind_to_a_bare_name_prefix(self):
        """MUT-GRAPH-504: the ': ' in the binding test carries the whole rule.

        A candidate subject binds an AFFECTED CONSUMERS line only when the
        rendered body begins with that name followed by the separator. Drop
        the separator and a component whose name is a strict prefix of the
        annotated one claims the annotation, so the baseline identity names a
        component that did not drift. Every unrelated candidate the suite had
        tried until now was either the exact owner or a name sharing no prefix
        with it, and both of those answer the same way with or without the
        separator, which is how the fault went unnoticed.
        """
        self.assertIsNone(
            violation_identity(self.PREFIX_OF_OWNER, component_subjects={"svc"})
        )

    def test_the_prefixed_line_still_binds_to_the_component_that_owns_it(self):
        """The premise: the line itself is well formed and does bind.

        Without this, the assertion above could hold because the regex never
        matched the line at all, and it would keep holding if consumer binding
        stopped working entirely.
        """
        identity = violation_identity(
            self.PREFIX_OF_OWNER, component_subjects={"svcx"}
        )
        self.assertEqual(identity["kind"], "affected-consumers")
        self.assertEqual(identity["subject"], "svcx")

    def test_the_owner_outranks_a_shorter_name_that_prefixes_it(self):
        """The contrast: refusing the prefix must not refuse the real owner.

        Both names are offered as candidates here, which is the case a real
        issue set produces when two components have related names, and the
        annotation still has to go to the one whose display name it carries.
        """
        identity = violation_identity(
            self.PREFIX_OF_OWNER, component_subjects={"svc", "svcx"}
        )
        self.assertEqual(identity["subject"], "svcx")

    def test_the_two_facets_are_distinct_identities(self):
        direct = violation_identity(self.DIRECT, component_subjects={"svc"})
        transitive = violation_identity(self.TRANSITIVE, component_subjects={"svc"})
        self.assertNotEqual(direct["id"], transitive["id"])


class ConsumerPreviewAccountingTests(unittest.TestCase):
    """OBL-GRAPH-006: a bounded consumer list must account for every name."""

    COMPONENTS = tuple(f"component-{index:02d}" for index in range(20))
    OMITTED_SUFFIX = " more"

    def _rendered_component_list(self, components) -> str:
        """Return the body of the 'Components:' line the report prints."""
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            print_consumer_impact(
                [
                    {
                        "component": "svc",
                        "facets": ["boundary"],
                        "components": list(components),
                        "transitive": False,
                    }
                ]
            )
        label = "Components:"
        line = next(
            candidate.strip()
            for candidate in stdout.getvalue().splitlines()
            if candidate.strip().startswith(label)
        )
        return line[len(label):].strip()

    def _shown_and_omitted(self, components):
        """Split the rendered line into the names shown and the count omitted."""
        pieces = self._rendered_component_list(components).split(", ")
        tail = pieces[-1]
        if tail.startswith("+") and tail.endswith(self.OMITTED_SUFFIX):
            return pieces[:-1], int(tail[1:-len(self.OMITTED_SUFFIX)])
        return pieces, 0

    def test_the_names_shown_and_the_count_omitted_cover_every_consumer(self):
        """MUT-GRAPH-405: the preview must not lose a name it does not count.

        The preview is a promise about a list the reader cannot see in full:
        these names, and this many others. A renderer that shows one fewer
        name than it reserved room for, while still reporting the old omitted
        count, drops a downstream consumer out of the report with nothing left
        to say it existed. The suite asserted that the first name appeared,
        that a late name did not, and that some '+N more' suffix was printed,
        and all three of those survive the fault untouched. None of them added
        the shown names and the omitted count back together and compared the
        total against the list that went in, which is the only assertion that
        notices a name has gone missing.
        """
        shown, omitted = self._shown_and_omitted(self.COMPONENTS)
        self.assertEqual(
            len(shown) + omitted,
            len(self.COMPONENTS),
            (shown, omitted),
        )
        self.assertEqual(shown, list(self.COMPONENTS[: len(shown)]))

    def test_the_fixture_really_makes_the_preview_truncate(self):
        """The premise: the accounting above is asserted on a truncated list.

        A list short enough to be printed whole satisfies the sum for free and
        would keep satisfying it however badly the bounded path behaved, so
        the fixture has to be long enough for the bound to engage. The names
        also have to be free of the ', ' separator, because splitting on it is
        how the assertion counts them.
        """
        shown, omitted = self._shown_and_omitted(self.COMPONENTS)
        self.assertGreater(omitted, 0)
        self.assertLess(len(shown), len(self.COMPONENTS))
        for name in self.COMPONENTS:
            self.assertNotIn(", ", name)

    def test_a_list_that_fits_is_printed_whole_and_omits_nothing(self):
        """The contrast: bounding must not cost a name from a list that fits.

        A renderer that dropped every consumer, or that appended an omitted
        count to a complete list, would also make the sum above come out
        wrong in a way this notices.
        """
        shown, omitted = self._shown_and_omitted(self.COMPONENTS[:3])
        self.assertEqual(shown, list(self.COMPONENTS[:3]))
        self.assertEqual(omitted, 0)

    def test_a_sequence_exactly_at_the_limit_shows_all_of_it(self):
        """MUT-GRAPH-405 at the boundary, with the limit stated outright.

        The report reaches the helper on its default limit, so this states the
        boundary directly instead: a sequence of exactly `limit` values omits
        nothing, and therefore has to print every value and no count at all.
        """
        self.assertEqual(
            _bounded_diagnostic_list_preview(["a", "b", "c"], limit=3),
            "a, b, c",
        )

    def test_one_value_past_the_limit_shows_the_limit_and_counts_the_rest(self):
        """The paired case: the first omission costs a count, not a name."""
        self.assertEqual(
            _bounded_diagnostic_list_preview(["a", "b", "c", "d"], limit=3),
            "a, b, c, +1 more",
        )


class VerifyJsonShapeTests(unittest.TestCase):
    """OBL-GRAPH-017: one key set, whatever the verdict."""

    def _verified(self, build, *extra):
        with Scenario() as scene:
            build(scene)
            scene.commit()
            generated = run_cli(scene.root, "generate", "--source", "head")
            if generated.returncode == 0:
                scene.commit("lock")
            result = run_cli(
                scene.root, "verify", "--source", "head", "--format", "json", *extra
            )
            return result.returncode, json.loads(result.stdout)

    def _clean(self, scene):
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")

    def _drifted(self, scene):
        self._clean(scene)

    def _outcomes(self) -> dict:
        outcomes = {}
        outcomes["clean"] = self._verified(self._clean)

        with Scenario() as scene:
            self._clean(scene)
            scene.commit()
            assert run_cli(scene.root, "generate", "--source", "head").returncode == 0
            scene.commit("lock")
            scene.append_line("svc/main.py", "y\n")
            scene.commit("drift")
            result = run_cli(
                scene.root, "verify", "--source", "head", "--format", "json"
            )
            outcomes["drift"] = (result.returncode, json.loads(result.stdout))

        def broken(scene):
            scene.config["components"] = {}
            scene.file("svc/main.py", "x\n")

        outcomes["config error"] = self._verified(broken)
        return outcomes

    def test_every_outcome_carries_the_same_keys(self):
        outcomes = self._outcomes()
        codes = {label: code for label, (code, _document) in outcomes.items()}
        self.assertEqual(codes["clean"], 0)
        self.assertEqual(codes["drift"], DRIFT)
        self.assertEqual(codes["config error"], COULD_NOT_CHECK)
        shapes = {label: sorted(document) for label, (_code, document) in outcomes.items()}
        self.assertEqual(len(set(map(tuple, shapes.values()))), 1, shapes)
        self.assertNotIn("baseline", next(iter(shapes.values())))

    def test_the_named_fields_are_always_arrays(self):
        for label, (_code, document) in self._outcomes().items():
            for field in ALWAYS_ARRAYS:
                with self.subTest(outcome=label, field=field):
                    self.assertIsInstance(document[field], list)

    def test_the_baseline_key_appears_exactly_when_one_was_requested(self):
        _code, without = self._verified(self._clean)
        _code, with_write = self._verified(self._clean, "--write-baseline", "b.json")
        self.assertNotIn("baseline", without)
        self.assertIn("baseline", with_write)
        self.assertEqual(sorted(with_write), sorted(set(without) | {"baseline"}))


if __name__ == "__main__":
    unittest.main()
