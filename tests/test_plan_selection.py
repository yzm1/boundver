"""What a review plan selects, what triggers impact, and how it says so.

A plan exists to tell a CI job what to run, so `test_components` is the load
bearing field: it must be the union of what changed and what that reaches, as
a relation rather than as a list someone wrote down. What feeds the second
half of that union is a policy - a boundary or compat transition, or an edit
to the consumer graph - and the cases that matter most are the ones that must
*not* trigger it.

The tags are the third question. A facet line says whether it was gated, and
"gated at the base but not the target" is the case a reader most needs to see
marked, because that is where a contract was quietly dropped.

Covers OBL-GRAPH-007, OBL-GRAPH-008, OBL-GRAPH-015, OBL-GRAPH-016,
OBL-FACETS-006 and OBL-FACETS-011.
"""

from __future__ import annotations

import json
import unittest

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from boundver._review_plan import _impact_names, _sorted_names
from tests._parity import run_cli
from tests._scenarios import Scenario

OPENAPI = "openapi: 3.1.0\ninfo:\n  title: t\n  version: '1'\npaths: {}\n"

PROFILE = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

#: Component names the generated graphs draw from. Small, so a random graph
#: is dense in edges rather than sparse in a five-name space.
NAMES = ("a", "b", "c", "d")


def _lock(scene, message: str) -> None:
    result = run_cli(scene.root, "generate", "--source", "head")
    assert result.returncode == 0, result.stderr
    scene.commit(message)


class _Reviewed:
    """One range, with the plan and the text view of the same review."""

    def __init__(self, build, edit, *extra) -> None:
        scene = Scenario()
        build(scene)
        scene.commit()
        _lock(scene, "lock")
        self.base = scene.head()
        edit(scene)
        scene.commit("edit")
        _lock(scene, "relock")
        self.scene = scene
        self.extra = list(extra)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.scene.close()

    def _run(self, *arguments):
        result = run_cli(
            self.scene.root, "review", "--base", self.base,
            "--target", self.scene.head(), *self.extra, *arguments,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def plan(self) -> dict:
        return json.loads(self._run("--format", "plan"))

    def document(self) -> dict:
        return json.loads(self._run("--format", "json"))

    def text(self) -> str:
        return self._run("--format", "text")


def _consumer_names(plan: dict) -> set:
    names = set()
    for row in plan.get("consumer_impact", []):
        for entry in row.get("components", []):
            names.add(entry["name"])
    return names


class PlanNameCollectionTests(unittest.TestCase):
    """The two helpers that turn review rows into plan names.

    Both filter what they collect - _sorted_names keeps only string names,
    _impact_names reads the external_consumers key - and neither filter was
    exercised by any test driving the CLI, because a validated config
    cannot produce a non-string name and the plans under test carried no
    external consumers. MUT-GRAPH-202 and MUT-GRAPH-203 both survived.
    Calling the helpers directly is the only way to reach either.
    """

    def test_only_string_row_names_are_collected(self):
        rows = [
            {"name": "svc"},
            {"name": None},
            {"name": 7},
            {"name": ["sdk"]},
            {"name": "api"},
            {},
        ]
        self.assertEqual(_sorted_names(rows), ["api", "svc"])

    def test_row_names_are_deduplicated_and_sorted(self):
        """The premise: the filter is not the only thing being asserted."""
        rows = [{"name": "b"}, {"name": "a"}, {"name": "b"}]
        self.assertEqual(_sorted_names(rows), ["a", "b"])

    def test_external_consumers_are_read_from_their_own_key(self):
        impacts = [
            {
                "components": [{"name": "sdk"}, {"name": None}],
                "external_consumers": [{"name": "mobile"}, {"nope": "x"}],
            },
            {"components": [{"name": "api"}], "external_consumers": []},
        ]
        components, external = _impact_names(impacts)
        self.assertEqual(components, ["api", "sdk"])
        self.assertEqual(external, ["mobile"])

    def test_an_impact_without_either_key_is_tolerated(self):
        """Fail closed on shape, not loudly: a row missing both is empty."""
        self.assertEqual(_impact_names([{}]), ([], []))


class SelectionRelationTests(unittest.TestCase):
    """OBL-GRAPH-007: test_components is a union, asserted as one."""

    def _graph(self, edges) -> callable:
        def build(scene):
            for index, name in enumerate(NAMES):
                consumers = sorted(edges.get(name, set()))
                scene.component(
                    name, path=name,
                    boundary=["api"] if index == 0 else None,
                    provider="path-hash" if index == 0 else "leaf",
                    consumers=consumers or None,
                )
                if index == 0:
                    scene.file(f"{name}/api/v1.yaml", OPENAPI)
                else:
                    scene.file(f"{name}/index.ts", "export const x = 1;\n")

        return build

    def _assert_relation(self, plan: dict) -> None:
        selection = plan["selection"]
        self.assertEqual(
            selection["test_components"],
            sorted(set(selection["changed_components"])
                   | set(selection["impacted_components"])),
        )
        named = {entry["name"] for entry in plan["changed_components"]}
        named |= _consumer_names(plan)
        self.assertLessEqual(named, set(selection["test_components"]))

    def test_the_relation_holds_for_a_hand_built_graph(self):
        """The premise: a graph with real impact, so the union is not trivial."""
        edges = {"a": {"b", "c"}, "b": {"c"}}
        with _Reviewed(
            self._graph(edges),
            lambda scene: scene.append_line("a/api/v1.yaml", "change\n"),
            "--transitive",
        ) as review:
            plan = review.plan()
            self.assertEqual(plan["selection"]["changed_components"], ["a"])
            self.assertEqual(plan["selection"]["impacted_components"], ["b", "c"])
            self._assert_relation(plan)

    @PROFILE
    @given(
        edges=st.dictionaries(
            st.sampled_from(NAMES),
            st.frozensets(st.sampled_from(NAMES), max_size=3),
            max_size=len(NAMES),
        )
    )
    def test_the_relation_holds_for_a_generated_graph(self, edges):
        """Cycles and repeated names included.

        A self-edge is dropped rather than skipped over: config validation
        refuses one ("cannot consume its own boundary"), so a graph carrying
        it never reaches a review, and letting the example through would only
        make the property vacuous. Every other shape - two-cycles, longer
        cycles, a name reached by several paths - is generated and asserted.
        """
        declared = {
            name: {target for target in targets if target != name}
            for name, targets in edges.items()
        }
        with Scenario() as scene:
            self._graph(declared)(scene)
            scene.commit()
            built = run_cli(scene.root, "generate", "--source", "head")
            self.assertEqual(built.returncode, 0, built.stderr)
            scene.commit("lock")
            base = scene.head()
            scene.append_line(f"{NAMES[0]}/api/v1.yaml", "change\n")
            scene.commit("edit")
            _lock(scene, "relock")
            result = run_cli(
                scene.root, "review", "--base", base, "--target", scene.head(),
                "--transitive", "--format", "plan",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self._assert_relation(json.loads(result.stdout))


def _graph_with_consumer(scene) -> None:
    scene.component("svc", path="svc", boundary=["api"], consumers=["sdk"])
    scene.component("sdk", path="sdk", provider="leaf")
    scene.file("svc/api/v1.yaml", OPENAPI)
    scene.file("svc/impl.txt", "one\n")
    scene.file("sdk/index.ts", "export const x = 1;\n")


class ImpactTriggerTests(unittest.TestCase):
    """OBL-GRAPH-008: which transitions reach the consumer graph."""

    def test_an_exact_only_change_triggers_nothing(self):
        for extra in ([], ["--transitive"]):
            with self.subTest(transitive=bool(extra)):
                with _Reviewed(
                    _graph_with_consumer,
                    lambda scene: scene.append_line("svc/impl.txt", "two\n"),
                    *extra,
                ) as review:
                    plan = review.plan()
                    moved = {
                        facet["facet"]
                        for entry in plan["changed_components"]
                        for facet in entry["facets"]
                    }
                    self.assertEqual(moved, {"exact"})
                    self.assertEqual(plan["consumer_impact"], [])
                    self.assertEqual(plan["selection"]["impacted_components"], [])

    def test_a_boundary_change_triggers_impact(self):
        """The contrast: the same graph, a transition that does reach it."""
        with _Reviewed(
            _graph_with_consumer,
            lambda scene: scene.append_line("svc/api/v1.yaml", "x-note: a\n"),
            "--transitive",
        ) as review:
            plan = review.plan()
            self.assertEqual(plan["selection"]["impacted_components"], ["sdk"])
            self.assertEqual(_consumer_names(plan), {"sdk"})

    def test_a_graph_edit_alone_triggers_impact(self):
        """No facet moved at all, and the edit is to the graph itself."""
        def edit(scene):
            scene.config["components"]["svc"]["external_consumers"] = ["mobile"]
            scene.write_config()

        with _Reviewed(_graph_with_consumer, edit, "--transitive") as review:
            plan = review.plan()
            moved = {
                facet["facet"]
                for entry in plan["changed_components"]
                for facet in entry["facets"]
            }
            self.assertEqual(moved, set())
            self.assertEqual(plan["selection"]["impacted_components"], ["sdk"])


class GatedImpactTests(unittest.TestCase):
    """OBL-GRAPH-016: verify emits impact only for gated boundary or compat."""

    def _verified(self, verify_facets, edit):
        with Scenario() as scene:
            scene.component(
                "svc", path="svc", boundary=["api"], consumers=["sdk"],
                verify_facets=verify_facets,
            )
            scene.component("sdk", path="sdk", provider="leaf")
            scene.file("svc/api/v1.yaml", OPENAPI)
            scene.file("sdk/index.ts", "export const x = 1;\n")
            scene.commit()
            _lock(scene, "lock")
            edit(scene)
            scene.commit("edit")
            result = run_cli(
                scene.root, "verify", "--source", "head", "--transitive",
                "--format", "json",
            )
            return result, json.loads(result.stdout)

    def test_a_gated_boundary_mismatch_emits_a_row(self):
        """The premise: this fixture does produce impact when gated."""
        result, document = self._verified(
            None, lambda scene: scene.append_line("svc/api/v1.yaml", "x-note: a\n")
        )
        self.assertEqual(result.returncode, 4)
        self.assertEqual(
            document["consumer_impact"],
            [{
                "component": "svc", "components": ["sdk"],
                "external_consumers": [], "facets": ["boundary"], "transitive": True,
            }],
        )
        self.assertTrue(
            any(line.startswith("AFFECTED CONSUMERS") for line in document["issues"])
        )

    def test_an_ungated_boundary_mismatch_emits_none(self):
        result, document = self._verified(
            ["exact"], lambda scene: scene.append_line("svc/api/v1.yaml", "x-note: a\n")
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(document["consumer_impact"], [])
        self.assertFalse(
            any(line.startswith("AFFECTED CONSUMERS") for line in document["issues"])
        )
        self.assertTrue(
            any("svc.boundary" in line for line in document["observations"])
        )

    def test_gated_exact_drift_alone_emits_none(self):
        with Scenario() as scene:
            scene.component(
                "svc", path="svc", boundary=["api"], consumers=["sdk"],
            )
            scene.component("sdk", path="sdk", provider="leaf")
            scene.file("svc/api/v1.yaml", OPENAPI)
            scene.file("svc/impl.txt", "one\n")
            scene.file("sdk/index.ts", "export const x = 1;\n")
            scene.commit()
            _lock(scene, "lock")
            scene.append_line("svc/impl.txt", "two\n")
            scene.commit("edit")
            result = run_cli(
                scene.root, "verify", "--source", "head", "--transitive",
                "--format", "json",
            )
            document = json.loads(result.stdout)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(document["consumer_impact"], [])
            self.assertFalse(
                any(line.startswith("AFFECTED CONSUMERS") for line in document["issues"])
            )

    def test_boundary_and_compat_together_merge_into_one_row(self):
        with Scenario() as scene:
            scene.config["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "openapi-canonical", "paths": ["api/v1.yaml"]},
                    "version_source": {"file": "version.json", "field": "version"},
                    "consumers": ["sdk"],
                },
                "sdk": {"path": "sdk", "boundary": {"provider": "leaf", "paths": []}},
            }
            scene.file("svc/api/v1.yaml", OPENAPI)
            scene.file("svc/version.json", '{"version": "1.0.0"}\n')
            scene.file("sdk/index.ts", "export const x = 1;\n")
            scene.commit()
            _lock(scene, "lock")
            (scene.root / "svc" / "version.json").write_text(
                '{"version": "2.0.0"}\n', encoding="utf-8"
            )
            scene.append_line("svc/api/v1.yaml", "x-note: changed\n")
            scene.commit("both")
            result = run_cli(
                scene.root, "verify", "--source", "head", "--transitive",
                "--format", "json",
            )
            rows = json.loads(result.stdout)["consumer_impact"]
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual(rows[0]["facets"], ["boundary", "compat"])


class FacetTagTests(unittest.TestCase):
    """OBL-FACETS-006: gated at either end is selected; at neither, observed."""

    CELLS = {
        "gated at both": (["exact", "boundary"], ["exact", "boundary"], "[selected]"),
        "gated at base only": (["exact", "boundary"], ["exact"], "[selected]"),
        "gated at target only": (["exact"], ["exact", "boundary"], "[selected]"),
        "gated at neither": (["exact"], ["exact"], "[observed]"),
    }

    def _tag_for_boundary(self, base_facets, target_facets) -> str:
        def build(scene):
            scene.component(
                "svc", path="svc", boundary=["api"], verify_facets=list(base_facets)
            )
            scene.file("svc/api/v1.yaml", OPENAPI)

        def edit(scene):
            scene.config["components"]["svc"]["verify_facets"] = list(target_facets)
            scene.write_config()
            scene.append_line("svc/api/v1.yaml", "x-note: changed\n")

        with _Reviewed(build, edit) as review:
            lines = [
                line.strip() for line in review.text().splitlines()
                if line.strip().startswith("boundary:")
            ]
            assert len(lines) == 1, lines
            return lines[0].rsplit(" ", 1)[-1]

    def test_every_cell_of_the_table(self):
        for label, (base_facets, target_facets, expected) in self.CELLS.items():
            with self.subTest(case=label):
                self.assertEqual(
                    self._tag_for_boundary(base_facets, target_facets), expected
                )

    def test_the_two_tags_are_the_only_ones(self):
        """The premise: both spellings really do appear, so neither is dead."""
        produced = {
            self._tag_for_boundary(base, target)
            for base, target, _expected in self.CELLS.values()
        }
        self.assertEqual(produced, {"[selected]", "[observed]"})


class StructuralCandidateTests(unittest.TestCase):
    """OBL-FACETS-011: one report per boundary transition, and none otherwise."""

    def _reports(self, build, edit) -> list:
        with _Reviewed(build, edit, "--transitive") as review:
            document = review.document()
            return [
                report["component"]
                for report in document["structural_changes"]["reports"]
            ]

    def test_a_boundary_transition_gets_one_report(self):
        """The premise: this fixture does produce a report."""
        def build(scene):
            scene.component(
                "svc", path="svc", provider="openapi-canonical",
                boundary=["api/v1.yaml"],
            )
            scene.file("svc/api/v1.yaml", OPENAPI)

        self.assertEqual(
            self._reports(
                build, lambda scene: scene.append_line("svc/api/v1.yaml", "x-note: a\n")
            ),
            ["svc"],
        )

    def test_an_exact_only_change_gets_none(self):
        def build(scene):
            scene.component("svc", path="svc", boundary=["api"], consumers=["sdk"])
            scene.component("sdk", path="sdk", provider="leaf")
            scene.file("svc/api/v1.yaml", OPENAPI)
            scene.file("svc/impl.txt", "one\n")
            scene.file("sdk/index.ts", "export const x = 1;\n")

        self.assertEqual(
            self._reports(build, lambda scene: scene.append_line("svc/impl.txt", "two\n")),
            [],
        )

    def test_a_metadata_only_change_gets_none(self):
        def edit(scene):
            scene.config["components"]["svc"]["external_consumers"] = ["mobile"]
            scene.write_config()

        self.assertEqual(self._reports(_graph_with_consumer, edit), [])

    def test_a_compat_change_gets_none(self):
        def build(scene):
            scene.config["components"] = {
                "svc": {
                    "path": "svc",
                    "boundary": {"provider": "openapi-canonical", "paths": ["api/v1.yaml"]},
                    "version_source": {"file": "version.json", "field": "version"},
                },
            }
            scene.file("svc/api/v1.yaml", OPENAPI)
            scene.file("svc/version.json", '{"version": "1.0.0"}\n')

        def edit(scene):
            (scene.root / "svc" / "version.json").write_text(
                '{"version": "2.0.0"}\n', encoding="utf-8"
            )

        self.assertEqual(self._reports(build, edit), [])


class MergeBaseDisclosureTests(unittest.TestCase):
    """OBL-GRAPH-015: the human view says which base was actually used."""

    def _branched(self):
        scene = Scenario()
        scene.component("svc", path="svc", provider="leaf")
        scene.file("svc/main.py", "x\n")
        scene.commit()
        _lock(scene, "lock")
        trunk = scene.git("rev-parse", "--abbrev-ref", "HEAD")
        scene.branch("feature")
        scene.append_line("svc/main.py", "feature\n")
        scene.commit("feature work")
        _lock(scene, "feature lock")
        feature = scene.head()
        scene.checkout(trunk)
        scene.append_line("svc/main.py", "trunk\n")
        scene.commit("trunk work")
        _lock(scene, "trunk lock")
        return scene, trunk, feature

    def _lines(self, scene, trunk, feature, *extra) -> dict:
        result = run_cli(
            scene.root, "review", "--base", trunk, "--target", feature, *extra
        )
        assert result.returncode == 0, result.stderr
        found = {}
        for line in result.stdout.splitlines():
            stripped = line.strip()
            for label in ("Requested base:", "Effective base:", "Target:"):
                if stripped.startswith(label):
                    found[label] = stripped[len(label):].strip()
        return found

    def test_the_merge_base_is_named_and_marked(self):
        scene, trunk, feature = self._branched()
        try:
            merge_base = scene.git("merge-base", trunk, feature)
            tip = scene.git("rev-parse", trunk)
            self.assertNotEqual(merge_base, tip)
            lines = self._lines(scene, trunk, feature, "--merge-base")
            self.assertEqual(lines["Requested base:"], f"{trunk} -> {tip}")
            self.assertEqual(lines["Effective base:"], f"{merge_base} (merge base)")
            self.assertEqual(lines["Target:"], feature)
        finally:
            scene.close()

    def test_without_the_flag_the_two_commits_are_the_same(self):
        scene, trunk, feature = self._branched()
        try:
            tip = scene.git("rev-parse", trunk)
            lines = self._lines(scene, trunk, feature)
            self.assertEqual(lines["Requested base:"], f"{trunk} -> {tip}")
            self.assertEqual(lines["Effective base:"], tip)
            self.assertNotIn("merge base", lines["Effective base:"])
        finally:
            scene.close()


if __name__ == "__main__":
    unittest.main()
